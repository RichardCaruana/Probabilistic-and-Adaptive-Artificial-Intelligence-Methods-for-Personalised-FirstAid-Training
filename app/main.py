from __future__ import annotations
import csv
import io
import json as _json
import random
import time
from datetime import datetime
from urllib.parse import quote

from dotenv import load_dotenv
load_dotenv()  # picks up ANTHROPIC_API_KEY from a .env file in the project root

import qrcode
from typing import Optional
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app import db, rag, llm
from app.adaptive_engine import (
    select_skill, spaced_repetition_interval, detect_misconception,
    skill_name, all_skill_ids, instructor_context_summary, record_response, SKILLS,
    difficulty_for_learner,
)
from app.learner_model import apply_response

# Below this confidence, a question/simulation is always logged as an
# instructor alert regardless of review mode -- separate from the
# mode-driven review-routing threshold (stored per-instructor in the DB via
# db.get_review_mode()/set_review_mode()); this just makes sure genuinely
# weak generations don't go unnoticed even when review mode is "off".
ALERT_THRESHOLD = 0.40

app = FastAPI(title="Adaptive First-Aid Training Prototype")
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

db.init_db()


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/learner", response_class=HTMLResponse)
def learner_page(request: Request):
    return templates.TemplateResponse("learner.html", {"request": request})


@app.get("/instructor", response_class=HTMLResponse)
def instructor_page(request: Request):
    return templates.TemplateResponse(
        "instructor.html", {"request": request, "skills": SKILLS}
    )


@app.get("/qr.png")
def qr_code(request: Request, cohort: Optional[str] = None):
    """QR code linking to the learner join page, per the report's design of
    connecting printed course materials to the adaptive tool. An optional
    `cohort` label is baked into the encoded URL (O4) so learners scanning it
    are auto-tagged with which session/class they joined from."""
    base = str(request.base_url).rstrip("/")
    url = f"{base}/learner"
    if cohort:
        url += f"?cohort={quote(cohort)}"
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")


# ---------------- Learner API ----------------

@app.get("/api/courses")
def list_courses():
    from app.adaptive_engine import COURSES
    return {"courses": COURSES}


class JoinRequest(BaseModel):
    name: str
    profession: Optional[str] = None
    course_id: Optional[int] = None
    cohort: Optional[str] = None
    condition: Optional[str] = None  # 'adaptive' | 'random'; auto-assigned 50/50 if omitted


@app.post("/api/learner/join")
def join(req: JoinRequest):
    from app.adaptive_engine import default_course_id
    course_id = req.course_id or default_course_id()
    condition = req.condition if req.condition in ("adaptive", "random") else random.choice(
        ["adaptive", "random"]
    )
    learner_id = db.create_learner(
        req.name.strip(), (req.profession or "").strip() or None, course_id=course_id,
        cohort=(req.cohort or "").strip() or None, condition=condition,
    )
    return {"learner_id": learner_id, "course_id": course_id, "condition": condition}


@app.get("/api/learner/{learner_id}/next_question")
def next_question(learner_id: int):
    learner = db.get_learner(learner_id)
    if not learner:
        raise HTTPException(404, "Learner not found")

    skill_id, is_review = select_skill(learner_id)
    state = db.get_skill_state(learner_id, skill_id)
    difficulty = difficulty_for_learner(learner.get("condition"), state["p_mastery"])
    sname = skill_name(skill_id)

    query = f"{sname} {difficulty} scenario {learner.get('profession') or ''}"
    retrieval = rag.retrieve(skill_id, query, top_k=3)

    generated = llm.generate_scenario_question(
        skill_name=sname,
        difficulty=difficulty,
        passages=retrieval["passages"],
        profession=learner.get("profession"),
        is_review=is_review,
        retrieval_score=retrieval["top_score"],
    )

    turn_index = db.count_turns(learner_id) + 1

    if generated.get("insufficient_context") or not generated.get("options"):
        # Log it so the instructor can see corpus gaps, but don't hand a
        # broken question to the learner -- surface a clear message instead.
        # Always approved (nothing meaningful to review-gate here), but
        # confidence is recorded as 0.0 for consistency in the interactions
        # table.
        interaction_id = db.create_interaction(
            learner_id=learner_id, skill_id=skill_id, turn_index=turn_index,
            difficulty=difficulty, is_review=int(is_review),
            scenario="", question="", options_json="[]", correct_index=0,
            explanation=generated.get("explanation", ""), citation="",
            retrieval_score=retrieval["top_score"],
            insufficient_context=1, safety_flag=int(generated.get("safety_flag", False)),
            safety_reason=generated.get("safety_reason"), created_at=time.time(),
            review_status="approved",
            confidence_score=generated.get("confidence_score", 0.0),
            review_reason=None,
        )
        db.create_alert(learner_id, skill_id,
                         f"Insufficient grounding material for '{sname}' at {difficulty} difficulty.")
        return {
            "interaction_id": interaction_id, "insufficient_context": True,
            "skill_name": sname,
            "message": "No adequately-grounded question could be generated for this "
                        "topic right now. An instructor alert has been logged.",
        }

    # --- Confidence-based review routing (Feature 2) ---
    confidence = generated.get("confidence_score", 0.0)
    review_settings = db.get_review_mode()
    mode = review_settings["mode"]
    threshold = review_settings["threshold"]

    if mode == "always":
        review_status = "pending"
        review_reason = "manual"
    elif mode == "confidence" and confidence < threshold:
        review_status = "pending"
        review_reason = "low_confidence"
    else:
        review_status = "approved"
        review_reason = None

    interaction_id = db.create_interaction(
        learner_id=learner_id, skill_id=skill_id, turn_index=turn_index,
        difficulty=difficulty, is_review=int(is_review),
        scenario=generated["scenario"], question=generated["question"],
        options_json=_json.dumps(generated["options"]),
        correct_index=generated["correct_index"],
        explanation=generated["explanation"], citation=generated.get("source_citation", ""),
        retrieval_score=retrieval["top_score"], insufficient_context=0,
        safety_flag=int(generated.get("safety_flag", False)),
        safety_reason=generated.get("safety_reason"), created_at=time.time(),
        review_status=review_status,
        confidence_score=confidence,
        review_reason=review_reason,
    )

    if generated.get("safety_flag"):
        db.create_alert(learner_id, skill_id,
                         f"Safety filter flagged a generated question for '{sname}': "
                         f"{generated.get('safety_reason')}")

    if confidence < ALERT_THRESHOLD:
        db.create_alert(learner_id, skill_id,
                         f"Low confidence ({confidence:.0%}) on '{sname}' ({difficulty})")

    if review_status == "pending":
        # Learner sees a holding screen; frontend polls /question_status until approved.
        return {
            "interaction_id": interaction_id,
            "insufficient_context": False,
            "pending_review": True,
            "skill_name": sname,
        }

    return {
        "interaction_id": interaction_id,
        "insufficient_context": False,
        "pending_review": False,
        "skill_id": skill_id,
        "skill_name": sname,
        "difficulty": difficulty,
        "is_review": is_review,
        "scenario": generated["scenario"],
        "question": generated["question"],
        "options": generated["options"],
    }


@app.get("/api/learner/{learner_id}/question_status/{interaction_id}")
def question_status(learner_id: int, interaction_id: int):
    """Polled by the learner's browser while a question is awaiting instructor
    approval (Review Mode). Once approved, returns the same shape next_question
    would have; if rejected, tells the frontend to request a fresh question."""
    interaction = db.get_interaction(interaction_id)
    if not interaction or interaction["learner_id"] != learner_id:
        raise HTTPException(404, "Interaction not found")

    if interaction["review_status"] == "pending":
        return {"status": "pending"}

    if interaction["review_status"] == "rejected":
        return {"status": "rejected"}

    return {
        "status": "approved",
        "interaction_id": interaction["id"],
        "skill_id": interaction["skill_id"],
        "skill_name": skill_name(interaction["skill_id"]),
        "difficulty": interaction["difficulty"],
        "is_review": bool(interaction["is_review"]),
        "scenario": interaction["scenario"],
        "question": interaction["question"],
        "options": _json.loads(interaction["options_json"]),
    }


class AnswerRequest(BaseModel):
    interaction_id: int
    chosen_index: int
    response_time_ms: Optional[int] = None


@app.post("/api/learner/{learner_id}/answer")
def answer(learner_id: int, req: AnswerRequest):
    interaction = db.get_interaction(req.interaction_id)
    if not interaction or interaction["learner_id"] != learner_id:
        raise HTTPException(404, "Interaction not found")
    if interaction["answered_at"] is not None:
        raise HTTPException(400, "Already answered")
    if interaction["review_status"] != "approved":
        raise HTTPException(409, "This question has not been approved yet")

    is_correct = req.chosen_index == interaction["correct_index"]
    db.update_interaction(
        req.interaction_id, chosen_index=req.chosen_index, is_correct=int(is_correct),
        response_time_ms=req.response_time_ms, answered_at=time.time(),
    )

    skill_id = interaction["skill_id"]
    result = record_response(learner_id, skill_id, is_correct)

    if result["misconception_flagged"]:
        db.create_alert(
            learner_id, skill_id,
            f"{db.get_learner(learner_id)['name']} shows a recurring misconception "
            f"in '{skill_name(skill_id)}' (2+ wrong of last 3 attempts).",
        )

    return {
        "is_correct": is_correct,
        "explanation": interaction["explanation"],
        "citation": interaction["citation"],
        "correct_index": interaction["correct_index"],
        "new_mastery": result["new_mastery"],
        "uncertainty": result["uncertainty"],
        "misconception_flagged": result["misconception_flagged"],
    }


@app.get("/api/learner/{learner_id}/simulation/start")
def start_simulation(learner_id: int):
    learner = db.get_learner(learner_id)
    if not learner:
        raise HTTPException(404, "Learner not found")

    skill_id, _ = select_skill(learner_id)
    state = db.get_skill_state(learner_id, skill_id)
    difficulty = difficulty_for_learner(learner.get("condition"), state["p_mastery"])
    sname = skill_name(skill_id)

    query = f"{sname} {difficulty} procedure sequence steps"
    retrieval = rag.retrieve(skill_id, query, top_k=4)

    generated = llm.generate_simulation(
        skill_name=sname, difficulty=difficulty,
        passages=retrieval["passages"], profession=learner.get("profession"),
        retrieval_score=retrieval["top_score"],
    )

    if generated.get("insufficient_context") or not generated.get("steps"):
        db.create_alert(learner_id, skill_id,
                         f"Insufficient grounding material for a '{sname}' simulation.")
        return {
            "insufficient_context": True, "skill_name": sname,
            "message": "No adequately-grounded simulation could be generated for this "
                        "topic right now. An instructor alert has been logged.",
        }

    # --- Confidence-based review routing (Feature 2), mirrors next_question ---
    confidence = generated.get("confidence_score", 0.0)
    review_settings = db.get_review_mode()
    mode = review_settings["mode"]
    threshold = review_settings["threshold"]

    if mode == "always":
        review_status = "pending"
        review_reason = "manual"
    elif mode == "confidence" and confidence < threshold:
        review_status = "pending"
        review_reason = "low_confidence"
    else:
        review_status = "approved"
        review_reason = None

    sim_id = db.create_simulation(
        learner_id=learner_id, skill_id=skill_id, difficulty=difficulty,
        patient_intro=generated["patient_intro"], steps_json=_json.dumps(generated["steps"]),
        resolution=generated.get("resolution", ""), current_step=0,
        correct_count=0, total_steps=len(generated["steps"]), completed=0,
        safety_flag=int(generated.get("safety_flag", False)),
        safety_reason=generated.get("safety_reason"), review_status=review_status,
        confidence_score=confidence, review_reason=review_reason,
        created_at=time.time(),
    )

    if generated.get("safety_flag"):
        db.create_alert(learner_id, skill_id,
                         f"Safety filter flagged a generated simulation for '{sname}': "
                         f"{generated.get('safety_reason')}")

    if confidence < ALERT_THRESHOLD:
        db.create_alert(learner_id, skill_id,
                         f"Low confidence ({confidence:.0%}) on '{sname}' simulation ({difficulty})")

    if review_status == "pending":
        return {"sim_id": sim_id, "insufficient_context": False, "pending_review": True, "skill_name": sname}

    return _simulation_public_view(db.get_simulation(sim_id))


@app.get("/api/learner/{learner_id}/simulation/{sim_id}/status")
def simulation_status(learner_id: int, sim_id: int):
    sim = db.get_simulation(sim_id)
    if not sim or sim["learner_id"] != learner_id:
        raise HTTPException(404, "Simulation not found")
    if sim["review_status"] == "pending":
        return {"status": "pending"}
    if sim["review_status"] == "rejected":
        return {"status": "rejected"}
    view = _simulation_public_view(sim)
    view["status"] = "approved"
    return view


def _simulation_public_view(sim: dict) -> dict:
    """Only ever send the CURRENT step's actions to the learner -- never the
    full script, so the correct answer and future steps aren't visible in
    the browser's network tab."""
    steps = _json.loads(sim["steps_json"])
    step_index = sim["current_step"]
    if sim["completed"] or step_index >= len(steps):
        return {
            "pending_review": False, "sim_id": sim["id"], "completed": True,
            "resolution": sim["resolution"],
            "score": f"{sim['correct_count']}/{sim['total_steps']}",
        }
    current = steps[step_index]
    return {
        "pending_review": False, "sim_id": sim["id"], "completed": False,
        "skill_id": sim["skill_id"], "skill_name": skill_name(sim["skill_id"]),
        "difficulty": sim["difficulty"], "step_index": step_index,
        "total_steps": sim["total_steps"],
        "patient_intro": sim["patient_intro"] if step_index == 0 else None,
        "prompt": current["prompt"],
        "actions": [a["label"] for a in current["actions"]],
    }


class SimulationActionRequest(BaseModel):
    action_index: int


@app.post("/api/learner/{learner_id}/simulation/{sim_id}/action")
def simulation_action(learner_id: int, sim_id: int, req: SimulationActionRequest):
    sim = db.get_simulation(sim_id)
    if not sim or sim["learner_id"] != learner_id:
        raise HTTPException(404, "Simulation not found")
    if sim["review_status"] != "approved":
        raise HTTPException(409, "This simulation has not been approved yet")
    if sim["completed"]:
        raise HTTPException(400, "Simulation already completed")

    steps = _json.loads(sim["steps_json"])
    step_index = sim["current_step"]
    if step_index >= len(steps):
        raise HTTPException(400, "No more steps")

    current = steps[step_index]
    actions = current["actions"]
    if not (0 <= req.action_index < len(actions)):
        raise HTTPException(400, "Invalid action index")

    chosen = actions[req.action_index]
    is_correct = bool(chosen.get("correct"))
    correct_idx = next(i for i, a in enumerate(actions) if a.get("correct"))

    result = record_response(learner_id, sim["skill_id"], is_correct)
    if result["misconception_flagged"]:
        db.create_alert(
            learner_id, sim["skill_id"],
            f"{db.get_learner(learner_id)['name']} shows a recurring misconception "
            f"in '{skill_name(sim['skill_id'])}' (2+ wrong of last 3 attempts).",
        )

    # Log exactly like an MCQ interaction so the dashboard, mastery model,
    # and error-pattern detection all treat this identically (see O1/O2 in
    # the code manual -- Patient Simulator decisions feed the same model).
    turn_index = db.count_turns(learner_id) + 1
    db.create_interaction(
        learner_id=learner_id, skill_id=sim["skill_id"], turn_index=turn_index,
        difficulty=sim["difficulty"], is_review=0, modality="simulation",
        scenario=sim["patient_intro"], question=current["prompt"],
        options_json=_json.dumps([a["label"] for a in actions]), correct_index=correct_idx,
        explanation=chosen.get("outcome", ""), citation="", retrieval_score=None,
        insufficient_context=0, chosen_index=req.action_index, is_correct=int(is_correct),
        response_time_ms=None, safety_flag=0, safety_reason=None,
        review_status="approved", created_at=time.time(), answered_at=time.time(),
    )

    new_correct_count = sim["correct_count"] + (1 if is_correct else 0)
    new_step = step_index + 1
    completed = new_step >= len(steps)
    db.update_simulation(sim_id, current_step=new_step, correct_count=new_correct_count,
                          completed=int(completed))

    response = {
        "is_correct": is_correct,
        "outcome": chosen.get("outcome", ""),
        "new_mastery": result["new_mastery"],
        "completed": completed,
    }
    if completed:
        response["resolution"] = sim["resolution"]
        response["score"] = f"{new_correct_count}/{len(steps)}"
    else:
        next_step = steps[new_step]
        response["next_prompt"] = next_step["prompt"]
        response["next_actions"] = [a["label"] for a in next_step["actions"]]
        response["step_index"] = new_step
        response["total_steps"] = len(steps)
    return response


# ---------------- Instructor API ----------------

@app.get("/api/instructor/overview")
def instructor_overview():
    from app.adaptive_engine import course_name, default_course_id
    learners = db.list_learners()
    out = []
    for learner in learners:
        course_id = learner.get("course_id") or default_course_id()
        learner_skill_ids = all_skill_ids(course_id)
        states = {s["skill_id"]: s for s in db.get_all_skill_states(learner["id"])}
        mastery_by_skill = {
            sid: round(states[sid]["p_mastery"], 2) if sid in states else None
            for sid in learner_skill_ids
        }
        attempted = [sid for sid in learner_skill_ids if states.get(sid, {}).get("attempts", 0) > 0]
        avg_mastery = (
            sum(states[sid]["p_mastery"] for sid in attempted) / len(attempted)
            if attempted else None
        )
        out.append({
            "id": learner["id"], "name": learner["name"], "profession": learner["profession"],
            "course_id": course_id, "course_name": course_name(course_id),
            "cohort": learner.get("cohort"), "condition": learner.get("condition", "adaptive"),
            "mastery_by_skill": mastery_by_skill,
            "avg_mastery": round(avg_mastery, 2) if avg_mastery is not None else None,
            "turns": db.count_turns(learner["id"]),
        })
    return {"learners": out, "skills": SKILLS}


@app.get("/api/instructor/alerts")
def instructor_alerts():
    return {"alerts": db.list_alerts(unresolved_only=True)}


@app.post("/api/instructor/alerts/{alert_id}/resolve")
def resolve_alert(alert_id: int):
    db.resolve_alert(alert_id)
    return {"ok": True}


class TopicRequest(BaseModel):
    course_id: int
    skill_ids: list[int]


@app.post("/api/instructor/topic")
def set_topic(req: TopicRequest):
    db.set_course_scope(req.course_id, req.skill_ids)
    return instructor_context_summary(req.course_id)


@app.get("/api/instructor/topic")
def get_topic(course_id: int):
    return instructor_context_summary(course_id)


class ReviewModeRequest(BaseModel):
    mode: str  # 'off' | 'always' | 'confidence'
    threshold: Optional[float] = None


@app.get("/api/instructor/review_mode")
def get_review_mode_endpoint():
    return db.get_review_mode()


@app.post("/api/instructor/review_mode")
def set_review_mode_endpoint(req: ReviewModeRequest):
    if req.mode not in ("off", "always", "confidence"):
        raise HTTPException(400, "mode must be one of: off, always, confidence")
    db.set_review_mode(req.mode, req.threshold)
    return db.get_review_mode()


@app.get("/api/instructor/review_queue")
def review_queue():
    """Questions currently awaiting instructor approval (Review Mode)."""
    pending = db.list_pending_interactions()
    out = []
    for p in pending:
        learner = db.get_learner(p["learner_id"])
        out.append({
            "interaction_id": p["id"],
            "learner_name": learner["name"] if learner else "?",
            "skill_name": skill_name(p["skill_id"]),
            "difficulty": p["difficulty"],
            "scenario": p["scenario"],
            "question": p["question"],
            "options": _json.loads(p["options_json"]),
            "correct_index": p["correct_index"],
            "explanation": p["explanation"],
            "citation": p["citation"],
            "confidence_score": p.get("confidence_score"),
            "review_reason": p.get("review_reason"),
            "safety_flag": bool(p["safety_flag"]),
            "safety_reason": p["safety_reason"],
        })
    return {"pending": out}


class ReviewDecisionRequest(BaseModel):
    # Optional edits the instructor can make before approving
    question: Optional[str] = None
    options: Optional[list[str]] = None
    correct_index: Optional[int] = None
    explanation: Optional[str] = None


@app.post("/api/instructor/review/{interaction_id}/approve")
def approve_review(interaction_id: int, req: ReviewDecisionRequest):
    edits = {}
    if req.question is not None:
        edits["question"] = req.question
    if req.options is not None:
        edits["options_json"] = _json.dumps(req.options)
    if req.correct_index is not None:
        edits["correct_index"] = req.correct_index
    if req.explanation is not None:
        edits["explanation"] = req.explanation
    db.set_review_status(interaction_id, "approved", **edits)
    return {"ok": True}


@app.post("/api/instructor/review/{interaction_id}/reject")
def reject_review(interaction_id: int):
    db.set_review_status(interaction_id, "rejected")
    return {"ok": True}


@app.get("/api/instructor/review_queue_simulations")
def review_queue_simulations():
    """Pending Patient Simulator scripts awaiting approval. Unlike MCQ review,
    this is Approve/Reject only (no inline editing) -- a multi-step branching
    script is impractical to hand-edit in a text box; reject and regenerate
    is the intended fix-up path here."""
    pending = db.list_pending_simulations()
    out = []
    for sim in pending:
        learner = db.get_learner(sim["learner_id"])
        out.append({
            "sim_id": sim["id"],
            "learner_name": learner["name"] if learner else "?",
            "skill_name": skill_name(sim["skill_id"]),
            "difficulty": sim["difficulty"],
            "patient_intro": sim["patient_intro"],
            "steps": _json.loads(sim["steps_json"]),
            "resolution": sim["resolution"],
            "confidence_score": sim.get("confidence_score"),
            "review_reason": sim.get("review_reason"),
            "safety_flag": bool(sim["safety_flag"]),
            "safety_reason": sim["safety_reason"],
        })
    return {"pending": out}


@app.post("/api/instructor/review_simulation/{sim_id}/approve")
def approve_simulation(sim_id: int):
    db.set_simulation_review_status(sim_id, "approved")
    return {"ok": True}


@app.post("/api/instructor/review_simulation/{sim_id}/reject")
def reject_simulation(sim_id: int):
    db.set_simulation_review_status(sim_id, "rejected")
    return {"ok": True}


# ---------------- Instructor Export (for results-chapter analysis) ----------------
# CSV dumps of interactions/mastery/alerts across all learners, so the
# results chapter doesn't have to go through raw `sqlite3 data/app.db` queries.

def _csv_response(rows: list[dict], columns: list[str], filename: str) -> StreamingResponse:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/api/instructor/export/interactions.csv")
def export_interactions():
    from app.adaptive_engine import course_name, skill_name
    rows = []
    for it in db.list_all_interactions():
        learner = db.get_learner(it["learner_id"])
        rows.append({
            **it,
            "learner_name": learner["name"] if learner else "?",
            "course_name": course_name(learner["course_id"]) if learner else "?",
            "cohort": learner.get("cohort") if learner else None,
            "condition": learner.get("condition") if learner else None,
            "skill_name": skill_name(it["skill_id"]),
            "created_at_iso": datetime.fromtimestamp(it["created_at"]).isoformat(),
            "answered_at_iso": datetime.fromtimestamp(it["answered_at"]).isoformat() if it["answered_at"] else "",
        })
    columns = ["id", "learner_id", "learner_name", "course_name", "cohort", "condition",
               "skill_id", "skill_name", "turn_index", "modality", "difficulty", "is_review",
               "scenario", "question", "correct_index", "chosen_index", "is_correct",
               "response_time_ms", "confidence_score", "review_reason", "safety_flag",
               "safety_reason", "review_status", "retrieval_score", "insufficient_context",
               "created_at_iso", "answered_at_iso"]
    return _csv_response(rows, columns, "interactions.csv")


@app.get("/api/instructor/export/mastery.csv")
def export_mastery():
    from app.adaptive_engine import course_name, skill_name, SKILLS_BY_ID
    from app.learner_model import beta_uncertainty
    rows = []
    for state in db.list_all_skill_states():
        learner = db.get_learner(state["learner_id"])
        rows.append({
            **state,
            "learner_name": learner["name"] if learner else "?",
            "course_name": course_name(learner["course_id"]) if learner else "?",
            "cohort": learner.get("cohort") if learner else None,
            "condition": learner.get("condition") if learner else None,
            "skill_name": skill_name(state["skill_id"]),
            "unit": SKILLS_BY_ID.get(state["skill_id"], {}).get("unit"),
            "uncertainty": round(beta_uncertainty(state["beta_a"], state["beta_b"]), 4),
        })
    columns = ["learner_id", "learner_name", "course_name", "cohort", "condition", "skill_id",
               "skill_name", "unit", "p_mastery", "uncertainty", "attempts", "correct_streak",
               "incorrect_streak", "flagged_misconception", "due_in_turns", "last_seen_turn"]
    return _csv_response(rows, columns, "mastery.csv")


@app.get("/api/instructor/export/alerts.csv")
def export_alerts():
    from app.adaptive_engine import skill_name
    rows = []
    for a in db.list_alerts(unresolved_only=False):
        learner = db.get_learner(a["learner_id"])
        rows.append({
            **a,
            "learner_name": learner["name"] if learner else "?",
            "skill_name": skill_name(a["skill_id"]),
            "created_at_iso": datetime.fromtimestamp(a["created_at"]).isoformat(),
        })
    columns = ["id", "learner_id", "learner_name", "skill_id", "skill_name", "message",
               "created_at_iso", "resolved"]
    return _csv_response(rows, columns, "alerts.csv")


@app.get("/api/instructor/export/corpus_coverage.csv")
def export_corpus_coverage():
    """O3: per-skill corpus size vs. generation-failure rate, so a thin
    corpus file can be directly correlated with insufficient_context alerts
    rather than asserted qualitatively."""
    from app.adaptive_engine import course_name
    from app.rag import corpus_word_count
    stats = db.skill_generation_stats()
    rows = []
    for s in SKILLS:
        st = stats.get(s["id"], {"total": 0, "insufficient": 0})
        total, insufficient = st["total"], st["insufficient"]
        rows.append({
            "skill_id": s["id"], "skill_name": s["name"], "unit": s["unit"],
            "course_name": course_name(s["course_id"]),
            "corpus_word_count": corpus_word_count(s["id"]),
            "generation_attempts": total,
            "insufficient_context_count": insufficient,
            "insufficient_context_rate": round(insufficient / total, 3) if total else "",
        })
    columns = ["skill_id", "skill_name", "unit", "course_name", "corpus_word_count",
               "generation_attempts", "insufficient_context_count", "insufficient_context_rate"]
    return _csv_response(rows, columns, "corpus_coverage.csv")


@app.get("/api/instructor/learner/{learner_id}")
def learner_detail(learner_id: int):
    from app.adaptive_engine import course_name, default_course_id
    learner = db.get_learner(learner_id)
    if not learner:
        raise HTTPException(404, "Learner not found")
    course_id = learner.get("course_id") or default_course_id()
    states = {s["skill_id"]: s for s in db.get_all_skill_states(learner_id)}
    skills_out = []
    for s in SKILLS:
        if s["course_id"] != course_id:
            continue
        st = states.get(s["id"])
        skills_out.append({
            "id": s["id"], "name": s["name"], "unit": s["unit"],
            "mastery": round(st["p_mastery"], 2) if st else None,
            "attempts": st["attempts"] if st else 0,
            "flagged": bool(st["flagged_misconception"]) if st else False,
            "due_in_turns": st["due_in_turns"] if st else None,
        })
    history = db.get_all_interactions_for_learner(learner_id)
    return {
        "learner": {**learner, "course_name": course_name(course_id)},
        "skills": skills_out, "history": history,
    }