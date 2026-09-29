"""
Exercises the learner-facing quiz + simulation loop end-to-end against the
real API routes. `llm.generate_scenario_question`/`generate_simulation` are
monkeypatched to fixed, valid-shaped dicts so these tests need no
ANTHROPIC_API_KEY and no network call (app/llm.py:32-38 requires a real key
otherwise). Retrieval (app/rag.py) is left real -- it needs no API key and
the TF-IDF index is already built in data/index/.
"""
from app import db, llm


FAKE_QUESTION = {
    "insufficient_context": False,
    "scenario": "A colleague collapses in the warehouse.",
    "question": "What is the first thing you should do?",
    "options": ["Call for help", "Give water", "Walk away", "Take a photo"],
    "correct_index": 0,
    "explanation": "Calling for help is the first step per the source material.",
    "source_citation": "Unit 1",
    "safety_flag": False,
    "safety_reason": None,
    "confidence_score": 0.9,
}

FAKE_SIMULATION = {
    "insufficient_context": False,
    "patient_intro": "A patient is unresponsive on the warehouse floor.",
    "steps": [
        {
            "prompt": "What do you do first?",
            "actions": [
                {"label": "Check responsiveness", "correct": True, "outcome": "Good call."},
                {"label": "Leave the scene", "correct": False, "outcome": "Not the best choice."},
            ],
        },
    ],
    "resolution": "The patient recovers.",
    "safety_flag": False,
    "safety_reason": None,
    "confidence_score": 0.9,
}


def _join(client, name):
    return client.post("/api/learner/join", json={"name": name, "course_id": 1}).json()["learner_id"]


def test_quiz_loop_updates_mastery(client, monkeypatch):
    monkeypatch.setattr(llm, "generate_scenario_question", lambda **kw: FAKE_QUESTION)
    learner_id = _join(client, "Quiz Learner")

    res = client.get(f"/api/learner/{learner_id}/next_question")
    assert res.status_code == 200
    data = res.json()
    assert data["insufficient_context"] is False
    assert data["pending_review"] is False
    interaction_id = data["interaction_id"]

    res = client.post(f"/api/learner/{learner_id}/answer", json={
        "interaction_id": interaction_id, "chosen_index": 0, "response_time_ms": 1500,
    })
    assert res.status_code == 200
    answer_data = res.json()
    assert answer_data["is_correct"] is True
    assert answer_data["new_mastery"] > 0.15  # moved up from PRIOR_MASTERY


def test_review_mode_always_gates_question(client, monkeypatch):
    monkeypatch.setattr(llm, "generate_scenario_question", lambda **kw: FAKE_QUESTION)
    client.post("/api/instructor/review_mode", json={"mode": "always"})
    try:
        learner_id = _join(client, "Reviewed Learner")
        res = client.get(f"/api/learner/{learner_id}/next_question")
        data = res.json()
        assert data["pending_review"] is True
        interaction_id = data["interaction_id"]

        res = client.get(f"/api/learner/{learner_id}/question_status/{interaction_id}")
        assert res.json()["status"] == "pending"

        res = client.post(f"/api/instructor/review/{interaction_id}/approve", json={})
        assert res.status_code == 200

        res = client.get(f"/api/learner/{learner_id}/question_status/{interaction_id}")
        assert res.json()["status"] == "approved"
    finally:
        client.post("/api/instructor/review_mode", json={"mode": "off"})


def test_simulation_single_step_completes(client, monkeypatch):
    monkeypatch.setattr(llm, "generate_simulation", lambda **kw: FAKE_SIMULATION)
    learner_id = _join(client, "Sim Learner")

    res = client.get(f"/api/learner/{learner_id}/simulation/start")
    assert res.status_code == 200
    data = res.json()
    assert data["completed"] is False
    sim_id = data["sim_id"]

    res = client.post(f"/api/learner/{learner_id}/simulation/{sim_id}/action", json={"action_index": 0})
    assert res.status_code == 200
    result = res.json()
    assert result["is_correct"] is True
    assert result["completed"] is True
    assert result["score"] == "1/1"
