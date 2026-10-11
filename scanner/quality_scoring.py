"""Transparent, rule-based content quality scoring (0-100, lower = more potential problems).

Five criteria, each with independent, explainable rules:

  A. Originality & distinctiveness   (default 25 pts)
  B. Depth & topical coverage        (default 20 pts)
  C. Evidence & factual support      (default 20 pts)
  D. Specificity & usefulness        (default 20 pts)
  E. Structure & readability         (default 15 pts)

Each rule removes a *fraction* of its criterion's points, so weights can be
changed freely in config.yaml. Every lost point is tied to a named signal that
is shown in the output. This is a screening aid for manual review:
  * it never labels anything as AI-generated,
  * no single signal is treated as proof of low quality,
  * originality is only judged against articles this tool collected,
  * evidence expectations depend on the article type (news vs opinion, etc.).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from .extractor import ArticleData
from .topics import STOPWORDS, tokenize

CRITERIA = ("originality", "depth", "evidence", "usefulness", "readability")
DEFAULT_WEIGHTS = {"originality": 25, "depth": 20, "evidence": 20, "usefulness": 20, "readability": 15}
DEFAULT_TYPES = {
    "news": {"expected_words": 300, "evidence_importance": 0.9},
    "tutorial": {"expected_words": 700, "evidence_importance": 0.4},
    "listicle": {"expected_words": 600, "evidence_importance": 0.5},
    "review": {"expected_words": 700, "evidence_importance": 0.6},
    "opinion": {"expected_words": 500, "evidence_importance": 0.3},
    "general": {"expected_words": 500, "evidence_importance": 0.6},
}
DEFAULT_THRESHOLDS = {
    "near_duplicate_similarity": 0.80,
    "high_similarity": 0.60,
    "duplicate_paragraph_ratio": 0.15,
    "generic_phrase_per_1000_words": 4,
    "keyword_phrase_density": 0.035,
    "thin_section_words": 40,
    "max_avg_sentence_words": 28,
    "max_avg_paragraph_words": 120,
    "min_concrete_details_per_100_words": 1.5,
    "vague_words_per_100_words": 2.0,
}
DEFAULT_CATEGORIES = [
    {"min": 80, "label": "Strong quality signals"},
    {"min": 60, "label": "Reasonable quality"},
    {"min": 40, "label": "Needs manual review"},
    {"min": 0, "label": "Potentially very low quality"},
]
NOT_SCORED = "Not scored (insufficient text extracted)"

# Filler / boilerplate phrasing. Common in weak content but also used by good writers,
# so it only ever costs a limited number of points.
GENERIC_PHRASES = [
    "in today's fast-paced world", "in today's digital age", "in today's digital world", "in today's world",
    "in this article, we will", "in this article we will", "in this blog post", "in this post, we",
    "it is important to note", "it's important to note", "it is worth noting", "at the end of the day",
    "without further ado", "in conclusion", "to sum up", "game-changer", "game changer",
    "unlock the power", "unlock the potential", "take your business to the next level", "to the next level",
    "look no further", "whether you're a beginner or", "whether you are a beginner or", "let's dive in",
    "dive deep into", "deep dive into", "delve into", "ever-evolving", "ever-changing", "plays a crucial role",
    "plays a vital role", "a wide range of", "the world of", "revolutionize", "seamlessly", "cutting-edge",
    "harness the power", "navigate the complexities", "it goes without saying", "needless to say",
    "first and foremost", "last but not least", "in the realm of", "embark on", "elevate your",
    "stay ahead of the curve", "one-stop shop", "everything you need to know", "the ultimate guide",
    "in the ever", "a testament to", "rest assured", "tapestry", "landscape of",
]
VAGUE_CLAIMS = [
    "studies show", "studies have shown", "research shows", "research has shown", "experts say",
    "experts agree", "experts believe", "it is widely known", "it is widely believed", "many people believe",
    "according to experts", "statistics show", "data shows", "it has been proven", "science says",
    "everyone knows", "it is well known", "it's well known", "many experts",
]
VAGUE_WORDS = ["various", "numerous", "several", "things", "stuff", "a lot of", "lots of", "basically",
               "very", "really", "certain", "many", "somehow", "kind of", "sort of", "etc"]
EVIDENCE_MARKERS = re.compile(
    r"according to|\bstudy\b|\bsurvey\b|\breport(ed)?\b|\bresearch(ers)? (by|from|at)\b|data (from|by|shows)|"
    r"found that|\bpercent\b|%|\bsaid\b|\bsays\b|\btold\b|\bcited?\b|\bsource:|\[\d+\]|\bper the\b|\bdocumentation\b",
    re.I,
)
EXAMPLE_MARKERS = re.compile(r"for example|for instance|e\.g\.|such as|case study|example:|here's how|"
                             r"step \d|screenshot|we tested|i tested|in our test", re.I)
REVIEW_MARKERS = re.compile(r"\bpros?\b|\bcons?\b|\brating\b|\bprice\b|\bpricing\b|\$\d|tested|verdict|"
                            r"\bscore\b|out of \d|/10\b|/5\b", re.I)
UNIT_RE = re.compile(r"\b\d[\d,.]*\s?(%|percent|ms|s|sec|seconds|minutes|hours|days|kb|mb|gb|px|usd|\$|€|£|x)\b"
                     r"|[$€£]\s?\d", re.I)
NUMBER_RE = re.compile(r"\b\d[\d,.]*\b")
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"“'])")


@dataclass
class Signal:
    criterion: str
    code: str
    penalty: float  # fraction of the criterion's points removed (0-1)
    detail: str  # exact measurement
    explanation: str  # short plain-language reason used in "explanation"
    points: float = 0.0  # filled in after weighting


@dataclass
class QualityResult:
    score: float | None
    category: str
    criterion_scores: dict[str, float] = field(default_factory=dict)
    signals: list[Signal] = field(default_factory=list)
    strengths: list[str] = field(default_factory=list)
    article_type: str = "general"
    explanation: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def flags(self) -> list[str]:
        return [s.code for s in self.signals]

    @property
    def signals_text(self) -> str:
        return "; ".join(f"[{s.criterion} -{s.points:.1f}] {s.code}: {s.detail}" for s in self.signals)


# ----------------------------------------------------------------------------- helpers

def _sentences(text: str) -> list[str]:
    out = []
    for line in text.split("\n"):
        out.extend(s.strip() for s in SENTENCE_SPLIT.split(line) if len(s.split()) >= 3)
    return out


def _syllables(word: str) -> int:
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return 0
    groups = re.findall(r"[aeiouy]+", w)
    count = len(groups)
    if w.endswith("e") and count > 1 and not w.endswith(("le", "ee")):
        count -= 1
    return max(1, count)


def flesch_reading_ease(text: str) -> float | None:
    sents = _sentences(text)
    words = re.findall(r"[A-Za-z]+", text)
    if len(sents) < 3 or len(words) < 100:
        return None
    syl = sum(_syllables(w) for w in words)
    return 206.835 - 1.015 * (len(words) / len(sents)) - 84.6 * (syl / len(words))


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\s+", " ", text.lower())).strip()


def _count_phrases(text_lower: str, phrases: list[str]) -> Counter:
    c: Counter = Counter()
    for p in phrases:
        n = len(re.findall(r"(?<![a-z])" + re.escape(p) + r"(?![a-z])", text_lower))
        if n:
            c[p] = n
    return c


def detect_article_type(article: ArticleData) -> str:
    types = set(article.jsonld_types)
    title = (article.title or "").lower()
    url = article.final_url.lower()
    if types & {"opinionnewsarticle"} or re.search(r"\bopinion\b|/opinion/|/op-ed/", url):
        return "opinion"
    if types & {"review", "reviewnewsarticle"} or re.search(r"\breview(ed)?\b|\bvs\.?\b|\bversus\b|hands-on|compared", title):
        return "review"
    if re.search(r"^\d+\s|\b(top|best)\s+\d+|\b\d+\s+(ways|tips|tools|examples|reasons|ideas|strategies|"
                 r"mistakes|things|steps|tricks|apps|plugins|alternatives|templates)\b", title):
        return "listicle"
    if types & {"howto", "techarticle"} or re.search(r"\bhow to\b|tutorial|step[- ]by[- ]step|\bguide to\b|"
                                                     r"\bsetting up\b|\binstall", title):
        return "tutorial"
    if types & {"newsarticle", "reportagenewsarticle", "analysisnewsarticle"} or "/news/" in url:
        return "news"
    if re.search(r"\b(i think|my take|why i|we need to|stop |unpopular|should you|the case for|the case against)", title):
        return "opinion"
    return "general"


def _sections(article: ArticleData) -> list[tuple[str, int]]:
    """(heading, words until next heading) for every heading in the body."""
    out: list[tuple[str, int]] = []
    current, words = None, 0
    for tag, text in article.blocks:
        if tag in ("h2", "h3", "h4", "h5", "h6"):
            if current is not None:
                out.append((current, words))
            current, words = text, 0
        elif current is not None:
            words += len(text.split())
    if current is not None:
        out.append((current, words))
    return out


# ----------------------------------------------------------------------------- scorer

class QualityScorer:
    def __init__(self, quality_config: dict | None = None):
        cfg = quality_config or {}
        self.weights = {**DEFAULT_WEIGHTS, **(cfg.get("weights") or {})}
        self.types = {k: {**DEFAULT_TYPES.get(k, DEFAULT_TYPES["general"]), **v}
                      for k, v in {**DEFAULT_TYPES, **(cfg.get("article_types") or {})}.items()}
        self.t = {**DEFAULT_THRESHOLDS, **(cfg.get("thresholds") or {})}
        self.categories = sorted(cfg.get("categories") or DEFAULT_CATEGORIES, key=lambda c: -c["min"])
        self.min_words = int(cfg.get("min_words_to_score", 80))

    def categorize(self, score: float | None) -> str:
        if score is None:
            return NOT_SCORED
        for c in self.categories:
            if score >= c["min"]:
                return c["label"]
        return self.categories[-1]["label"]

    def score(self, article: ArticleData, similarity: tuple[float, str] = (0.0, ""),
              keywords: list[str] | None = None) -> QualityResult:
        atype = detect_article_type(article)
        words = article.word_count
        if article.extraction_status in ("failed", "js_rendered_suspected") or words < self.min_words:
            reason = (f"only {words} words extracted (status: {article.extraction_status}); "
                      f"not enough text to judge quality - review manually")
            return QualityResult(None, NOT_SCORED, article_type=atype, explanation=reason, notes=[reason])

        text = article.body_text
        lower = text.lower()
        profile = self.types.get(atype, self.types["general"])
        signals: list[Signal] = []
        strengths: list[str] = []
        sig = lambda *a: signals.append(Signal(*a))  # noqa: E731

        # ------------------------------------------------------------ A. originality
        sim, sim_label = similarity
        if sim >= self.t["near_duplicate_similarity"]:
            sig("originality", "near_duplicate_of_collected_article", 0.6,
                f"text similarity {sim:.2f} with {sim_label}",
                "Very similar to another collected article.")
        elif sim >= self.t["high_similarity"]:
            sig("originality", "highly_similar_to_collected_article", 0.3,
                f"text similarity {sim:.2f} with {sim_label}",
                "Substantially overlaps with another collected article.")

        paras = [_norm(p) for p in article.paragraphs + article.list_items if len(p.split()) >= 8]
        if len(paras) >= 4:
            counts = Counter(paras)
            dup = sum(c - 1 for c in counts.values() if c > 1)
            sents = [_norm(s) for s in _sentences(text) if len(s.split()) >= 8]
            sc = Counter(sents)
            dup_sent = sum(c - 1 for c in sc.values() if c > 1)
            ratio = max(dup / len(paras), dup_sent / max(1, len(sents)))
            if ratio >= self.t["duplicate_paragraph_ratio"]:
                sig("originality", "repetitive_paragraphs", min(0.4, 0.25 + ratio / 2),
                    f"{ratio:.0%} of paragraphs/sentences are repeated",
                    "Multiple repetitive sections.")
        heads = [_norm(h) for _, h in article.headings if h.strip()]
        rep_heads = [h for h, c in Counter(heads).items() if c > 1]
        if rep_heads:
            sig("originality", "repeated_headings", 0.1, f"repeated headings: {', '.join(rep_heads[:3])}",
                "Repeated section headings.")

        generic = _count_phrases(lower, GENERIC_PHRASES)
        g_total = sum(generic.values())
        g_rate = g_total / max(1, words) * 1000
        if g_total >= 3 and g_rate >= self.t["generic_phrase_per_1000_words"]:
            pen = 0.25 if g_rate >= 2 * self.t["generic_phrase_per_1000_words"] else 0.15
            top = ", ".join(f"'{p}'x{n}" for p, n in generic.most_common(4))
            sig("originality", "generic_language", pen, f"{g_total} generic phrases ({g_rate:.1f}/1000 words): {top}",
                "Unusually generic, boilerplate phrasing.")

        toks = [t for t in tokenize(text) if t not in STOPWORDS and len(t) > 2 and not t.isdigit()]
        if len(toks) >= 150:
            grams = Counter(" ".join(toks[i:i + 2]) for i in range(len(toks) - 1))
            phrase, n = grams.most_common(1)[0]
            density = n * 2 / len(toks)
            if n >= 6 and density >= self.t["keyword_phrase_density"]:
                sig("originality", "repeated_keyword_phrase", 0.2,
                    f"'{phrase}' repeated {n} times ({density:.1%} of content words)",
                    f"Phrase '{phrase}' is repeated unusually often.")
            window = toks[:800]
            ttr = len(set(window)) / len(window)
            if ttr < 0.35:
                sig("originality", "low_vocabulary_variety", 0.1, f"type-token ratio {ttr:.2f} (first 800 content words)",
                    "Repetitive vocabulary.")

        # ------------------------------------------------------------ concrete-detail metrics (used by B & D)
        numbers = len(NUMBER_RE.findall(text))
        units = len(UNIT_RE.findall(text))
        proper = len(re.findall(r"(?<=[a-z,;:] )[A-Z][a-zA-Z0-9]+(?:\s[A-Z][a-zA-Z0-9]+)*", text))
        examples = len(EXAMPLE_MARKERS.findall(text))
        concrete = numbers + units + proper + examples * 2 + article.code_block_count * 3 + article.table_count * 3
        concrete_rate = concrete / max(1, words) * 100

        # ------------------------------------------------------------ B. depth
        ratio = words / max(1, profile["expected_words"])
        short_pen = 0.0
        if ratio < 0.35:
            short_pen = 0.55
        elif ratio < 0.6:
            short_pen = 0.3
        elif ratio < 0.85:
            short_pen = 0.1
        if short_pen:
            if concrete_rate >= 2 * self.t["min_concrete_details_per_100_words"] and article.external_link_count >= 1:
                short_pen /= 2  # short but specific and sourced: may well satisfy its purpose
                note = " (halved: short but specific and sourced)"
            else:
                note = ""
            sig("depth", "short_for_article_type", short_pen,
                f"{words} words vs ~{profile['expected_words']} typical for {atype}{note}",
                "Very little substantive content." if ratio < 0.35 else "Shorter than typical for this article type.")
        elif words >= 1.5 * profile["expected_words"]:
            strengths.append(f"{words} words (substantial for {atype})")

        sections = _sections(article)
        if len(sections) >= 3:
            thin = [h for h, w in sections if w < self.t["thin_section_words"]]
            if len(thin) / len(sections) >= 0.5:
                sig("depth", "thin_sections", 0.3,
                    f"{len(thin)}/{len(sections)} sections have <{self.t['thin_section_words']} words",
                    "Many headings with almost no substantive text.")
        distinct_terms = len({t for t in toks if len(t) >= 5})
        if words >= 300 and distinct_terms < 0.12 * words:
            sig("depth", "limited_terminology", 0.15,
                f"{distinct_terms} distinct substantive terms in {words} words",
                "Little subject-specific terminology.")
        if keywords:
            body_stems = Counter(tokenize(lower))
            for kw in keywords:
                kw_words = [w for w in tokenize(kw) if w not in STOPWORDS]
                if kw_words and kw.lower() in (article.title or "").lower():
                    mentions = min(body_stems.get(w, 0) for w in kw_words)
                    if mentions <= 1 and words >= 300:
                        sig("depth", "topic_barely_covered", 0.15,
                            f"title targets '{kw}' but body mentions it {mentions} time(s)",
                            f"Title targets '{kw}' but the body barely covers it.")
                    break

        # ------------------------------------------------------------ C. evidence (scaled by type)
        imp = float(profile["evidence_importance"])
        ext = article.external_link_count
        markers = len(EVIDENCE_MARKERS.findall(text))
        vague = _count_phrases(lower, VAGUE_CLAIMS)
        vague_n = sum(vague.values())
        if ext == 0 and words >= 250:
            sig("evidence", "no_external_sources", 0.5 * imp,
                f"0 outbound links to other sites (evidence importance for {atype}: {imp})",
                "No external sources linked.")
        elif ext <= 2 and words >= 900:
            sig("evidence", "few_external_sources", 0.2 * imp, f"{ext} outbound link(s) in {words} words",
                "Few external sources for its length.")
        elif ext >= 5:
            strengths.append(f"{ext} external sources linked")
        if vague_n >= 2 and ext < vague_n:
            sig("evidence", "unsupported_broad_claims", min(0.4, 0.15 * vague_n) * max(imp, 0.5),
                f"{vague_n} broad claims ({', '.join(vague)}) with {ext} outbound link(s)",
                "Broad claims with limited evidence.")
        if markers + units == 0 and words >= 300:
            sig("evidence", "no_attributed_facts_or_data", 0.3 * imp,
                "no data points, quotes or attributions detected",
                "Little data, quotes or attributed facts.")
        elif markers >= 5:
            strengths.append(f"{markers} attribution/data markers")

        # ------------------------------------------------------------ D. usefulness / specificity
        min_rate = self.t["min_concrete_details_per_100_words"]
        if concrete_rate < min_rate / 2:
            sig("usefulness", "very_few_specifics", 0.55,
                f"{concrete_rate:.1f} concrete details per 100 words (numbers, names, examples, code)",
                "Few specific examples or supporting details.")
        elif concrete_rate < min_rate:
            sig("usefulness", "few_specifics", 0.3, f"{concrete_rate:.1f} concrete details per 100 words",
                "Few specific examples or supporting details.")
        elif concrete_rate >= 3 * min_rate:
            strengths.append(f"{concrete_rate:.1f} concrete details per 100 words")
        if examples == 0 and words >= 500 and atype in ("tutorial", "listicle", "general", "review"):
            sig("usefulness", "no_examples", 0.15, "no example markers ('for example', 'such as', case study...)",
                "No concrete examples.")
        vw = _count_phrases(lower, VAGUE_WORDS)
        vw_rate = sum(vw.values()) / max(1, words) * 100
        if vw_rate >= self.t["vague_words_per_100_words"]:
            sig("usefulness", "vague_wording", 0.2,
                f"{vw_rate:.1f} vague words per 100 words ({', '.join(p for p, _ in vw.most_common(4))})",
                "Vague wording instead of specifics.")
        if atype == "tutorial" and not (article.ordered_list_count or article.code_block_count
                                        or re.search(r"\bstep\s*\d|^\d+[.)]", lower, re.M)):
            sig("usefulness", "tutorial_without_steps", 0.25, "no numbered steps, ordered lists or code blocks",
                "Tutorial without clear steps.")
        if atype == "listicle":
            m = re.search(r"\b(\d{1,3})\b", article.title or "")
            if m:
                promised = int(m.group(1))
                delivered = max(len([h for h in article.headings if h[0] >= 2]), len(article.list_items))
                if 3 <= promised <= 200 and delivered < promised * 0.8:
                    sig("usefulness", "list_shorter_than_title", 0.2,
                        f"title promises {promised} items, found {delivered} headings/list items",
                        "Fewer list items than the title promises.")
        if atype == "review" and len(REVIEW_MARKERS.findall(text)) < 2:
            sig("usefulness", "review_without_specifics", 0.2, "no pros/cons, pricing, ratings or testing details",
                "Review lacks pros/cons, pricing or testing detail.")

        # ------------------------------------------------------------ E. structure & readability
        sents = _sentences(text)
        if sents:
            avg_sent = sum(len(s.split()) for s in sents) / len(sents)
            if avg_sent > self.t["max_avg_sentence_words"]:
                sig("readability", "long_sentences", 0.3, f"average sentence length {avg_sent:.0f} words",
                    "Long, hard-to-read sentences.")
        if article.paragraphs:
            avg_para = sum(len(p.split()) for p in article.paragraphs) / len(article.paragraphs)
            if avg_para > self.t["max_avg_paragraph_words"]:
                sig("readability", "wall_of_text", 0.25, f"average paragraph length {avg_para:.0f} words",
                    "Long unbroken paragraphs.")
            if avg_para < 12 and len(article.paragraphs) > 25:
                sig("readability", "fragmented_paragraphs", 0.1,
                    f"{len(article.paragraphs)} paragraphs averaging {avg_para:.0f} words",
                    "Very fragmented, choppy structure.")
        subheads = [h for h in article.headings if h[0] >= 2]
        if words >= 700 and not subheads:
            sig("readability", "no_subheadings", 0.35, f"{words} words with no subheadings",
                "Long article with no subheadings.")
        levels = [lvl for lvl, _ in article.headings]
        skips = sum(1 for a, b in zip(levels, levels[1:]) if b - a > 1)
        if skips >= 2 or levels.count(1) > 1:
            sig("readability", "disorganized_headings", 0.1,
                f"{skips} heading-level skips, {levels.count(1)} H1 headings",
                "Disorganized heading structure.")
        fre = flesch_reading_ease(text)
        if fre is not None and fre < 25:
            sig("readability", "low_readability", 0.2, f"Flesch reading ease {fre:.0f}",
                "Dense, hard-to-read text.")

        # ------------------------------------------------------------ totals
        crit_scores = {}
        for c in CRITERIA:
            weight = float(self.weights.get(c, 0))
            cs = [s for s in signals if s.criterion == c]
            total_pen = min(1.0, sum(s.penalty for s in cs))
            # Distribute lost points proportionally so each signal shows its exact cost.
            raw = sum(s.penalty for s in cs) or 1
            for s in cs:
                s.points = round(weight * total_pen * s.penalty / raw, 1)
            crit_scores[c] = round(weight * (1 - total_pen), 1)
        score = round(sum(crit_scores.values()), 1)
        category = self.categorize(score)

        notes = []
        criteria_hit = {s.criterion for s in signals if s.points >= 1}
        if score < self._lowest_band_ceiling() and len(criteria_hit) < 2:
            notes.append("score is in the lowest band but problems come from a single criterion; "
                         "shown as 'Needs manual review' because one signal alone is not proof of low quality")
            category = self._review_label()

        signals.sort(key=lambda s: -s.points)
        explanation_parts = list(dict.fromkeys(s.explanation for s in signals if s.points >= 0.5))[:4]
        explanation = " ".join(explanation_parts) if explanation_parts else "No significant problem signals detected."
        return QualityResult(score, category, crit_scores, signals, strengths, atype, explanation, notes)

    def _lowest_band_ceiling(self) -> float:
        mins = sorted(c["min"] for c in self.categories)
        return mins[1] if len(mins) > 1 else 0

    def _review_label(self) -> str:
        ordered = sorted(self.categories, key=lambda c: c["min"])
        return ordered[1]["label"] if len(ordered) > 1 else ordered[0]["label"]


def check_explanations_match_signals(result: QualityResult) -> bool:
    """Every sentence in the explanation must come from a detected signal (used in tests)."""
    if result.score is None or not result.signals:
        return True
    allowed = {s.explanation for s in result.signals}
    return all(part in allowed for part in _split_explanation(result.explanation, allowed))


def _split_explanation(text: str, allowed: set[str]) -> list[str]:
    found, rest = [], text
    for a in sorted(allowed, key=len, reverse=True):
        if a in rest:
            found.append(a)
            rest = rest.replace(a, "")
    if rest.strip():
        found.append(rest.strip())
    return found
