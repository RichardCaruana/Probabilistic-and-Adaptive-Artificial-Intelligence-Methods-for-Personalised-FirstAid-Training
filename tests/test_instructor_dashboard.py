from app import db


def _join(client, name="Learner"):
    res = client.post("/api/learner/join", json={"name": name, "course_id": 1})
    return res.json()["learner_id"]


def test_overview_lists_joined_learner(client):
    learner_id = _join(client, "Overview Learner")
    res = client.get("/api/instructor/overview")
    assert res.status_code == 200
    data = res.json()
    learner = next(l for l in data["learners"] if l["id"] == learner_id)
    assert learner["course_id"] == 1
    assert "1" in learner["mastery_by_skill"]  # skill_id 1 exists for course 1 (JSON keys are strings)
    assert learner["condition"] in ("adaptive", "random")
    assert learner["cohort"] is None


def test_learner_detail_happy_path_and_404(client):
    learner_id = _join(client, "Detail Learner")
    res = client.get(f"/api/instructor/learner/{learner_id}")
    assert res.status_code == 200
    data = res.json()
    assert data["learner"]["id"] == learner_id
    assert any(s["id"] == 1 for s in data["skills"])

    res = client.get("/api/instructor/learner/999999")
    assert res.status_code == 404


def test_alerts_create_list_resolve(client):
    learner_id = _join(client, "Alert Learner")
    alert_id = db.create_alert(learner_id, 1, "test alert message")

    res = client.get("/api/instructor/alerts")
    assert res.status_code == 200
    alerts = res.json()["alerts"]
    assert any(a["id"] == alert_id for a in alerts)

    res = client.post(f"/api/instructor/alerts/{alert_id}/resolve")
    assert res.status_code == 200

    res = client.get("/api/instructor/alerts")
    alerts = res.json()["alerts"]
    assert not any(a["id"] == alert_id for a in alerts)  # unresolved_only=True by default


def test_topic_scope_round_trip(client):
    res = client.post("/api/instructor/topic", json={"course_id": 1, "skill_ids": [1, 2]})
    assert res.status_code == 200

    res = client.get("/api/instructor/topic", params={"course_id": 1})
    assert res.status_code == 200


def test_review_mode_round_trip_and_validation(client):
    res = client.post("/api/instructor/review_mode", json={"mode": "confidence", "threshold": 0.5})
    assert res.status_code == 200
    assert res.json() == {"mode": "confidence", "threshold": 0.5}

    res = client.get("/api/instructor/review_mode")
    assert res.json() == {"mode": "confidence", "threshold": 0.5}

    res = client.post("/api/instructor/review_mode", json={"mode": "bogus"})
    assert res.status_code == 400

    # reset to 'off' so later tests (e.g. learner flow) aren't affected
    client.post("/api/instructor/review_mode", json={"mode": "off"})
