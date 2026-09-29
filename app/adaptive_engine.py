"""
Adaptive Learning Strategy Layer (Objective O2)
------------------------------------------------
Implements the five mechanisms described in the report as plain, inspectable
rules operating on the probabilistic learner model's output. No behavioural
change from the report's design — this is a simplification of *implementation*
(rules vs. a learned policy), not of the underlying concept.

  1. Topic Selection        -> select_skill()
  2. Difficulty Selection    -> learner_model.difficulty_for_mastery()
  3. Scenario Generation     -> handled in llm.py, fed by this module's output
  4. Error Pattern Detection -> detect_misconception()
  5. Spaced Repetition       -> spaced_repetition_tick() / mark_reviewed()

Instructor Context Filter is select_skill()'s `active_skill_ids` restriction.
"""

from __future__ import annotations
import json
import random
from pathlib import Path

from app import db
from app.learner_model import apply_response, difficulty_for_mastery

SKILLS_PATH = Path(__file__).parent.parent / "data" / "skills.json"
COURSES_PATH = Path(__file__).parent.parent / "data" / "courses.json"
SKILLS = json.loads(SKILLS_PATH.read_text())
SKILLS_BY_ID = {s["id"]: s for s in SKILLS}
COURSES = json.loads(COURSES_PATH.read_text())
COURSES_BY_ID = {c["id"]: c for c in COURSES}


def all_skill_ids(course_id: int | None = None) -> list[int]:
    if course_id is None:
        return [s["id"] for s in SKILLS]
    return [s["id"] for s in SKILLS if s["course_id"] == course_id]


def skill_name(skill_id: int) -> str:
    return SKILLS_BY_ID.get(skill_id, {}).get("name", f"Skill {skill_id}")


def course_name(course_id: int) -> str:
    return COURSES_BY_ID.get(course_id, {}).get("name", f"Course {course_id}")


def default_course_id() -> int:
    """Fallback for learners created before multi-course support existed, or
    if a learner is somehow created with no course_id at all."""
    return COURSES[0]["id"] if COURSES else 1


def select_skill(learner_id: int) -> tuple[int, bool]:
    """Topic Selection + Instructor Context Filter + Spaced Repetition.

    Returns (skill_id, is_review).

    Scoped to the learner's own course_id first -- a learner doing TFR never
    gets served a Level 4 skill and vice versa. Within that course:

      1. Any in-scope skill whose spaced-repetition timer is due (is_review=True)
      2. Otherwise, the in-scope skill with the weakest/least-certain estimate
         (score = (1 - mastery) + uncertainty), i.e. Topic Selection.
      3. If the instructor hasn't restricted scope for this course, every
         skill in the course is in scope -- useful for open self-study/testing.
    """
    learner = db.get_learner(learner_id)
    course_id = (learner or {}).get("course_id") or default_course_id()
    course_skill_ids = set(all_skill_ids(course_id))

    active = [sid for sid in db.get_course_scope(course_id) if sid in course_skill_ids]
    in_scope = active if active else list(course_skill_ids)

    states = {s["skill_id"]: s for s in db.get_all_skill_states(learner_id)}
    for sid in in_scope:
        if sid not in states:
            states[sid] = db.get_skill_state(learner_id, sid)

    if (learner or {}).get("condition") == "random":
        # O2 evaluation baseline: uniform random topic choice, bypassing both
        # the due-review priority check and the mastery/uncertainty ranking
        # below, so mastery growth under this condition can be compared
        # against the adaptive condition to demonstrate O2's actual effect.
        return random.choice(in_scope), False

    # 1. Due spaced-repetition reviews take priority (any previously-seen skill
    #    IN THIS COURSE, even outside current instructor scope, per report 4.2:
    #    "unresolved gaps ... flagged for deferred remediation in subsequent
    #    review sessions rather than being abandoned").
    all_states = {s["skill_id"]: s for s in db.get_all_skill_states(learner_id)}
    due = [
        sid for sid, st in all_states.items()
        if sid in course_skill_ids and st["attempts"] > 0 and st["due_in_turns"] <= 0
    ]
    if due:
        due.sort(key=lambda sid: all_states[sid]["p_mastery"])
        return due[0], True

    # 2. Topic Selection within instructor scope
    def score(sid):
        st = states[sid]
        uncertainty = _uncertainty_from_state(st)
        return (1 - st["p_mastery"]) + uncertainty

    best = max(in_scope, key=score)
    return best, False


def _uncertainty_from_state(state: dict) -> float:
    a, b = state["beta_a"], state["beta_b"]
    total = a + b
    return (a * b) / (total * total * (total + 1))


def difficulty_for_learner(condition: str | None, p_mastery: float) -> str:
    """Difficulty Selection, condition-aware. The 'random' O2-evaluation
    baseline fixes difficulty at 'medium' rather than adapting it to mastery,
    so the adaptive-vs-random comparison isolates the whole adaptive layer
    (topic + difficulty), not topic selection alone."""
    if condition == "random":
        return "medium"
    return difficulty_for_mastery(p_mastery)


def spaced_repetition_interval(p_mastery: float) -> int:
    """Turns until this skill is due for review again. Higher mastery ->
    longer interval (fewer reviews needed), matching the report's principle
    that 'concepts with stable mastery estimates are revisited less
    frequently, while those showing declining performance ... are
    prioritised for re-exposure.' Turn-based rather than wall-clock so a
    single pilot session can exercise the full mechanism; swap for
    day-based intervals for a multi-week deployment."""
    return round(3 + p_mastery * 12)


def detect_misconception(learner_id: int, skill_id: int, latest_correct: bool) -> bool:
    """Error Pattern Detection: distinguishes a one-off slip from a recurring
    misconception by looking at the last 3 attempts on this skill. Flags if
    at least 2 of the last 3 (including this one) were wrong -- a pattern,
    not a single slip that the guess/slip model already absorbs."""
    if latest_correct:
        return False
    recent = db.get_recent_interactions(learner_id, skill_id, limit=3)
    wrong_count = sum(1 for r in recent if r["is_correct"] == 0)
    return wrong_count >= 2  # includes the response just recorded


def instructor_context_summary(course_id: int) -> dict:
    active = db.get_course_scope(course_id)
    return {
        "course_id": course_id,
        "active_skill_ids": active,
        "active_skill_names": [skill_name(s) for s in active],
        "scope": "restricted" if active else "all_skills",
    }


def record_response(learner_id: int, skill_id: int, correct: bool) -> dict:
    """Shared scoring path for BOTH a plain MCQ answer and a single Patient
    Simulator decision point. Applies the BKT mastery update, error-pattern
    detection, and spaced-repetition scheduling, then persists the new
    skill_state. Returns the info the caller needs to build its response
    and to log an alert if a misconception was flagged.

    Keeping this in one place means the Patient Simulator's per-step
    decisions feed the exact same probabilistic learner model (O1) as
    ordinary questions, rather than a parallel, harder-to-defend scoring
    scheme.
    """
    state = db.get_skill_state(learner_id, skill_id)
    updates = apply_response(state, correct)
    uncertainty = updates.pop("_uncertainty")

    misconception = detect_misconception(learner_id, skill_id, correct)
    updates["due_in_turns"] = spaced_repetition_interval(updates["p_mastery"])
    updates["flagged_misconception"] = int(misconception)
    db.upsert_skill_state(learner_id, skill_id, **updates)

    for st in db.get_all_skill_states(learner_id):
        if st["skill_id"] != skill_id and st["attempts"] > 0:
            db.upsert_skill_state(learner_id, st["skill_id"], due_in_turns=st["due_in_turns"] - 1)

    return {
        "new_mastery": round(updates["p_mastery"], 3),
        "uncertainty": round(uncertainty, 3),
        "misconception_flagged": misconception,
    }
