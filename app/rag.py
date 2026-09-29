"""
Retrieval component of the Grounded Content Generation Framework (O3)
-----------------------------------------------------------------------
"""

from __future__ import annotations
import json
import pickle
import re
from pathlib import Path

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

DATA_DIR = Path(__file__).parent.parent / "data"
CORPUS_DIR = DATA_DIR / "corpus"
INDEX_PATH = DATA_DIR / "index" / "tfidf_index.pkl"
SKILLS = json.loads((DATA_DIR / "skills.json").read_text())

RELEVANCE_THRESHOLD = 0.08  # cosine similarity floor before broadening search

_CACHE = {}


def corpus_word_count(skill_id: int) -> int:
    """O3 corpus-coverage export: raw word count of a skill's corpus file, so
    thin grounding material can be correlated against insufficient_context
    generation failures for that skill."""
    skill = next((s for s in SKILLS if s["id"] == skill_id), None)
    if not skill:
        return 0
    path = CORPUS_DIR / skill["file"]
    return len(path.read_text().split()) if path.exists() else 0


def clean_text(raw: str) -> str:
    """Strip PPTX/PDF extraction artefacts: form-feed page breaks, copyright
    footers, bare slide/page numbers, stray bullet glyphs."""
    text = raw.replace("\f", "\n")
    text = re.sub(r"©\s*AOFAQ\s*\d*", "", text)
    text = re.sub(r"^\s*\d{1,3}\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*•\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunk_text(text: str, target_words: int = 120) -> list[str]:
    """Chunk a skill's cleaned text into ~target_words passages, splitting on
    blank lines first so we don't cut a bullet block in half where avoidable."""
    blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
    chunks, current, count = [], [], 0
    for block in blocks:
        words = block.split()
        current.append(block)
        count += len(words)
        if count >= target_words:
            chunks.append("\n".join(current))
            current, count = [], 0
    if current:
        chunks.append("\n".join(current))
    return chunks if chunks else [text]


def build_index():
    """Run once (via scripts/ingest.py) to (re)build the TF-IDF index from
    the corpus files. Persists chunk metadata + vectorizer + matrix."""
    chunks_meta = []
    raw_texts = []
    for skill in SKILLS:
        path = CORPUS_DIR / skill["file"]
        if not path.exists():
            continue
        cleaned = clean_text(path.read_text(errors="ignore"))
        for chunk in chunk_text(cleaned):
            chunks_meta.append({"skill_id": skill["id"], "unit": skill["unit"], "text": chunk})
            raw_texts.append(chunk)

    vectorizer = TfidfVectorizer(stop_words="english", max_df=0.9)
    matrix = vectorizer.fit_transform(raw_texts)

    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(INDEX_PATH, "wb") as f:
        pickle.dump({"vectorizer": vectorizer, "matrix": matrix, "chunks": chunks_meta}, f)

    return len(chunks_meta)


def _load_index():
    if "index" not in _CACHE:
        if not INDEX_PATH.exists():
            raise RuntimeError(
                "RAG index not built yet. Run: python scripts/ingest.py"
            )
        with open(INDEX_PATH, "rb") as f:
            _CACHE["index"] = pickle.load(f)
    return _CACHE["index"]


def retrieve(skill_id: int, query: str, top_k: int = 3) -> dict:
    """Retrieve top_k passages for skill_id given a query (typically the
    skill name + difficulty + any learner profession context, so scenario
    generation is grounded in the right material). Returns retrieved
    passages plus a CRAG-lite verdict.
    """
    idx = _load_index()
    vectorizer, matrix, chunks = idx["vectorizer"], idx["matrix"], idx["chunks"]

    query_vec = vectorizer.transform([query])
    sims = cosine_similarity(query_vec, matrix)[0]

    skill_positions = [i for i, c in enumerate(chunks) if c["skill_id"] == skill_id]
    if not skill_positions:
        return {"passages": [], "top_score": 0.0, "insufficient_context": True, "broadened": False}

    skill_scored = sorted(((sims[i], i) for i in skill_positions), reverse=True)
    top_score = skill_scored[0][0] if skill_scored else 0.0

    broadened = False
    if top_score < RELEVANCE_THRESHOLD:
        # CRAG-lite fallback: broaden to the rest of the same unit
        broadened = True
        unit = next((c["unit"] for c in chunks if c["skill_id"] == skill_id), None)
        unit_positions = [i for i, c in enumerate(chunks) if c["unit"] == unit]
        skill_scored = sorted(((sims[i], i) for i in unit_positions), reverse=True)
        top_score = skill_scored[0][0] if skill_scored else 0.0

    selected = skill_scored[:top_k]
    passages = [
        {"text": chunks[i]["text"], "score": float(score), "skill_id": chunks[i]["skill_id"]}
        for score, i in selected
    ]
    insufficient = top_score < RELEVANCE_THRESHOLD
    return {
        "passages": passages,
        "top_score": float(top_score),
        "insufficient_context": insufficient,
        "broadened": broadened,
    }
