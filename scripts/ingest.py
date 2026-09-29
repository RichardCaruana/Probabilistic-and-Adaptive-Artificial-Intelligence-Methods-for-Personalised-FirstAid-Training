"""
Run once (and again any time corpus files change):

    python scripts/ingest.py

Builds the TF-IDF retrieval index used by app/rag.py from data/corpus/*.txt.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import rag

if __name__ == "__main__":
    n_chunks = rag.build_index()
    print(f"Indexed {n_chunks} passages across {len(rag.SKILLS)} skills.")
    print(f"Index written to {rag.INDEX_PATH}")
