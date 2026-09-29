"""
evaluation/export_review_sample.py

Builds a blinded, content-only sample of AI-generated MCQs and Patient
Simulator scenarios for expert (instructor) content-quality review.

So this script deliberately produces a REDUCED, BLINDED view of a sample
of already-generated items:

  KEPT (shown to instructors):
    - skill name, difficulty, modality (mcq / simulation)
    - the scenario / patient_intro
    - the question / steps (each step's action options)
    - which option/action the system marked correct
    - the explanation / outcome narration
    - the source citation (MCQs only)

  STRIPPED (never shown to instructors -- would bias an "independent" check):
    - confidence_score, safety_flag, safety_reason, citation_overlap,
      retrieval_score, review_status, review_reason
      (these are the system's OWN automated judgement of the content --
      showing them defeats the purpose of an independent expert check)
    - learner_id, is_correct, chosen_index, response_time_ms, timestamps
      (irrelevant to a content-quality judgement, and unnecessarily
      exposes participant data to a rater)

A companion answer_key.csv keeps the mapping from each blinded item back
to its real database row AND the system's own signals. This file is for
YOUR analysis only (e.g. correlating instructor-judged accuracy against
the system's own confidence_score is a genuinely interesting result) --
it must never be shared with the raters themselves.

USAGE
-----
Run from the project root, with your normal venv active:

    python evaluation/export_review_sample.py --n-per-skill 2 --seed 42

--n-per-skill controls how many MCQs are drawn per skill (simulations are
sampled at roughly half that rate per skill, since there are usually far
fewer of them). Increase it if you want a larger rating sample; keep the
--seed fixed if you want the sample to be reproducible from your write-up.

OUTPUT
------
Two files under evaluation/output/ (created if missing):

  - review_sample.json   Blinded item bank. Safe to share / build a rating
                          sheet from. This is what you hand to instructors.
  - answer_key.csv        blinded_id -> real id + system signals.
                          RESEARCHER ONLY. Do not share with raters.

Both instructors should rate the SAME review_sample.json (two independent
copies of the same items), so you can report inter-rater agreement.
"""
from __future__ import annotations
import argparse
import csv
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import db, adaptive_engine

OUTPUT_DIR = Path(__file__).parent / "output"


def _mcq_items() -> list[dict]:
    """Real, non-empty, quick-question MCQs only -- excludes the per-step
    interaction rows logged for Patient Simulator decisions (modality =
    'simulation'), which are a different content unit handled separately
    via the simulations table below."""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM interactions "
            "WHERE modality = 'mcq' AND insufficient_context = 0 "
            "AND question IS NOT NULL AND question != '' "
            "ORDER BY skill_id, id"
        ).fetchall()
    return [dict(r) for r in rows]


def _simulation_items() -> list[dict]:
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM simulations "
            "WHERE steps_json IS NOT NULL AND steps_json != '' "
            "ORDER BY skill_id, id"
        ).fetchall()
    return [dict(r) for r in rows]


def _stratified_sample(items: list[dict], n_per_skill: int, rng: random.Random) -> list[dict]:
    """Draw up to n_per_skill items per skill, so the rating sample covers
    the taxonomy rather than clustering on whichever skills were asked
    about most often during piloting."""
    by_skill: dict[int, list[dict]] = {}
    for item in items:
        by_skill.setdefault(item["skill_id"], []).append(item)
    sampled = []
    for skill_id, group in by_skill.items():
        rng.shuffle(group)
        sampled.extend(group[:n_per_skill])
    return sampled


def _blind_mcq(item: dict, blinded_id: str) -> tuple[dict, dict]:
    options = json.loads(item["options_json"])
    public = {
        "blinded_id": blinded_id,
        "modality": "mcq",
        "skill_name": adaptive_engine.skill_name(item["skill_id"]),
        "difficulty": item["difficulty"],
        "scenario": item["scenario"],
        "question": item["question"],
        "options": options,
        "marked_correct_index": item["correct_index"],
        "explanation": item["explanation"],
        "source_citation": item.get("citation") or "",
    }
    private = {
        "blinded_id": blinded_id,
        "modality": "mcq",
        "interaction_id": item["id"],
        "simulation_id": "",
        "skill_id": item["skill_id"],
        "difficulty": item["difficulty"],
        "confidence_score": item.get("confidence_score"),
        "safety_flag": item.get("safety_flag"),
        "safety_reason": item.get("safety_reason"),
        "retrieval_score": item.get("retrieval_score"),
        "review_status": item.get("review_status"),
    }
    return public, private


def _blind_simulation(item: dict, blinded_id: str) -> tuple[dict, dict]:
    steps = json.loads(item["steps_json"])
    public = {
        "blinded_id": blinded_id,
        "modality": "simulation",
        "skill_name": adaptive_engine.skill_name(item["skill_id"]),
        "difficulty": item["difficulty"],
        "patient_intro": item["patient_intro"],
        "steps": steps,
        "resolution": item.get("resolution") or "",
    }
    private = {
        "blinded_id": blinded_id,
        "modality": "simulation",
        "interaction_id": "",
        "simulation_id": item["id"],
        "skill_id": item["skill_id"],
        "difficulty": item["difficulty"],
        "confidence_score": item.get("confidence_score"),
        "safety_flag": item.get("safety_flag"),
        "safety_reason": item.get("safety_reason"),
        "retrieval_score": None,
        "review_status": item.get("review_status"),
    }
    return public, private


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n-per-skill", type=int, default=2,
                         help="MCQs to sample per skill (default 2)")
    parser.add_argument("--seed", type=int, default=42,
                         help="Random seed, for a reproducible sample")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    mcqs = _stratified_sample(_mcq_items(), args.n_per_skill, rng)
    sims = _stratified_sample(_simulation_items(), max(1, args.n_per_skill // 2), rng)

    if not mcqs and not sims:
        print("No eligible generated content found in data/app.db. "
              "Has the pilot been run yet, and does app.db contain real "
              "generated interactions (not just data/test_harness.db)?")
        return

    all_items = [("mcq", m) for m in mcqs] + [("sim", s) for s in sims]
    rng.shuffle(all_items)  # blinded order shouldn't cluster by skill/modality

    public_bank, private_bank = [], []
    for i, (kind, item) in enumerate(all_items, start=1):
        blinded_id = f"ITEM-{i:03d}"
        if kind == "mcq":
            public, private = _blind_mcq(item, blinded_id)
        else:
            public, private = _blind_simulation(item, blinded_id)
        public_bank.append(public)
        private_bank.append(private)

    (OUTPUT_DIR / "review_sample.json").write_text(json.dumps(public_bank, indent=2))

    with open(OUTPUT_DIR / "answer_key.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "blinded_id", "modality", "interaction_id", "simulation_id", "skill_id",
            "difficulty", "confidence_score", "safety_flag", "safety_reason",
            "retrieval_score", "review_status",
        ])
        writer.writeheader()
        for row in private_bank:
            writer.writerow(row)

    print(f"Sampled {len(public_bank)} items ({len(mcqs)} MCQs, {len(sims)} simulations) "
          f"across {len({i['skill_id'] for i in mcqs + sims})} skills.")
    print(f"Blinded item bank (share with instructors): {OUTPUT_DIR / 'review_sample.json'}")
    print(f"Answer key (RESEARCHER ONLY, do not share): {OUTPUT_DIR / 'answer_key.csv'}")


if __name__ == "__main__":
    main()
