"""
Black-box / grey-box test harness (Objective O4, pre-deployment QA phase)
---------------------------------------------------------------------------
Simulates learner interaction histories against the adaptive engine and
learner model directly (bypassing the live LLM call, so this runs without
an API key and in seconds). This is the "testing before live deployment"
step the report specifies: validating that state updates propagate
correctly through the probabilistic model and that question selection
responds correctly to them, before trusting the system with real trainees.

Run:
    python scripts/test_harness.py
"""

from __future__ import annotations
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import db, adaptive_engine, learner_model

random.seed(42)


def simulate_learner(name: str, n_turns: int, skill_bias: dict[int, float] | None = None):
    """skill_bias: optional {skill_id: p_correct} to simulate a learner who's
    genuinely weak/strong at specific skills, rather than uniformly random."""
    lid = db.create_learner(name, None)
    log = []
    for turn in range(1, n_turns + 1):
        skill_id, is_review = adaptive_engine.select_skill(lid)
        state = db.get_skill_state(lid, skill_id)
        difficulty = learner_model.difficulty_for_mastery(state["p_mastery"])

        p_correct = (skill_bias or {}).get(skill_id, 0.6)
        correct = random.random() < p_correct

        db.create_interaction(
            learner_id=lid, skill_id=skill_id, turn_index=turn, difficulty=difficulty,
            is_review=int(is_review), scenario="sim", question="sim", options_json="[]",
            correct_index=0, explanation="sim", citation="sim", retrieval_score=0.5,
            insufficient_context=0, safety_flag=0, safety_reason=None, created_at=turn,
            chosen_index=0 if correct else 1, is_correct=int(correct), answered_at=turn,
        )

        misconception = adaptive_engine.detect_misconception(lid, skill_id, correct)
        updates = learner_model.apply_response(state, correct)
        uncertainty = updates.pop("_uncertainty")
        updates["due_in_turns"] = adaptive_engine.spaced_repetition_interval(updates["p_mastery"])
        updates["flagged_misconception"] = int(misconception)
        db.upsert_skill_state(lid, skill_id, **updates)

        for st in db.get_all_skill_states(lid):
            if st["skill_id"] != skill_id and st["attempts"] > 0:
                db.upsert_skill_state(lid, st["skill_id"], due_in_turns=st["due_in_turns"] - 1)

        log.append({
            "turn": turn, "skill_id": skill_id, "skill": adaptive_engine.skill_name(skill_id),
            "is_review": is_review, "difficulty": difficulty, "correct": correct,
            "mastery_after": round(updates["p_mastery"], 2), "uncertainty_after": round(uncertainty, 3),
            "misconception": misconception,
        })
    return lid, log


def check(condition: bool, description: str, failures: list[str]):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {description}")
    if not condition:
        failures.append(description)


def run_tests():
    failures = []

    print("\n--- Test 1: monotonic mastery increase under consistent correctness ---")
    lid, log = simulate_learner("SimA_AlwaysCorrect", 8, skill_bias={s: 0.98 for s in adaptive_engine.all_skill_ids()})
    same_skill_masteries = [r["mastery_after"] for r in log if r["skill_id"] == log[0]["skill_id"]]
    check(all(x <= y + 1e-6 for x, y in zip(same_skill_masteries, same_skill_masteries[1:])) or len(same_skill_masteries) < 2,
          "Repeated correct answers on same skill -> non-decreasing mastery", failures)

    print("\n--- Test 2: mastery declines on repeated incorrect answers ---")
    lid2, log2 = simulate_learner("SimB_AlwaysWrong", 8, skill_bias={s: 0.02 for s in adaptive_engine.all_skill_ids()})
    first_state = log2[0]["mastery_after"]
    check(first_state < 0.5, "First wrong answer pulls mastery below prior-boosted midpoint", failures)

    print("\n--- Test 3: error pattern detection fires only on genuine patterns, not single slips ---")
    lid3 = db.create_learner("SimC_Misconception", None)
    skill_id = 5
    outcomes = [True, False, False]  # 2 of 3 wrong -> should flag
    flags = []
    for turn, correct in enumerate(outcomes, start=1):
        state = db.get_skill_state(lid3, skill_id)
        db.create_interaction(
            learner_id=lid3, skill_id=skill_id, turn_index=turn, difficulty="easy",
            is_review=0, scenario="s", question="q", options_json="[]", correct_index=0,
            explanation="e", citation="c", retrieval_score=0.5, insufficient_context=0,
            safety_flag=0, safety_reason=None, created_at=turn,
            chosen_index=0 if correct else 1, is_correct=int(correct), answered_at=turn,
        )
        flags.append(adaptive_engine.detect_misconception(lid3, skill_id, correct))
        updates = learner_model.apply_response(state, correct)
        updates.pop("_uncertainty")
        db.upsert_skill_state(lid3, skill_id, **updates)
    check(flags == [False, False, True], f"Flag sequence {flags} matches expected [False, False, True]", failures)

    print("\n--- Test 4: instructor context filter restricts topic selection ---")
    db.set_course_scope(1, [5, 6])
    lid4 = db.create_learner("SimD_ScopedLearner", None, course_id=1)
    seen_skills = set()
    for _ in range(6):
        sid, is_review = adaptive_engine.select_skill(lid4)
        seen_skills.add(sid)
        state = db.get_skill_state(lid4, sid)
        updates = learner_model.apply_response(state, True)
        updates.pop("_uncertainty")
        updates["due_in_turns"] = adaptive_engine.spaced_repetition_interval(updates["p_mastery"])
        db.upsert_skill_state(lid4, sid, **updates)
    check(seen_skills.issubset({5, 6}), f"Topic-restricted learner only saw skills {seen_skills} (expected subset of {{5,6}})", failures)
    db.set_course_scope(1, [])  # reset scope

    print("\n--- Test 5: spaced repetition resurfaces a previously-seen skill ---")
    lid5, log5 = simulate_learner("SimE_SpacedRep", 20, skill_bias={s: 0.9 for s in adaptive_engine.all_skill_ids()})
    reviews = [r for r in log5 if r["is_review"]]
    check(len(reviews) > 0, f"At least one 'is_review' turn occurred in 20 turns ({len(reviews)} found)", failures)

    print("\n--- Test 6: difficulty scales with mastery ---")
    low_state = {"p_mastery": 0.1}
    mid_state = {"p_mastery": 0.5}
    high_state = {"p_mastery": 0.85}
    diffs = [learner_model.difficulty_for_mastery(s["p_mastery"]) for s in (low_state, mid_state, high_state)]
    check(diffs == ["easy", "medium", "hard"], f"Difficulty progression {diffs} == ['easy','medium','hard']", failures)

    print("\n" + "=" * 60)
    if failures:
        print(f"{len(failures)} TEST(S) FAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print("ALL TESTS PASSED")


if __name__ == "__main__":
    # Use an isolated test DB so this doesn't pollute real pilot data
    db.DB_PATH = db.DB_PATH.parent / "test_harness.db"
    db.DB_PATH.unlink(missing_ok=True)
    db.init_db()
    run_tests()
