from scanner.extractor import extract_article
from scanner.quality_scoring import NOT_SCORED, QualityScorer, check_explanations_match_signals, detect_article_type
from scanner.similarity import max_similarity

from .conftest import fixture_bytes


def art(name, url="https://x.com/blog/some-post"):
    return extract_article(fixture_bytes(name), url)


def test_good_article_scores_high_and_thin_article_scores_low():
    scorer = QualityScorer()
    good = scorer.score(art("article_good.html"), keywords=["technical seo"])
    thin = scorer.score(art("article_thin.html"), keywords=["technical seo"])
    assert good.score >= 75, good.signals_text
    assert thin.score < 40, thin.signals_text
    assert thin.category == "Potentially very low quality"
    assert good.score - thin.score > 35


def test_thin_article_signals_are_specific_and_explained():
    r = QualityScorer().score(art("article_thin.html"), keywords=["technical seo"])
    flags = set(r.flags)
    assert {"generic_language", "repetitive_paragraphs", "repeated_headings", "unsupported_broad_claims",
            "no_external_sources", "thin_sections"} <= flags
    assert "Multiple repetitive sections." in r.explanation
    assert check_explanations_match_signals(r)
    # every lost point is attributed to a signal
    for c, score in r.criterion_scores.items():
        lost = sum(s.points for s in r.signals if s.criterion == c)
        weight = QualityScorer().weights[c]
        assert abs((weight - score) - lost) < 0.5, c


def test_never_labels_ai_generated():
    r = QualityScorer().score(art("article_thin.html"))
    text = (r.explanation + r.signals_text).lower()
    assert "ai-generated" not in text and "ai generated" not in text and "chatgpt" not in text


def test_short_news_article_not_penalised_for_length_alone():
    r = QualityScorer().score(art("article_news_short.html"))
    assert r.article_type == "news"
    assert r.score >= 60, r.signals_text
    short = [s for s in r.signals if s.code == "short_for_article_type"]
    assert not short or short[0].points < 5


def test_evidence_importance_depends_on_type():
    a = art("article_thin.html")
    general = QualityScorer().score(a)
    opinion_cfg = {"article_types": {"tutorial": {"expected_words": 700, "evidence_importance": 0.1}}}
    relaxed = QualityScorer(opinion_cfg).score(a)
    assert relaxed.criterion_scores["evidence"] > general.criterion_scores["evidence"]


def test_single_criterion_problems_capped_at_manual_review():
    scorer = QualityScorer({"weights": {"originality": 90, "depth": 2, "evidence": 2, "usefulness": 3,
                                        "readability": 3},
                            "categories": [{"min": 80, "label": "Strong quality signals"},
                                           {"min": 60, "label": "Reasonable quality"},
                                           {"min": 50, "label": "Needs manual review"},
                                           {"min": 0, "label": "Potentially very low quality"}]})
    a = art("article_good.html")
    r = scorer.score(a, similarity=(0.95, "https://copy.com/x"))
    assert r.score < 50
    assert {s.criterion for s in r.signals if s.points >= 1} == {"originality"}
    assert r.category == "Needs manual review" and r.notes


def test_weights_configurable_and_scores_sum():
    r = QualityScorer({"weights": {"originality": 20, "depth": 20, "evidence": 20, "usefulness": 20,
                                   "readability": 20}}).score(art("article_good.html"))
    assert abs(sum(r.criterion_scores.values()) - r.score) < 0.2
    assert all(v <= 20 for v in r.criterion_scores.values())


def test_insufficient_text_is_not_scored():
    r = QualityScorer().score(art("article_js.html"))
    assert r.score is None and r.category == NOT_SCORED


def test_article_type_detection():
    assert detect_article_type(art("article_news_short.html")) == "news"
    assert detect_article_type(art("article_good.html")) == "tutorial"
    a = art("article_thin.html")
    a.title = "10 Best Technical SEO Tools"
    assert detect_article_type(a) == "listicle"


def test_listicle_promising_more_items_than_delivered():
    a = art("article_thin.html")
    a.title = "25 Technical SEO Tips"
    r = QualityScorer().score(a)
    assert "list_shorter_than_title" in r.flags


def test_similarity_detects_near_duplicates_among_collected_articles():
    good = art("article_good.html").body_text
    dup = art("article_duplicate.html").body_text
    other = art("article_conflicting_dates.html").body_text
    sims = max_similarity([good, dup, other], ["u1", "u2", "u3"])
    assert sims[0][0] > 0.6 and sims[0][1] == "u2"
    assert sims[2][0] < 0.3
    r = QualityScorer().score(art("article_duplicate.html"), similarity=sims[1])
    assert any(f in r.flags for f in ("near_duplicate_of_collected_article", "highly_similar_to_collected_article"))


def test_similarity_fallback_without_sklearn(monkeypatch):
    import scanner.similarity as sim

    def boom(texts):
        raise ImportError("no sklearn")

    monkeypatch.setattr(sim, "_tfidf_matrix", boom)
    matrix, method = sim.similarity_matrix(["a b c d e f g h", "a b c d e f g h", "x y z"])
    assert method == "jaccard" and matrix[0][1] == 1.0
