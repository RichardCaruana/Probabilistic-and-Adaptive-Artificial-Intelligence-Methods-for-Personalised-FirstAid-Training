# Adaptive First-Aid Training

Implementation accompanying _Probabilistic and Adaptive Artificial
Intelligence Methods for Personalised First-Aid Training_. The system
realises all four proposed objectives (O1–O4) at a scope suitable for a
solo MSc project: buildable, testable on real participants.

## Overview

The system consists of two client-facing surfaces backed by a shared
adaptive-learning core:

- **Learner client** — participants join with a name and optional
  profession, then answer AI-generated, scenario-based multiple-choice
  questions grounded in the course's own Level 4 first-aid materials, with
  immediate feedback.
- **Instructor dashboard** — sets the topic(s) currently in scope
  (Instructor Context Filter), surfaces per-skill mastery for each
  learner, and reports real-time alerts (misconceptions, safety-filter
  flags, corpus coverage gaps).

A QR code links directly to the learner join page, for embedding in
printed course materials.

## Setup

```bash
cd prototype
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

export ANTHROPIC_API_KEY=sk-ant-...

python scripts/ingest.py         # builds the RAG index from data/corpus/ (run once, or after editing corpus)
python scripts/test_harness.py   # sanity-checks the adaptive engine + learner model (no API key needed)

uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Then open `http://localhost:8000` — or, for a multi-participant session,
`http://<host-LAN-IP>:8000` (via `ipconfig`/`ifconfig`) so learners can
scan the QR code shown on the instructor dashboard from their own
devices.

## Corpus construction

The RAG corpus was derived from source course material as follows:

- **First_aid_part_1_pptx.pdf** and **First_aid_part_2 (2).pptx** — the
  two official Level 4 Emergency First Aid at Work units — were split
  along their existing **Learning Outcome** boundaries into 17 skill
  files under `data/corpus/`. This 17-skill split constitutes the
  taxonomy over which the probabilistic learner model tracks mastery
  (see `data/skills.json`).
- **BLS-3.pptx** (ERRC's BLS kit/equipment orientation) was excluded from
  the RAG corpus: it is operational kit content rather than procedural
  first-aid guidance and does not fit the skill taxonomy or the
  safety-grounding requirement. It remains available as a candidate for a
  separate "equipment familiarisation" module. This exclusion is a
  scoping decision and is documented here for citation in the methodology
  chapter.

Several skill files are thin (e.g. Choking, ≈250 words); expanding these
for future work is recommended. To extend a skill, append cleaned text
to the corresponding `data/corpus/*.txt` file and re-run
`python scripts/ingest.py`.

## Architecture against the stated objectives ++++

| Objective                        | File(s)                                                        | Implementation                                                                                               | Deviation from report                                           |
| -------------------------------- | -------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------- |
| O1 — Probabilistic learner model | `app/learner_model.py`                                         | Bayesian Knowledge Tracing (guess/slip-aware mastery) with Beta pseudo-counts for uncertainty                | Yes — BKT in place of DKT (see rationale below)                 |
| O2 — Adaptive strategy layer     | `app/adaptive_engine.py`                                       | Topic selection, difficulty selection, error-pattern detection, spaced repetition, instructor context filter | None — same five mechanisms, rule-based implementation          |
| O3 — Grounded content generation | `app/rag.py`, `app/llm.py`                                     | TF-IDF retrieval, CRAG-lite relevance gating, Claude-based generation, safety filter                         | Yes — TF-IDF in place of dense embeddings (justification below) |
| O4 — Deployment and evaluation   | `scripts/test_harness.py`, instructor dashboard, this document | Black-box test harness for pre-deployment QA, real-time dashboard for the live pilot                         | None                                                            |

### Rationale for the O1 simplification

The report specifies a DKT-based continuous knowledge-state vector. DKT
is a neural sequence model requiring a corpus of prior learner
interaction sequences for training, which does not yet exist for this
domain — a constraint the report's own O1 evaluation section
acknowledges ("no gold-standard dataset exists for modelling learner
behaviour in first-aid education"). BKT is the classical member of the
same probabilistic family DKT extends: it yields a mastery estimate with
associated uncertainty per skill, updates online without a training
phase, and is more tractable to validate and defend at pilot scale (small
_n_). Interaction logs collected during this pilot provide a
well-motivated basis for training a DKT model as subsequent work.

### Rationale for the O3 simplification

TF-IDF retrieval is used in place of dense embeddings. This avoids a
model-download dependency and is defensible at the current corpus size:
the material is small and structurally regular (bounded by explicit
Learning Outcome divisions), conditions under which sparse lexical
retrieval performs comparably to dense retrieval. A larger or
less-structured corpus would motivate revisiting this choice.

## Human-in-the-loop review and patient simulator

Two extensions beyond the original O1–O4 implementation:

**Review mode** (instructor dashboard, toggle). When enabled, every
AI-generated question or simulation step is held in a queue pending
instructor approval — with optional edits — or rejection. When disabled
(default), generation proceeds automatically and safety-flagged content
is logged as an after-the-fact alert rather than gated. Enabling review
mode for the pilot session allows all generated content to be inspected
before a participant sees it.

**Patient simulator**, a second learner modality alongside single-item
questions. The learner works through a 3–5 step case: an initial patient
presentation, a structured action menu at each decision point (choice is
constrained rather than free-text, to keep every step gradable and
corpus-grounded), and a narrated outcome carrying the learner to the next
step, concluding in a case resolution and score. Each decision updates
the same BKT mastery model used for single questions, tagged
`modality='simulation'` in the database, so dashboard and results queries
treat both modalities uniformly.

Both modalities respect review mode; further detail is provided in the
code manual.

## Multi-course support

The system supports multiple courses running concurrently — for example,
Level 4 Emergency First Aid alongside a Trauma First Responder (TFR)
course — with learners selecting a course at join time and instructors
setting topic scope independently per course.

- `data/courses.json` is the course registry.
- Every entry in `data/skills.json` carries a `course_id`.
- `scripts/onboard_course.py` adds a new course: it extracts text from
  the source deck, proposes a skill-boundary split (subject to a
  mandatory review step before any change is applied to existing data),
  and rebuilds the search index:

  ```bash
  python scripts/onboard_course.py extract --course-name "Trauma First Responder" \
      --files TFR1.pptx TFR2.pptx --out data/pending_tfr
  # review/edit data/pending_tfr/boundaries.json, then:
  python scripts/onboard_course.py apply --pending data/pending_tfr
  ```

  Boundary-detection internals and tuning guidance for differently
  structured decks are covered in the code manual, §17.

## Pilot testing protocol

1. Run `python scripts/ingest.py` after reviewing and, where needed,
   expanding thinner corpus files.
2. Start the server and open the instructor dashboard; optionally set a
   topic scope matching the material being covered live.
3. Have participants scan the QR code (or visit `/learner` directly) and
   complete a set of questions.
4. Monitor the Alerts panel for misconception and safety-filter flags in
   real time. `insufficient_context` alerts identify skills whose corpus
   file requires further material before a longer pilot.
5. All interaction data is logged to `data/app.db` (SQLite). Every
   interaction, mastery trajectory, and flag is queryable for the results
   chapter, e.g.:

   ```bash
   sqlite3 data/app.db "SELECT * FROM interactions WHERE learner_id = 1;"
   ```

## Testing

Two layers of automated verification:

- `python scripts/test_harness.py` — black-box simulation of the BKT
  computations (no server or API key required).
- `pytest` — exercises the FastAPI routes (join, quiz loop, review-mode
  gating, patient simulator, instructor dashboard, CSV export) against an
  isolated on-disk test database, leaving `data/app.db` untouched:

  ```bash
  pip install -r requirements-dev.txt
  pytest
  ```

## Data export

The instructor dashboard's "Export Data" card provides three CSV
downloads, also reachable directly:

- `/api/instructor/export/interactions.csv` — every MCQ/simulation turn
- `/api/instructor/export/mastery.csv` — current per-learner, per-skill
  BKT mastery and uncertainty
- `/api/instructor/export/alerts.csv` — misconception, safety, and
  corpus-gap alerts

These exports are the intended source for results-chapter tables and
figures, in preference to direct SQLite queries.
