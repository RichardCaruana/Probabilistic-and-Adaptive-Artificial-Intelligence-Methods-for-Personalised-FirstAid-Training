# Instructor Content-Quality Evaluation

This folder is separate from the running application (`app/`) and from the
general participant questionnaire (which evaluates the system as a whole —
usability, perceived learning, personalisation, dashboard usefulness — and
whose results belong in the thesis's overall pilot evaluation).

This folder exists for one specific, narrower purpose: an **expert content
rubric**, rated only by qualified first-aid instructors, that produces
real quantitative metrics for Section 3.6.7 ("Content Generation
Evaluation") — factual accuracy, grounding, relevance, explanation
appropriateness, completeness, and hallucination occurrence.

## Two ways to build the sample — use `generate_review_sample.py`

There are two scripts here, producing the same output shape:

- **`generate_review_sample.py` (recommended, primary approach)** — calls
  the real generation pipeline (`app/rag.py` + `app/llm.py`) directly,
  fresh, to produce a systematic sample covering every skill across
  easy/medium/hard difficulty, plus a handful of simulations. This is the
  right choice when the question you're asking is "does the generation
  framework itself reliably produce good content" — you get complete,
  even coverage of the taxonomy and a reproducible sample, independent of
  how any one pilot session happened to go. It never touches `data/app.db`.
- **`export_review_sample.py` (optional, secondary)** — samples content
  that was actually logged during real pilot usage. Use this only if you
  specifically want to make a claim about the content real pilot learners
  received, as a complement to the main evaluation above. Its coverage
  depends on whatever the adaptive engine happened to serve during
  piloting, so some skills/difficulties may be thin or missing.

Unless you have a specific reason to evaluate pilot-served content
specifically, run `generate_review_sample.py`.

## Why a separate, blinded export

For the rubric to mean anything, instructors need to judge the generated
content on its own merits — not be influenced by the system's own opinion
of that content (`confidence_score`, `safety_flag`), and not see anything
about which learner saw the item or how they answered. Both scripts
produce a reduced view containing only what's needed to judge content
quality (scenario/question/options/explanation/citation, or the
simulation equivalent) with everything else stripped out. See each
script's own docstring for the exact field list kept vs. stripped.

## How to run it

```bash
cd TraningTool
source venv/bin/activate   # or however you normally activate it
export ANTHROPIC_API_KEY=sk-ant-...        # generate_review_sample.py calls the real API
python evaluation/generate_review_sample.py
```

This makes one real API call per generated item (roughly 17-25 calls with
the defaults) and does not write anything to `data/app.db`.

It produces:

- `evaluation/output/review_sample.json` — the blinded item bank. This is
  what gets turned into the instructor rating sheets.
- `evaluation/output/answer_key.csv` — **researcher only, never share this
  with a rater.** Maps each blinded item back to its real database row and
  the system's own signals (confidence score, safety flag, retrieval
  score), so you can later analyse things like "did instructor-judged
  accuracy correlate with the system's own confidence score?"

## Next step

Once you've run the script, share `review_sample.json` back and the two
instructor rating-sheet documents (one per instructor, same items) will be
generated from your real sampled content, replacing the placeholder items
in the current draft rating sheet.

## Suggested procedure with your two instructors

1. Both instructors rate the **same** set of blinded items independently
   (don't let them compare notes while rating) — this is what lets you
   report inter-rater agreement rather than a single opinion.
2. Keep `answer_key.csv` on your machine only.
3. Once both rating sheets come back, you can compute: mean score per
   rubric criterion, the proportion of items flagged for hallucinated
   content, agreement between the two instructors (e.g. exact-agreement
   rate or Cohen's kappa per criterion), and — using the answer key — how
   instructor ratings relate to the system's own confidence score.
