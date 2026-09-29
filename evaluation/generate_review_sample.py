"""
evaluation/generate_review_sample.py

Generates a FRESH, systematic sample of AI content directly from the real
generation pipeline (app/rag.py + app/llm.py), for instructor
content-quality rating -- rather than sampling whatever content real
learners happened to be served during piloting.

This calls the real app/rag.py + app/llm.py pipeline directly, so it
needs ANTHROPIC_API_KEY set exactly as the running app does, and it makes
one real API call per generated item (roughly 17-25 calls with the
defaults below). Generated items are NOT written to data/app.db -- they
exist only in this script's output, so this evaluation never touches or
risks polluting your real pilot data or instructor dashboard.

As with export_review_sample.py (the alternative, pilot-sampling version
kept alongside this script), the blinded item bank strips the system's
own quality signals (confidence_score, safety_flag, retrieval_score) so
instructors judge content on its own merits, not anchored by the
system's opinion of it. Those signals are kept in the researcher-only
answer_key.csv for later analysis.

USAGE
-----
    export ANTHROPIC_API_KEY=sk-ant-...
    python evaluation/generate_review_sample.py

OUTPUT (under evaluation/output/):
  - review_sample.json   Blinded item bank. Share with instructors /
                          feed into the rating-sheet builder.
  - answer_key.csv        blinded_id -> skill/difficulty/system signals.
                           RESEARCHER ONLY -- never share with raters.
"""
from __future__ import annotations
import argparse
import csv
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")  # picks up ANTHROPIC_API_KEY the same way app/main.py does

from app import rag, llm, adaptive_engine

OUTPUT_DIR = Path(__file__).parent / "output"
DEFAULT_DIFFICULTIES = ["easy", "medium", "hard"]


def _generate_mcq(skill_id: int, difficulty: str):
    skill_name = adaptive_engine.skill_name(skill_id)
    query = f"{skill_name} {difficulty} scenario"
    retrieval = rag.retrieve(skill_id, query)
    if not retrieval["passages"] or retrieval["insufficient_context"]:
        return None
    generated = llm.generate_scenario_question(
        skill_name=skill_name, difficulty=difficulty,
        passages=retrieval["passages"], profession=None, is_review=False,
        retrieval_score=retrieval["top_score"],
    )
    if generated.get("insufficient_context"):
        return None
    return generated, retrieval["top_score"]


def _generate_sim(skill_id: int, difficulty: str):
    skill_name = adaptive_engine.skill_name(skill_id)
    query = f"{skill_name} {difficulty} patient simulation"
    retrieval = rag.retrieve(skill_id, query)
    if not retrieval["passages"] or retrieval["insufficient_context"]:
        return None
    generated = llm.generate_simulation(
        skill_name=skill_name, difficulty=difficulty,
        passages=retrieval["passages"], profession=None,
        retrieval_score=retrieval["top_score"],
    )
    if generated.get("insufficient_context"):
        return None
    return generated, retrieval["top_score"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--difficulties", nargs="+", default=DEFAULT_DIFFICULTIES)
    parser.add_argument("--sim-every-n-skills", type=int, default=3,
                         help="Generate one simulation for every Nth skill (default 3)")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    skill_ids = adaptive_engine.all_skill_ids()
    rng.shuffle(skill_ids)

    public_bank, private_bank = [], []
    counter = 1

    for i, skill_id in enumerate(skill_ids):
        difficulty = args.difficulties[i % len(args.difficulties)]

        result = _generate_mcq(skill_id, difficulty)
        if result is None:
            print(f"[skip] MCQ  skill={skill_id} ({adaptive_engine.skill_name(skill_id)}) "
                  f"difficulty={difficulty}: insufficient context or no passages")
        else:
            generated, retrieval_score = result
            blinded_id = f"ITEM-{counter:03d}"
            counter += 1
            public_bank.append({
                "blinded_id": blinded_id,
                "modality": "mcq",
                "skill_name": adaptive_engine.skill_name(skill_id),
                "difficulty": difficulty,
                "scenario": generated["scenario"],
                "question": generated["question"],
                "options": generated["options"],
                "marked_correct_index": generated["correct_index"],
                "explanation": generated["explanation"],
                "source_citation": generated.get("source_citation", ""),
            })
            private_bank.append({
                "blinded_id": blinded_id, "modality": "mcq",
                "skill_id": skill_id, "difficulty": difficulty,
                "confidence_score": generated.get("confidence_score"),
                "safety_flag": generated.get("safety_flag"),
                "safety_reason": generated.get("safety_reason"),
                "retrieval_score": retrieval_score,
            })
        time.sleep(0.3)  # be polite to the API

        if i % args.sim_every_n_skills == 0:
            sim_result = _generate_sim(skill_id, difficulty)
            if sim_result is None:
                print(f"[skip] SIM  skill={skill_id} ({adaptive_engine.skill_name(skill_id)}) "
                      f"difficulty={difficulty}: insufficient context or no passages")
            else:
                generated, retrieval_score = sim_result
                blinded_id = f"ITEM-{counter:03d}"
                counter += 1
                public_bank.append({
                    "blinded_id": blinded_id,
                    "modality": "simulation",
                    "skill_name": adaptive_engine.skill_name(skill_id),
                    "difficulty": difficulty,
                    "patient_intro": generated["patient_intro"],
                    "steps": generated["steps"],
                    "resolution": generated.get("resolution", ""),
                })
                private_bank.append({
                    "blinded_id": blinded_id, "modality": "simulation",
                    "skill_id": skill_id, "difficulty": difficulty,
                    "confidence_score": generated.get("confidence_score"),
                    "safety_flag": generated.get("safety_flag"),
                    "safety_reason": generated.get("safety_reason"),
                    "retrieval_score": retrieval_score,
                })
            time.sleep(0.3)

    if not public_bank:
        print("No items were generated -- check ANTHROPIC_API_KEY is set and "
              "data/index/tfidf_index.pkl exists (run scripts/ingest.py first if not).")
        return

    rng.shuffle(public_bank)  # display order shouldn't cluster by skill/modality
    (OUTPUT_DIR / "review_sample.json").write_text(json.dumps(public_bank, indent=2))

    with open(OUTPUT_DIR / "answer_key.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "blinded_id", "modality", "skill_id", "difficulty",
            "confidence_score", "safety_flag", "safety_reason", "retrieval_score",
        ])
        writer.writeheader()
        for row in private_bank:
            writer.writerow(row)

    n_mcq = sum(1 for p in public_bank if p["modality"] == "mcq")
    n_sim = len(public_bank) - n_mcq
    print(f"\nGenerated {len(public_bank)} items ({n_mcq} MCQs, {n_sim} simulations) "
          f"across {len({p['skill_name'] for p in public_bank})} skills.")
    print(f"Blinded item bank (share with instructors): {OUTPUT_DIR / 'review_sample.json'}")
    print(f"Answer key (RESEARCHER ONLY, do not share): {OUTPUT_DIR / 'answer_key.csv'}")


if __name__ == "__main__":
    main()
