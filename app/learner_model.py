"""
Probabilistic Learner Model (Objective O1)
-------------------------------------------
Per skill, per learner, we track:
  - p_mastery: BKT point estimate P(skill is learned)
  - beta_a, beta_b: pseudo-counts (successes, failures) -> Beta(a, b)
    used purely to quantify uncertainty (variance) independent of the
    guess/slip-corrected point estimate.
"""

from __future__ import annotations


GUESS = 0.20     # P(correct | not mastered)  -- ~1/4 chance for 4 options, minus scenario cues
SLIP = 0.10      # P(incorrect | mastered)    -- accounts for performance slips under scenario stress
P_TRANSIT = 0.25  # P(learns the skill after this practice opportunity, if not already mastered)

PRIOR_MASTERY = 0.15


def bkt_update(p_mastery: float, correct: bool) -> float:
    """Standard two-step BKT update: Bayesian posterior given evidence, then
    apply the learning-transition probability for the next opportunity."""
    if correct:
        num = p_mastery * (1 - SLIP)
        denom = num + (1 - p_mastery) * GUESS
    else:
        num = p_mastery * SLIP
        denom = num + (1 - p_mastery) * (1 - GUESS)

    posterior = num / denom if denom > 0 else p_mastery
    # Learning transition: even if not yet mastered, this practice opportunity
    # may have taught the skill.
    p_next = posterior + (1 - posterior) * P_TRANSIT
    return min(max(p_next, 0.01), 0.99)


def beta_update(beta_a: float, beta_b: float, correct: bool) -> tuple[float, float]:
    if correct:
        return beta_a + 1, beta_b
    return beta_a, beta_b + 1


def beta_uncertainty(beta_a: float, beta_b: float) -> float:
    """Variance of Beta(a, b), used as the uncertainty signal. Ranges (0, 0.25],
    highest when few observations have been made."""
    total = beta_a + beta_b
    return (beta_a * beta_b) / (total * total * (total + 1))


def apply_response(state: dict, correct: bool) -> dict:
    """Given a skill_state row (dict) and whether the response was correct,
    return the updated fields to persist."""
    new_mastery = bkt_update(state["p_mastery"], correct)
    new_a, new_b = beta_update(state["beta_a"], state["beta_b"], correct)
    uncertainty = beta_uncertainty(new_a, new_b)

    correct_streak = state["correct_streak"] + 1 if correct else 0
    incorrect_streak = 0 if correct else state["incorrect_streak"] + 1

    return {
        "p_mastery": new_mastery,
        "beta_a": new_a,
        "beta_b": new_b,
        "attempts": state["attempts"] + 1,
        "correct_streak": correct_streak,
        "incorrect_streak": incorrect_streak,
        "_uncertainty": uncertainty,  # not a column; adaptive engine reads this
    }


def difficulty_for_mastery(p_mastery: float) -> str:
    """O2 - Difficulty Selection: map mastery to a difficulty tier targeting
    the learner's zone of proximal development."""
    if p_mastery < 0.35:
        return "easy"
    if p_mastery < 0.70:
        return "medium"
    return "hard"
