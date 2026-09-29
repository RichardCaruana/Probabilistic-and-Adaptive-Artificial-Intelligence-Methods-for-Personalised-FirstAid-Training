"""
O2 evaluation baseline: the 'random' condition should bypass topic-selection
ranking and spaced-repetition priority entirely, and fix difficulty at
'medium' -- so that mastery growth under 'adaptive' vs 'random' is a valid
between-groups comparison of the whole adaptive strategy layer, not just a
comparison confounded by different difficulty exposure.
"""
from app import db
from app.adaptive_engine import select_skill, difficulty_for_learner
from app.learner_model import difficulty_for_mastery


def _join_with_condition(client, name, condition):
    res = client.post("/api/learner/join", json={
        "name": name, "course_id": 1, "condition": condition,
    })
    return res.json()["learner_id"]


def test_random_condition_ignores_ranking_and_scheduling(client):
    # Restrict the instructor context filter to a single skill so
    # random.choice(in_scope) is deterministic without mocking random.
    db.set_course_scope(1, [3])
    try:
        learner_id = _join_with_condition(client, "Baseline Learner", "random")
        skill_id, is_review = select_skill(learner_id)
        assert skill_id == 3
        assert is_review is False
    finally:
        db.set_course_scope(1, [])  # restore open-scope for other tests


def test_adaptive_condition_is_default(client):
    learner_id = db.create_learner("Default Condition Learner", None, course_id=1)
    learner = db.get_learner(learner_id)
    assert learner["condition"] == "adaptive"


def test_difficulty_for_learner_fixes_medium_for_random_condition():
    assert difficulty_for_learner("random", 0.05) == "medium"
    assert difficulty_for_learner("random", 0.95) == "medium"


def test_difficulty_for_learner_matches_adaptive_mapping_otherwise():
    for p_mastery in (0.1, 0.5, 0.9):
        assert difficulty_for_learner("adaptive", p_mastery) == difficulty_for_mastery(p_mastery)
        assert difficulty_for_learner(None, p_mastery) == difficulty_for_mastery(p_mastery)
