
"""
SQLite persistence layer for the first-aid adaptive learning.

"""
 
from __future__ import annotations
import os
import sqlite3
import time
import json
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path(os.environ.get("APP_DB_PATH") or (Path(__file__).parent.parent / "data" / "app.db"))
 
SCHEMA = """
CREATE TABLE IF NOT EXISTS learners (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    profession TEXT,
    course_id INTEGER,
    cohort TEXT,                                    -- optional session/cohort label (O4)
    condition TEXT NOT NULL DEFAULT 'adaptive',      -- 'adaptive' | 'random' (O2 evaluation baseline)
    created_at REAL NOT NULL
);
 
CREATE TABLE IF NOT EXISTS skill_state (
    learner_id INTEGER NOT NULL,
    skill_id INTEGER NOT NULL,
    p_mastery REAL NOT NULL DEFAULT 0.1,   -- BKT point estimate P(learned)
    beta_a REAL NOT NULL DEFAULT 1.0,      -- Beta pseudo-counts for uncertainty
    beta_b REAL NOT NULL DEFAULT 1.0,
    attempts INTEGER NOT NULL DEFAULT 0,
    correct_streak INTEGER NOT NULL DEFAULT 0,
    incorrect_streak INTEGER NOT NULL DEFAULT 0,
    due_in_turns INTEGER NOT NULL DEFAULT 0,   -- turn-based spaced repetition countdown
    last_seen_turn INTEGER NOT NULL DEFAULT 0,
    flagged_misconception INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (learner_id, skill_id)
);
 
CREATE TABLE IF NOT EXISTS interactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    learner_id INTEGER NOT NULL,
    skill_id INTEGER NOT NULL,
    turn_index INTEGER NOT NULL,
    difficulty TEXT NOT NULL,
    is_review INTEGER NOT NULL DEFAULT 0,
    modality TEXT NOT NULL DEFAULT 'mcq',
    scenario TEXT NOT NULL,
    question TEXT NOT NULL,
    options_json TEXT NOT NULL,
    correct_index INTEGER NOT NULL,
    explanation TEXT NOT NULL,
    citation TEXT,
    retrieval_score REAL,
    insufficient_context INTEGER NOT NULL DEFAULT 0,
    chosen_index INTEGER,
    is_correct INTEGER,
    response_time_ms INTEGER,
    safety_flag INTEGER NOT NULL DEFAULT 0,
    safety_reason TEXT,
    review_status TEXT NOT NULL DEFAULT 'approved',
    created_at REAL NOT NULL,
    answered_at REAL
);
 
CREATE TABLE IF NOT EXISTS simulations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    learner_id INTEGER NOT NULL,
    skill_id INTEGER NOT NULL,
    difficulty TEXT NOT NULL,
    patient_intro TEXT NOT NULL,
    steps_json TEXT NOT NULL,      -- full script incl. correct flags/outcomes; server-side only
    resolution TEXT,
    current_step INTEGER NOT NULL DEFAULT 0,
    correct_count INTEGER NOT NULL DEFAULT 0,
    total_steps INTEGER NOT NULL DEFAULT 0,
    completed INTEGER NOT NULL DEFAULT 0,
    safety_flag INTEGER NOT NULL DEFAULT 0,
    safety_reason TEXT,
    review_status TEXT NOT NULL DEFAULT 'approved',
    created_at REAL NOT NULL
);
 
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    learner_id INTEGER NOT NULL,
    skill_id INTEGER NOT NULL,
    message TEXT NOT NULL,
    created_at REAL NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0
);
 
CREATE TABLE IF NOT EXISTS session_context (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    active_skill_ids TEXT NOT NULL DEFAULT '[]',
    review_mode TEXT NOT NULL DEFAULT 'off',        -- 'off' | 'always' | 'confidence'
    confidence_threshold REAL NOT NULL DEFAULT 0.85
);
 
CREATE TABLE IF NOT EXISTS course_scope (
    course_id INTEGER PRIMARY KEY,
    active_skill_ids TEXT NOT NULL DEFAULT '[]'
);
"""
 
 
@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
 
 
def _column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)
 
 
def _add_column_if_missing(conn, table, column, coltype, default=None):
    if not _column_exists(conn, table, column):
        default_clause = f" DEFAULT {default}" if default is not None else ""
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}{default_clause}")
 
 
def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
 
        # --- Migrations for people upgrading an existing data/app.db ---
        # CREATE TABLE IF NOT EXISTS won't add columns to a table that
        # already existed from a previous version of this schema, so any
        # new columns need an explicit, idempotent ALTER TABLE.
 
        # multi-course support (existing)
        if not _column_exists(conn, "learners", "course_id"):
            conn.execute("ALTER TABLE learners ADD COLUMN course_id INTEGER")
 
        # O2 evaluation baseline + O4 cohort tagging (new)
        _add_column_if_missing(conn, "learners", "cohort", "TEXT")
        _add_column_if_missing(
            conn, "learners", "condition", "TEXT NOT NULL", default="'adaptive'"
        )

        # confidence-based review (new)
        _add_column_if_missing(conn, "interactions", "confidence_score", "REAL")
        _add_column_if_missing(conn, "interactions", "review_reason", "TEXT")
        _add_column_if_missing(conn, "simulations", "confidence_score", "REAL")
        _add_column_if_missing(conn, "simulations", "review_reason", "TEXT")
        _add_column_if_missing(
            conn, "session_context", "confidence_threshold", "REAL NOT NULL", default=0.85
        )
        # older DBs may still have review_mode as INTEGER 0/1 from before this
        # feature existed; if so, coerce the stored value onto the new
        # 'off'/'always' vocabulary rather than leaving a stray 0/1 in a TEXT
        # column (SQLite is dynamically typed so this won't crash, but it
        # would silently break the mode == 'always' / == 'confidence' checks
        # in main.py, so it's worth normalizing explicitly).
        row = conn.execute(
            "SELECT review_mode FROM session_context WHERE id = 1"
        ).fetchone()
        if row is not None and str(row["review_mode"]) in ("0", "1"):
            normalized = "always" if str(row["review_mode"]) == "1" else "off"
            conn.execute(
                "UPDATE session_context SET review_mode = ? WHERE id = 1", (normalized,)
            )
 
        cur = conn.execute("SELECT COUNT(*) c FROM session_context")
        if cur.fetchone()["c"] == 0:
            conn.execute(
                "INSERT INTO session_context (id, active_skill_ids, review_mode, confidence_threshold) "
                "VALUES (1, '[]', 'off', 0.85)"
            )
 
 
# ---------- Learners ----------
 
def create_learner(
    name: str,
    profession: str | None,
    course_id: int | None = None,
    cohort: str | None = None,
    condition: str = "adaptive",
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO learners (name, profession, course_id, cohort, condition, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, profession, course_id, cohort, condition, time.time()),
        )
        return cur.lastrowid
 
 
def get_learner(learner_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM learners WHERE id = ?", (learner_id,)).fetchone()
        return dict(row) if row else None
 
 
def list_learners():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM learners ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]
 
 
# ---------- Skill state ----------
 
def get_skill_state(learner_id: int, skill_id: int) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM skill_state WHERE learner_id=? AND skill_id=?",
            (learner_id, skill_id),
        ).fetchone()
        if row:
            return dict(row)
        conn.execute(
            "INSERT INTO skill_state (learner_id, skill_id) VALUES (?, ?)",
            (learner_id, skill_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM skill_state WHERE learner_id=? AND skill_id=?",
            (learner_id, skill_id),
        ).fetchone()
        return dict(row)
 
 
def get_all_skill_states(learner_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM skill_state WHERE learner_id=?", (learner_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def list_all_skill_states() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM skill_state ORDER BY learner_id, skill_id"
        ).fetchall()
        return [dict(r) for r in rows]
 
 
def upsert_skill_state(learner_id: int, skill_id: int, **fields):
    if not fields:
        return
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO skill_state (learner_id, skill_id) VALUES (?, ?) "
            "ON CONFLICT(learner_id, skill_id) DO NOTHING",
            (learner_id, skill_id),
        )
        cols = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [learner_id, skill_id]
        conn.execute(
            f"UPDATE skill_state SET {cols} WHERE learner_id=? AND skill_id=?", vals
        )
 
 
# ---------- Interactions ----------
 
def create_interaction(**fields) -> int:
    cols = ", ".join(fields.keys())
    placeholders = ", ".join("?" for _ in fields)
    with get_conn() as conn:
        cur = conn.execute(
            f"INSERT INTO interactions ({cols}) VALUES ({placeholders})",
            list(fields.values()),
        )
        return cur.lastrowid
 
 
def get_interaction(interaction_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM interactions WHERE id = ?", (interaction_id,)
        ).fetchone()
        return dict(row) if row else None
 
 
def update_interaction(interaction_id: int, **fields):
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [interaction_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE interactions SET {cols} WHERE id = ?", vals)
 
 
def get_recent_interactions(learner_id: int, skill_id: int, limit: int = 5):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM interactions WHERE learner_id=? AND skill_id=? "
            "AND answered_at IS NOT NULL ORDER BY id DESC LIMIT ?",
            (learner_id, skill_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
 
 
def get_all_interactions_for_learner(learner_id: int):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM interactions WHERE learner_id=? ORDER BY id ASC",
            (learner_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def list_all_interactions():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM interactions ORDER BY learner_id, id"
        ).fetchall()
        return [dict(r) for r in rows]
 
 
def count_turns(learner_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) c FROM interactions WHERE learner_id=?", (learner_id,)
        ).fetchone()
        return row["c"]


def skill_generation_stats() -> dict:
    """Per-skill generation attempt counts (O3 corpus-coverage export): how
    many generations were attempted for this skill, and how many came back
    insufficient_context -- lets that rate be correlated against corpus size."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT skill_id, COUNT(*) AS total, "
            "COALESCE(SUM(insufficient_context), 0) AS insufficient "
            "FROM interactions GROUP BY skill_id"
        ).fetchall()
        return {r["skill_id"]: {"total": r["total"], "insufficient": r["insufficient"]} for r in rows}
 
 
# ---------- Alerts ----------
 
def create_alert(learner_id: int, skill_id: int, message: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO alerts (learner_id, skill_id, message, created_at) VALUES (?, ?, ?, ?)",
            (learner_id, skill_id, message, time.time()),
        )
        return cur.lastrowid
 
 
def list_alerts(unresolved_only: bool = True):
    with get_conn() as conn:
        q = "SELECT * FROM alerts"
        if unresolved_only:
            q += " WHERE resolved = 0"
        q += " ORDER BY created_at DESC"
        rows = conn.execute(q).fetchall()
        return [dict(r) for r in rows]
 
 
def resolve_alert(alert_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE alerts SET resolved = 1 WHERE id = ?", (alert_id,))
 
 
# ---------- Session / instructor context filter ----------
 
def get_active_skill_ids() -> list[int]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT active_skill_ids FROM session_context WHERE id = 1"
        ).fetchone()
        return json.loads(row["active_skill_ids"]) if row else []
 
 
def set_active_skill_ids(skill_ids: list[int]):
    with get_conn() as conn:
        conn.execute(
            "UPDATE session_context SET active_skill_ids = ? WHERE id = 1",
            (json.dumps(skill_ids),),
        )
 
 
# ---------- Review mode (confidence-based human-in-the-loop) ----------
#
# Global (not per-course): one instructor dashboard, one review policy at a
# time. If you later want this per-course instead, this is the place to
# change — fold review_mode/confidence_threshold into course_scope and add
# a course_id parameter to both functions below.
 
def get_review_mode() -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT review_mode, confidence_threshold FROM session_context WHERE id = 1"
        ).fetchone()
        if row:
            return {"mode": row["review_mode"], "threshold": row["confidence_threshold"]}
        return {"mode": "off", "threshold": 0.85}
 
 
def set_review_mode(mode: str, threshold: float | None = None):
    assert mode in ("off", "always", "confidence"), f"invalid review mode: {mode}"
    with get_conn() as conn:
        if threshold is not None:
            conn.execute(
                "UPDATE session_context SET review_mode = ?, confidence_threshold = ? WHERE id = 1",
                (mode, threshold),
            )
        else:
            conn.execute(
                "UPDATE session_context SET review_mode = ? WHERE id = 1",
                (mode,),
            )
 
 
# ---------- Per-course topic scope (Instructor Context Filter, multi-course) ----------
 
def get_course_scope(course_id: int) -> list[int]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT active_skill_ids FROM course_scope WHERE course_id = ?", (course_id,)
        ).fetchone()
        return json.loads(row["active_skill_ids"]) if row else []
 
 
def set_course_scope(course_id: int, skill_ids: list[int]):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO course_scope (course_id, active_skill_ids) VALUES (?, ?) "
            "ON CONFLICT(course_id) DO UPDATE SET active_skill_ids = excluded.active_skill_ids",
            (course_id, json.dumps(skill_ids)),
        )
 
 
# ---------- Review queue (human-in-the-loop) ----------
 
def list_pending_interactions():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM interactions WHERE review_status = 'pending' ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]
 
 
def set_review_status(interaction_id: int, status: str, **edit_fields):
    """status is 'approved' or 'rejected'. edit_fields lets the instructor
    tweak question/options/explanation/citation before approving."""
    fields = {"review_status": status, **edit_fields}
    update_interaction(interaction_id, **fields)
 
 
# ---------- Patient Simulator ----------
 
def create_simulation(**fields) -> int:
    cols = ", ".join(fields.keys())
    placeholders = ", ".join("?" for _ in fields)
    with get_conn() as conn:
        cur = conn.execute(
            f"INSERT INTO simulations ({cols}) VALUES ({placeholders})",
            list(fields.values()),
        )
        return cur.lastrowid
 
 
def get_simulation(sim_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM simulations WHERE id = ?", (sim_id,)).fetchone()
        return dict(row) if row else None
 
 
def update_simulation(sim_id: int, **fields):
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [sim_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE simulations SET {cols} WHERE id = ?", vals)
 
 
def list_pending_simulations():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM simulations WHERE review_status = 'pending' ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]
 
 
def set_simulation_review_status(sim_id: int, status: str):
    update_simulation(sim_id, review_status=status)