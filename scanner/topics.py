"""Lightweight keyword/topic expansion.

This is NOT keyword research: no search volume, difficulty, ranking, traffic or
intent data is produced or estimated. It only generates query variations so that
discovery covers more phrasings of your topics. Every query is labelled with its
origin so user-supplied keywords are never confused with generated ones.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import yaml

STOPWORDS = set("""
a about above after again against all am an and any are as at be because been before being below
between both but by can could did do does doing down during each few for from further had has have
having he her here hers herself him himself his how i if in into is it its itself just me more most
my myself no nor not now of off on once only or other our ours ourselves out over own same she should
so some such than that the their theirs them themselves then there these they this those through to
too under until up very was we were what when where which while who whom why will with would you your
yours yourself yourselves new best top guide tips how-to vs using use get make
""".split())

ORIGIN_LABELS = {
    "user": "user keyword",
    "synonym": "generated: synonym",
    "combination": "generated: word combination",
    "variation": "generated: template variation",
    "mined_title": "generated: phrase seen in discovered titles",
}


@dataclass(frozen=True)
class Query:
    text: str
    origin: str  # user | synonym | combination | variation | mined_title
    user_keyword: str

    @property
    def is_user_supplied(self) -> bool:
        return self.origin == "user"

    @property
    def origin_label(self) -> str:
        return ORIGIN_LABELS.get(self.origin, self.origin)


def tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9][a-z0-9'+#.-]*[a-z0-9+#]|[a-z0-9]", (text or "").lower())


def _stem(word: str) -> str:
    for suffix in ("ies", "es", "s"):
        if len(word) > 4 and word.endswith(suffix):
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def load_synonyms(path: str | Path | None) -> dict[str, list[str]]:
    if not path:
        return {}
    p = Path(path)
    if not p.is_absolute():
        p = Path(__file__).resolve().parent.parent / p
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return {str(k).lower(): [str(v).lower() for v in (vals or [])] for k, vals in data.items()}


def expand_keywords(
    keywords: list[str],
    topic_config: dict | None = None,
    titles: list[str] | None = None,
    synonyms: dict[str, list[str]] | None = None,
) -> list[Query]:
    cfg = topic_config or {}
    max_generated = int(cfg.get("max_generated_queries", 20))
    clean = [re.sub(r"\s+", " ", k).strip().lower() for k in keywords if k and k.strip()]
    clean = list(dict.fromkeys(clean))
    queries: list[Query] = [Query(k, "user", k) for k in clean]
    generated: list[Query] = []

    if synonyms is None:
        synonyms = load_synonyms(cfg.get("synonyms_file"))
    for kw in clean:
        for term, alts in synonyms.items():
            if re.search(rf"\b{re.escape(term)}\b", kw):
                for alt in alts:
                    generated.append(Query(re.sub(rf"\b{re.escape(term)}\b", alt, kw), "synonym", kw))

    if cfg.get("use_word_combinations", True):
        for kw in clean:
            words = kw.split()
            if len(words) < 3:
                continue
            for n in range(len(words) - 1, 1, -1):
                for i in range(len(words) - n + 1):
                    sub = words[i:i + n]
                    if sum(w not in STOPWORDS for w in sub) >= 2:
                        generated.append(Query(" ".join(sub), "combination", kw))

    for kw in clean:
        for template in cfg.get("variation_templates", []) or []:
            if "{kw}" in template:
                generated.append(Query(template.replace("{kw}", kw), "variation", kw))

    if titles and cfg.get("mine_titles", True):
        generated.extend(mine_title_phrases(titles, clean))

    seen = {q.text for q in queries}
    for q in generated:
        if q.text not in seen and len([g for g in queries if not g.is_user_supplied]) < max_generated:
            seen.add(q.text)
            queries.append(q)
    return queries


def mine_title_phrases(titles: list[str], keywords: list[str], min_count: int = 2, limit: int = 10) -> list[Query]:
    """Find 2-3 word phrases that repeatedly co-occur with a keyword term in discovered titles."""
    kw_terms = {kw: {_stem(w) for w in kw.split() if w not in STOPWORDS} for kw in keywords}
    counts: Counter = Counter()
    owner: dict[str, str] = {}
    for title in titles:
        toks = tokenize(title)
        for n in (2, 3):
            for i in range(len(toks) - n + 1):
                gram = toks[i:i + n]
                if gram[0] in STOPWORDS or gram[-1] in STOPWORDS or any(t.isdigit() for t in gram):
                    continue
                stems = {_stem(t) for t in gram}
                for kw, terms in kw_terms.items():
                    if terms & stems and " ".join(gram) != kw:
                        phrase = " ".join(gram)
                        counts[phrase] += 1
                        owner.setdefault(phrase, kw)
    return [Query(p, "mined_title", owner[p]) for p, c in counts.most_common(limit) if c >= min_count]


def match_keywords(text: str, queries: list[Query]) -> list[Query]:
    """Queries whose meaningful words all appear in the text (light plural stemming)."""
    stems = {_stem(t) for t in tokenize(text)}
    matched = []
    for q in queries:
        words = [_stem(w) for w in tokenize(q.text) if w not in STOPWORDS]
        if words and all(w in stems for w in words):
            matched.append(q)
    return matched
