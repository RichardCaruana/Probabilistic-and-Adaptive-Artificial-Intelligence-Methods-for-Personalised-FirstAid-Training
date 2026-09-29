# Adaptive First-Aid Training

Source code accompanying the MSc dissertation
**_Probabilistic and Adaptive Artificial Intelligence Methods for Personalised First-Aid Training_**
(Richard Caruana, University of Malta).

The system is a web-based training tool. It generates scenario-based first-aid questions and patient simulations with a large language model, grounds them in the course's own Level 4 first-aid material, and adapts what each learner sees using a probabilistic learner model.

---

## Contents

- [Overview](#overview)
- [Key features](#key-features)
- [Repository structure](#repository-structure)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Using the system](#using-the-system)
- [Testing](#testing)
- [Data export](#data-export)
- [Corpus construction](#corpus-construction)
- [Design decisions](#design-decisions)
- [Multi-course support](#multi-course-support)
- [Data and privacy](#data-and-privacy)

---

## Overview

The system has two client-facing surfaces backed by a shared adaptive-learning core:

- **Learner client:** participants join with a name and optional profession, choose a course, and answer AI-generated, scenario-based multiple-choice questions with immediate feedback. They can also work through step-by-step patient simulations.
- **Instructor dashboard:** sets the topic(s) currently in scope (the Instructor Context Filter), shows per-skill mastery for each learner, reports real-time alerts (misconceptions, safety-filter flags, corpus coverage gaps), and optionally reviews AI-generated content before learners see it.

A QR code on the instructor dashboard links directly to the learner join page, so participants can join from their own phones during a live session. It can also be printed in course materials.

The implementation maps onto the four dissertation objectives:

| Objective | Main file(s) | Implementation |
|---|---|---|
| **O1:** Probabilistic learner model | `app/learner_model.py` | Bayesian Knowledge Tracing (guess/slip-aware mastery) with Beta pseudo-counts for uncertainty |
| **O2:** Adaptive strategy layer | `app/adaptive_engine.py` | Topic selection, difficulty selection, error-pattern detection, spaced repetition, instructor context filter |
| **O3:** Grounded content generation | `app/rag.py`, `app/llm.py` | TF-IDF retrieval, CRAG-lite relevance gating, Claude-based generation, safety filter |
| **O4:** Deployment and evaluation | `scripts/test_harness.py`, `tests/`, `evaluation/`, instructor dashboard | Pre-deployment test harness, automated route tests, expert content-rating materials, live pilot dashboard |

---

## Key features

- **Adaptive question selection:** chooses the next skill and difficulty from each learner's estimated mastery, and uses spaced repetition to revisit weaker skills.
- **Retrieval-augmented generation:** every question is generated from passages retrieved from the course corpus and carries a citation to its source skill.
- **Safety filter and confidence scoring:** potentially unsafe or poorly grounded content is flagged to the instructor.
- **Review mode** (instructor toggle): when enabled, every AI-generated question or simulation step waits in a queue until the instructor approves it (with optional edits) or rejects it. When disabled (the default), content is delivered automatically and safety flags are logged as alerts.
- **Patient simulator:** a second learner modality. The learner works through a 3–5 step case with an initial patient presentation, a constrained action menu at each decision point, and a narrated outcome, ending in a case resolution and score. Each decision updates the same BKT mastery model as single questions, tagged `modality='simulation'`, so dashboards and exports treat both modalities uniformly.
- **CSV export** of all interactions, mastery estimates, alerts and corpus coverage for analysis.

---

## Repository structure

```
.
├── app/                    # FastAPI application
│   ├── main.py             # Web routes (learner, instructor, API, CSV export)
│   ├── adaptive_engine.py  # O2: topic/difficulty selection, spaced repetition
│   ├── learner_model.py    # O1: Bayesian Knowledge Tracing
│   ├── rag.py              # O3: TF-IDF retrieval over the corpus
│   ├── llm.py              # O3: question/simulation generation + safety filter (Anthropic Claude)
│   └── db.py               # SQLite schema and data access
├── data/
│   ├── corpus/             # 17 skill text files derived from the Level 4 course material
│   ├── skills.json         # Skill taxonomy (one entry per corpus file, with course_id)
│   ├── courses.json        # Course registry
│   └── pending_tfr/        # Extracted Trauma First Responder material awaiting review (not yet active)
├── evaluation/             # Expert content-quality evaluation (see evaluation/README.md)
├── scripts/
│   ├── ingest.py           # Builds the retrieval index from data/corpus/
│   └── test_harness.py     # Black-box checks of the adaptive engine and learner model
├── static/                 # Front-end JavaScript and CSS (learner.js, instructor.js, style.css)
├── templates/              # HTML pages (index, learner, instructor)
├── tests/                  # pytest suite for the web routes
├── requirements.txt        # Runtime dependencies
├── requirements-dev.txt    # Test dependencies
└── pytest.ini
```

The SQLite database (`data/app.db`) and the retrieval index (`data/index/tfidf_index.pkl`) are **not** included. Both are created automatically, as described below.

---

## Requirements

- **Python 3.9 or newer**
- **An Anthropic API key**, used to generate questions and simulations. The test harness and the automated tests run without one.
- A modern web browser

---

## Quick start

From the root of this repository:

```bash
# 1. Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Provide your Anthropic API key (either option works)
export ANTHROPIC_API_KEY=sk-ant-...          # option A: environment variable
# echo "ANTHROPIC_API_KEY=sk-ant-..." > .env # option B: .env file in the project root

# 4. Build the retrieval index from data/corpus/ (run once, or after editing the corpus)
python scripts/ingest.py

# 5. (Optional) Sanity-check the adaptive engine and learner model (no API key needed)
python scripts/test_harness.py

# 6. Start the server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Then open **http://localhost:8000** in a browser. The database is created automatically on first start.

For a multi-participant session, have learners open `http://<host-LAN-IP>:8000` from their own devices (find the IP with `ifconfig` on macOS/Linux or `ipconfig` on Windows), or scan the QR code on the instructor dashboard.

---

## Using the system

| Page | URL | Purpose |
|---|---|---|
| Home | `/` | Links to the learner and instructor views |
| Learner | `/learner` | Join, answer questions, run patient simulations |
| Instructor | `/instructor` | Topic scope, learner mastery, alerts, review queue, data export, QR code |

A typical session:

1. Open the instructor dashboard and, optionally, set a topic scope that matches the material being taught.
2. Optionally enable **Review mode** to approve every generated item before learners see it.
3. Participants scan the QR code (or visit `/learner`), join, and start answering questions or simulations.
4. Monitor the **Alerts** panel for misconceptions, safety-filter flags and `insufficient_context` warnings (skills whose corpus file needs more material).

---

## Testing

There are two layers of automated verification:

```bash
# Black-box simulation of the BKT computations (no server or API key required)
python scripts/test_harness.py

# FastAPI route tests: join, quiz loop, review-mode gating, patient simulator,
# instructor dashboard, CSV export. Uses an isolated test database.
pip install -r requirements-dev.txt
pytest
```

---

## Data export

The instructor dashboard's **Export Data** card provides CSV downloads, also reachable directly:

| Endpoint | Contents |
|---|---|
| `/api/instructor/export/interactions.csv` | Every question and simulation turn |
| `/api/instructor/export/mastery.csv` | Current per-learner, per-skill BKT mastery and uncertainty |
| `/api/instructor/export/alerts.csv` | Misconception, safety and corpus-gap alerts |
| `/api/instructor/export/corpus_coverage.csv` | Per-skill corpus size vs. `insufficient_context` generation-failure rate |

These exports are the intended source for results-chapter tables and figures.

---

## Corpus construction

The retrieval corpus in `data/corpus/` was derived from the source course material as follows:

- **First_aid_part_1_pptx.pdf** and **First_aid_part_2 (2).pptx** are the two official Level 4 Emergency First Aid at Work units. They were split along their existing **Learning Outcome** boundaries into 17 skill files. This 17-skill split is the taxonomy over which the learner model tracks mastery (see `data/skills.json`).
- **BLS-3.pptx** (ERRC's BLS kit/equipment orientation) was **excluded**. It covers operational kit rather than procedural first-aid guidance, so it does not fit the skill taxonomy or the safety-grounding requirement. It remains a candidate for a separate "equipment familiarisation" module.

To extend a skill, append cleaned text to the corresponding `data/corpus/*.txt` file and re-run `python scripts/ingest.py`.

---

## Design decisions

**BKT instead of Deep Knowledge Tracing (O1).** DKT is a neural sequence model that needs a corpus of prior learner interaction sequences for training, and no such dataset exists for first-aid education. BKT belongs to the same probabilistic family: it gives a per-skill mastery estimate with uncertainty, updates online without a training phase, and is easier to validate at pilot scale. The interaction logs collected during the pilot provide a basis for training a DKT model in future work.

**TF-IDF instead of dense embeddings (O3).** The corpus is small and structurally regular (bounded by explicit Learning Outcome divisions). Under those conditions, sparse lexical retrieval performs comparably to dense retrieval and avoids a model-download dependency. A larger or less-structured corpus would justify revisiting this choice.

---

## Multi-course support

The data model supports several courses running at once, for example Level 4 Emergency First Aid alongside a Trauma First Responder (TFR) course. Learners pick a course when they join, and instructors set topic scope independently per course.

- `data/courses.json` is the course registry. It currently contains the Level 4 course only.
- Every entry in `data/skills.json` carries a `course_id`.
- `data/pending_tfr/` holds text extracted from the TFR slides and a proposed skill-boundary split. It must be reviewed before it is added as a second course.

---

## Data and privacy

No participant data is included in this repository. The pilot database (`data/app.db`) contains participant names and responses, so it has been deliberately excluded (see `.gitignore`). A fresh, empty database is created the first time the server starts.

