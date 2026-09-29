"""
Grounded Content Generation (Objective O3) - generation + safety filter half.
Retrieval lives in rag.py; this module conditions Claude on the retrieved
passages, enforces the required output structure (scenario, MCQ, cited
explanation), and applies a post-generation safety filter before anything
reaches a learner.
"""

from __future__ import annotations
import json
import os
import re

from anthropic import Anthropic

try:
    # RELEVANCE_THRESHOLD is the single source of truth in rag.py (§8) --
    # imported here so confidence scoring stays consistent with retrieval.
    from .rag import RELEVANCE_THRESHOLD
except ImportError:
    # Fallback if the package layout differs from app.rag -- keep this in
    # sync with rag.py's own value if you ever change it there.
    RELEVANCE_THRESHOLD = 0.08

MODEL = "claude-sonnet-4-5"
_client = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Export it before starting the server, "
                "e.g. `export ANTHROPIC_API_KEY=sk-ant-...`"
            )
        _client = Anthropic(api_key=api_key)
    return _client


SYSTEM_PROMPT = """You are a content generator for a Level 4 Emergency First Aid at Work \
training tool. You must generate ONE scenario-based multiple-choice question \
strictly grounded in the SOURCE MATERIAL provided below. Do not use any \
first-aid knowledge from outside the source material, even if you believe it \
to be correct -- the source material is the sole authority here because \
outputs are used to train real first responders.

Rules:
- The scenario must be a short, realistic situation a first responder could \
encounter, tailored to the learner's stated profession/context if given.
- Exactly 4 answer options, exactly one correct.
- The explanation MUST cite the specific procedural step from the source \
material that justifies the correct answer (paraphrase it, don't just \
assert the answer).
- If the source material does not contain enough information to safely \
support a question at the requested difficulty, set "insufficient_context" \
to true and explain why, rather than inventing content.
- Never include specific drug dosages, brand names, or any procedure not \
explicitly present in the source material.
- Respond with ONLY a JSON object, no markdown fences, no preamble, matching \
this schema exactly:
{
  "insufficient_context": false,
  "scenario": "...",
  "question": "...",
  "options": ["...", "...", "...", "..."],
  "correct_index": 0,
  "explanation": "...",
  "source_citation": "short quote or paraphrase of the specific step used"
}
"""

# Keyword list a generated explanation should never contain unless present
# verbatim in the retrieved passages -- catches the LLM reaching for
# out-of-corpus specifics (dosages, drug names) even when told not to.
DANGEROUS_PATTERNS = [
    r"\b\d+\s?(mg|ml|mcg|units?)\b",  # dosage-like numbers
    r"\bepinephrine\b(?!.*auto-injector)",  # bare drug name without the corpus's own phrasing
]

RETRIEVAL_WEIGHT = 0.4
CITATION_WEIGHT = 0.3
SAFETY_WEIGHT = 0.3


def compute_confidence(retrieval_score, safety_passed, citation_overlap=None):
    """
    retrieval_score: best TF-IDF match score from rag.py for this generation
    safety_passed: bool, from the dangerous-pattern scan
    citation_overlap: 0-1, from the vocabulary-overlap check (MCQs only --
        simulations have no single source_citation field, so this is
        optional; when omitted the citation weight is redistributed across
        the other two signals rather than silently zeroing it out).
    """
    retrieval_component = min(retrieval_score / RELEVANCE_THRESHOLD, 1.0) if retrieval_score is not None else 0.0
    safety_component = 1.0 if safety_passed else 0.0

    if citation_overlap is not None:
        confidence = (
            RETRIEVAL_WEIGHT * retrieval_component +
            CITATION_WEIGHT * citation_overlap +
            SAFETY_WEIGHT * safety_component
        )
    else:
        total_weight = RETRIEVAL_WEIGHT + SAFETY_WEIGHT
        confidence = (
            (RETRIEVAL_WEIGHT / total_weight) * retrieval_component +
            (SAFETY_WEIGHT / total_weight) * safety_component
        )

    return round(confidence, 2)


def generate_scenario_question(
    skill_name: str,
    difficulty: str,
    passages: list[dict],
    profession: str | None,
    is_review: bool,
    retrieval_score: float = 0.0,
) -> dict:
    if not passages:
        return {
            "insufficient_context": True,
            "scenario": "", "question": "", "options": [], "correct_index": 0,
            "explanation": "No retrieved content available for this skill.",
            "source_citation": "",
            "safety_flag": False,
            "safety_reason": None,
            "confidence_score": 0.0,
        }

    context_block = "\n\n---\n\n".join(p["text"] for p in passages)
    profession_line = f"The learner's profession/context is: {profession}." if profession else \
        "No specific profession context was given -- use a generic workplace setting."
    review_line = (
        "This is a SPACED-REPETITION REVIEW question -- keep it concise since the "
        "learner has seen this topic before."
        if is_review else ""
    )

    user_prompt = f"""SOURCE MATERIAL (skill: {skill_name}):
{context_block}

Generate a {difficulty}-difficulty scenario-based question for the skill "{skill_name}".
{profession_line}
{review_line}
"""

    client = _get_client()
    response = client.messages.create(
        model=MODEL,
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    raw_text = "".join(block.text for block in response.content if block.type == "text")
    return _parse_and_filter(raw_text, context_block, retrieval_score)


SIMULATION_SYSTEM_PROMPT = """You are a content generator for a Level 4 Emergency First Aid at Work \
training tool. You must generate an interactive PATIENT SIMULATION: a linear \
sequence of decision points where a first responder must choose the correct \
next action, strictly grounded in the SOURCE MATERIAL provided below. Do not \
use any first-aid knowledge from outside the source material, even if you \
believe it to be correct -- the source material is the sole authority here \
because outputs are used to train real first responders.

Rules:
- Produce between 3 and 5 sequential steps, each representing one decision \
point in managing this casualty, in the correct clinical order per the \
source material (e.g. assess -> intervene -> reassess -> escalate).
- Each step must offer exactly 3 or 4 action options. Exactly ONE action per \
step must be marked "correct": true -- the single best next action per the \
source material at that point in the case. The others are plausible but \
wrong (e.g. premature, out of order, or actively unhelpful), never options \
that would be obviously silly to a learner.
- Every action (correct or not) needs an "outcome" -- a short, grounded \
narration of what happens to the patient as a direct result of that choice, \
consistent with the source material. If a wrong action is chosen, the \
outcome should show the patient's condition failing to improve or mildly \
worsening (never anything gratuitously graphic), not simply "wrong, try \
again" -- this is what makes the patient feel like they're actually \
responding to the learner's choices.
- Tailor the scenario setting to the learner's stated profession/context if \
given.
- Never include specific drug dosages, brand names, or any procedure not \
explicitly present in the source material.
- If the source material does not contain enough sequential procedural \
detail to safely support a multi-step simulation at the requested \
difficulty, set "insufficient_context" to true and explain why, rather than \
inventing steps.
- Respond with ONLY a JSON object, no markdown fences, no preamble, matching \
this schema exactly:
{
  "insufficient_context": false,
  "patient_intro": "short realistic scene-setting description of the patient and situation",
  "steps": [
    {
      "prompt": "what does the learner do at this point?",
      "actions": [
        {"label": "...", "correct": true, "outcome": "..."},
        {"label": "...", "correct": false, "outcome": "..."}
      ]
    }
  ],
  "resolution": "short narration of the case's final outcome once all correct-path steps are followed"
}
"""


def generate_simulation(
    skill_name: str,
    difficulty: str,
    passages: list[dict],
    profession: str | None,
    retrieval_score: float = 0.0,
) -> dict:
    if not passages:
        return {
            "insufficient_context": True,
            "patient_intro": "", "steps": [], "resolution": "",
            "explanation": "No retrieved content available for this skill.",
            "safety_flag": False,
            "safety_reason": None,
            "confidence_score": 0.0,
        }

    context_block = "\n\n---\n\n".join(p["text"] for p in passages)
    profession_line = f"The learner's profession/context is: {profession}." if profession else \
        "No specific profession context was given -- use a generic workplace setting."

    user_prompt = f"""SOURCE MATERIAL (skill: {skill_name}):
{context_block}

Generate a {difficulty}-difficulty patient simulation for the skill "{skill_name}".
{profession_line}
"""

    client = _get_client()
    response = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=SIMULATION_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    raw_text = "".join(block.text for block in response.content if block.type == "text")
    return _parse_and_filter_simulation(raw_text, context_block, retrieval_score)


def _parse_and_filter(raw_text: str, context_block: str, retrieval_score: float = 0.0) -> dict:
    cleaned = re.sub(r"^```json|```$", "", raw_text.strip(), flags=re.MULTILINE).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return {
            "insufficient_context": True,
            "scenario": "", "question": "", "options": [], "correct_index": 0,
            "explanation": "Generation failed to produce valid structured output.",
            "source_citation": "", "safety_flag": True,
            "safety_reason": "malformed_json",
            "confidence_score": 0.0,
        }

    data.setdefault("safety_flag", False)
    data.setdefault("safety_reason", None)

    if data.get("insufficient_context"):
        data.setdefault("confidence_score", 0.0)
        return data

    # Structural checks
    if len(data.get("options", [])) != 4 or not (0 <= data.get("correct_index", -1) < 4):
        data["safety_flag"] = True
        data["safety_reason"] = "malformed_options"
        data["confidence_score"] = compute_confidence(retrieval_score, safety_passed=False)
        return data

    # Dangerous-content pattern check on the explanation
    explanation = data.get("explanation", "")
    for pattern in DANGEROUS_PATTERNS:
        match = re.search(pattern, explanation, re.IGNORECASE)
        if match and match.group(0).lower() not in context_block.lower():
            data["safety_flag"] = True
            data["safety_reason"] = f"out-of-corpus specific detail: '{match.group(0)}'"
            data["confidence_score"] = compute_confidence(retrieval_score, safety_passed=False)
            return data

    # Citation grounding sanity check: citation should share vocabulary with
    # the retrieved context (very light-weight check -- not a substitute for
    # the instructor spot-review the report specifies under O3/O4 evaluation).
    citation = data.get("source_citation", "")
    citation_words = set(re.findall(r"[a-z]{4,}", citation.lower()))
    context_words = set(re.findall(r"[a-z]{4,}", context_block.lower()))
    citation_overlap = len(citation_words & context_words) / max(len(citation_words), 1)

    safety_passed = True
    if citation_overlap < 0.3:
        data["safety_flag"] = True
        data["safety_reason"] = "citation does not clearly match retrieved source material"
        safety_passed = False

    data["citation_overlap"] = round(citation_overlap, 2)
    data["confidence_score"] = compute_confidence(
        retrieval_score, safety_passed=safety_passed, citation_overlap=citation_overlap
    )
    return data


def _parse_and_filter_simulation(raw_text: str, context_block: str, retrieval_score: float = 0.0) -> dict:
    cleaned = re.sub(r"^```json|```$", "", raw_text.strip(), flags=re.MULTILINE).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return {
            "insufficient_context": True, "patient_intro": "", "steps": [], "resolution": "",
            "safety_flag": True, "safety_reason": "malformed_json",
            "confidence_score": 0.0,
        }

    data.setdefault("safety_flag", False)
    data.setdefault("safety_reason", None)

    if data.get("insufficient_context"):
        data.setdefault("confidence_score", 0.0)
        return data

    steps = data.get("steps", [])
    if not (3 <= len(steps) <= 5):
        data["safety_flag"] = True
        data["safety_reason"] = f"unexpected step count: {len(steps)}"
        data["confidence_score"] = compute_confidence(retrieval_score, safety_passed=False)
        return data

    full_text_for_scan = data.get("patient_intro", "") + " " + data.get("resolution", "")
    for step in steps:
        actions = step.get("actions", [])
        if not (3 <= len(actions) <= 4):
            data["safety_flag"] = True
            data["safety_reason"] = "a step has an unexpected number of actions"
            data["confidence_score"] = compute_confidence(retrieval_score, safety_passed=False)
            return data
        correct_flags = [a.get("correct") for a in actions]
        if correct_flags.count(True) != 1:
            data["safety_flag"] = True
            data["safety_reason"] = "a step does not have exactly one correct action"
            data["confidence_score"] = compute_confidence(retrieval_score, safety_passed=False)
            return data
        full_text_for_scan += " " + step.get("prompt", "")
        full_text_for_scan += " " + " ".join(a.get("label", "") + " " + a.get("outcome", "") for a in actions)

    safety_passed = True
    for pattern in DANGEROUS_PATTERNS:
        match = re.search(pattern, full_text_for_scan, re.IGNORECASE)
        if match and match.group(0).lower() not in context_block.lower():
            data["safety_flag"] = True
            data["safety_reason"] = f"out-of-corpus specific detail: '{match.group(0)}'"
            safety_passed = False
            break

    # Simulations have no single source_citation field to check overlap
    # against, so confidence here is retrieval + safety only (see
    # compute_confidence's citation_overlap=None branch).
    data["confidence_score"] = compute_confidence(retrieval_score, safety_passed=safety_passed)
    return data