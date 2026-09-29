import csv
import io
import time

from app import db


def _rows(response):
    return list(csv.DictReader(io.StringIO(response.text)))


def test_export_interactions_csv(client):
    learner_id = _join(client, "Export Interactions Learner", cohort="cohortA", condition="random")
    interaction_id = db.create_interaction(
        learner_id=learner_id, skill_id=1, turn_index=1, difficulty="easy",
        is_review=0, scenario="s", question="q", options_json="[]",
        correct_index=0, explanation="e", citation="", retrieval_score=0.5,
        insufficient_context=0, safety_flag=0, safety_reason=None,
        review_status="approved", confidence_score=0.9, review_reason=None,
        created_at=time.time(),
    )

    res = client.get("/api/instructor/export/interactions.csv")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/csv")
    rows = _rows(res)
    row = next(r for r in rows if int(r["id"]) == interaction_id)
    assert row["learner_id"] == str(learner_id)
    assert row["skill_name"] == "Role & Responsibilities of a First Aider"
    assert row["question"] == "q"
    assert row["cohort"] == "cohortA"
    assert row["condition"] == "random"


def test_export_mastery_csv(client):
    learner_id = _join(client, "Export Mastery Learner", cohort="cohortB", condition="adaptive")
    db.get_skill_state(learner_id, 1)  # auto-creates a default row

    res = client.get("/api/instructor/export/mastery.csv")
    assert res.status_code == 200
    rows = _rows(res)
    row = next(r for r in rows if r["learner_id"] == str(learner_id) and r["skill_id"] == "1")
    assert row["skill_name"] == "Role & Responsibilities of a First Aider"
    assert float(row["p_mastery"]) > 0
    assert float(row["uncertainty"]) > 0
    assert row["cohort"] == "cohortB"
    assert row["condition"] == "adaptive"


def test_export_corpus_coverage_csv(client):
    res = client.get("/api/instructor/export/corpus_coverage.csv")
    assert res.status_code == 200
    rows = _rows(res)
    row = next(r for r in rows if r["skill_id"] == "1")
    assert row["skill_name"] == "Role & Responsibilities of a First Aider"
    assert int(row["corpus_word_count"]) > 0
    assert int(row["generation_attempts"]) >= 0


def test_export_alerts_csv(client):
    learner_id = _join(client, "Export Alerts Learner")
    alert_id = db.create_alert(learner_id, 1, "export test alert")

    res = client.get("/api/instructor/export/alerts.csv")
    assert res.status_code == 200
    rows = _rows(res)
    row = next(r for r in rows if int(r["id"]) == alert_id)
    assert row["learner_name"] == "Export Alerts Learner"
    assert row["message"] == "export test alert"


def _join(client, name, cohort=None, condition=None):
    return client.post("/api/learner/join", json={
        "name": name, "course_id": 1, "cohort": cohort, "condition": condition,
    }).json()["learner_id"]
