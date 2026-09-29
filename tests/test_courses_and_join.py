def test_list_courses(client):
    res = client.get("/api/courses")
    assert res.status_code == 200
    courses = res.json()["courses"]
    assert len(courses) >= 1
    assert courses[0]["id"] == 1


def test_join_with_explicit_course_id(client):
    res = client.post("/api/learner/join", json={
        "name": "Alice", "profession": "warehouse operator", "course_id": 1,
    })
    assert res.status_code == 200
    data = res.json()
    assert data["course_id"] == 1
    assert isinstance(data["learner_id"], int)


def test_join_falls_back_to_default_course(client):
    res = client.post("/api/learner/join", json={"name": "Bob"})
    assert res.status_code == 200
    data = res.json()
    assert data["course_id"] == 1


def test_join_auto_assigns_a_valid_condition(client):
    res = client.post("/api/learner/join", json={"name": "Auto Condition"})
    assert res.status_code == 200
    assert res.json()["condition"] in ("adaptive", "random")


def test_join_respects_explicit_condition_and_cohort(client):
    res = client.post("/api/learner/join", json={
        "name": "Explicit Condition", "condition": "random", "cohort": "2026-07-28-morning",
    })
    assert res.status_code == 200
    data = res.json()
    assert data["condition"] == "random"

    overview = client.get("/api/instructor/overview").json()
    learner = next(l for l in overview["learners"] if l["id"] == data["learner_id"])
    assert learner["condition"] == "random"
    assert learner["cohort"] == "2026-07-28-morning"
