"""Text similarity between collected articles.

Default: TF-IDF + cosine similarity (scikit-learn, transparent and fast).
Optional: semantic embeddings via sentence-transformers if installed locally.
Fallback: word-shingle Jaccard similarity (pure Python) if scikit-learn is missing.

Similarity is measured only against articles this tool has collected - it says
nothing about originality relative to the rest of the internet.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)


def _shingles(text: str, n: int = 5) -> set[str]:
    words = re.findall(r"\w+", text.lower())
    return {" ".join(words[i:i + n]) for i in range(max(0, len(words) - n + 1))}


def _jaccard_matrix(texts: list[str]) -> list[list[float]]:
    sh = [_shingles(t) for t in texts]
    m = [[0.0] * len(texts) for _ in texts]
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            if sh[i] and sh[j]:
                v = len(sh[i] & sh[j]) / len(sh[i] | sh[j])
                m[i][j] = m[j][i] = v
    return m


def _tfidf_matrix(texts: list[str]):
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity

    vec = TfidfVectorizer(ngram_range=(1, 2), stop_words="english", sublinear_tf=True, min_df=1, max_features=50000)
    tfidf = vec.fit_transform(texts)
    return cosine_similarity(tfidf).tolist()


def _semantic_matrix(texts: list[str]):
    from sentence_transformers import SentenceTransformer  # optional dependency
    from sklearn.metrics.pairwise import cosine_similarity

    model = SentenceTransformer("all-MiniLM-L6-v2")
    emb = model.encode([t[:5000] for t in texts], normalize_embeddings=True)
    return cosine_similarity(emb).tolist()


def similarity_matrix(texts: list[str], method: str = "tfidf") -> tuple[list[list[float]], str]:
    if len(texts) < 2:
        return [[0.0] * len(texts) for _ in texts], method
    if method == "semantic":
        try:
            return _semantic_matrix(texts), "semantic"
        except Exception as exc:  # noqa: BLE001 - optional package may be missing
            log.warning("semantic similarity unavailable (%s); using TF-IDF", exc)
    try:
        return _tfidf_matrix(texts), "tfidf"
    except Exception as exc:  # noqa: BLE001
        log.warning("TF-IDF unavailable (%s); using shingle Jaccard", exc)
        return _jaccard_matrix(texts), "jaccard"


def max_similarity(
    texts: list[str], labels: list[str], reference_texts: list[str] | None = None,
    reference_labels: list[str] | None = None, method: str = "tfidf",
) -> list[tuple[float, str]]:
    """For each text, the highest similarity to any *other* collected text, with its label (URL).

    Texts sharing the same label (same URL) are never compared with each other.
    """
    reference_texts = reference_texts or []
    reference_labels = reference_labels or []
    all_texts = list(texts) + list(reference_texts)
    all_labels = list(labels) + list(reference_labels)
    nonempty = [i for i, t in enumerate(all_texts) if t and t.strip()]
    if len(nonempty) < 2:
        return [(0.0, "") for _ in texts]
    matrix, _ = similarity_matrix([all_texts[i] for i in nonempty], method)
    pos = {orig: k for k, orig in enumerate(nonempty)}
    out = []
    for i in range(len(texts)):
        if i not in pos:
            out.append((0.0, ""))
            continue
        best, best_label = 0.0, ""
        row = matrix[pos[i]]
        for j_orig, k in pos.items():
            if j_orig == i or all_labels[j_orig] == labels[i]:
                continue
            if row[k] > best:
                best, best_label = float(row[k]), all_labels[j_orig]
        out.append((round(best, 3), best_label))
    return out
