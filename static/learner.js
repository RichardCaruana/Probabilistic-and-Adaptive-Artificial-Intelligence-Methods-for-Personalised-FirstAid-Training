let learnerId = null;
let currentInteractionId = null;
let questionStartTime = null;
let pendingPollTimer = null;

let currentSimId = null;
let simPendingPollTimer = null;
let lastSimActions = [];

const joinScreen = document.getElementById("join-screen");
const modeScreen = document.getElementById("mode-screen");
const questionScreen = document.getElementById("question-screen");
const feedbackScreen = document.getElementById("feedback-screen");
const loadingScreen = document.getElementById("loading");
const pendingScreen = document.getElementById("pending-screen");
const simIntroScreen = document.getElementById("sim-intro-screen");
const simOutcomeScreen = document.getElementById("sim-outcome-screen");
const simCompleteScreen = document.getElementById("sim-complete-screen");

const ALL_SCREENS = [
  joinScreen, modeScreen, questionScreen, feedbackScreen, loadingScreen,
  pendingScreen, simIntroScreen, simOutcomeScreen, simCompleteScreen,
];

function showOnly(el) {
  ALL_SCREENS.forEach(s => s.classList.add("hidden"));
  el.classList.remove("hidden");
}

function stopAllPolling() {
  if (pendingPollTimer) clearInterval(pendingPollTimer);
  if (simPendingPollTimer) clearInterval(simPendingPollTimer);
}

// ---------------- Join ----------------

// Session/cohort label (O4), set by the instructor's QR code as a query
// param so learners scanning it are auto-tagged with which session they joined.
const joinCohort = new URLSearchParams(window.location.search).get("cohort");

document.getElementById("join-btn").addEventListener("click", async () => {
  const name = document.getElementById("name-input").value.trim();
  const profession = document.getElementById("profession-input").value.trim();
  const errorEl = document.getElementById("join-error");
  if (!name) { errorEl.textContent = "Please enter your name."; return; }
  errorEl.textContent = "";

  try {
    const res = await fetch("/api/learner/join", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, profession, cohort: joinCohort }),
    });
    const data = await res.json();
    learnerId = data.learner_id;
    showOnly(modeScreen);
  } catch (e) {
    errorEl.textContent = "Could not join. Is the server running?";
  }
});

document.getElementById("mode-quiz-btn").addEventListener("click", loadNextQuestion);
document.getElementById("mode-sim-btn").addEventListener("click", startSimulation);
document.getElementById("sim-restart-btn").addEventListener("click", () => showOnly(modeScreen));

// ---------------- Quick Questions (MCQ) mode ----------------

async function loadNextQuestion() {
  stopAllPolling();
  showOnly(loadingScreen);
  try {
    const res = await fetch(`/api/learner/${learnerId}/next_question`);
    const data = await res.json();

    if (data.insufficient_context) {
      showOnly(questionScreen);
      document.getElementById("scenario-text").textContent = "";
      document.getElementById("question-text").textContent = data.message;
      document.getElementById("options-list").innerHTML = "";
      return;
    }

    if (data.pending_review) {
      currentInteractionId = data.interaction_id;
      showOnly(pendingScreen);
      startPendingPoll();
      return;
    }

    renderQuestion(data);
  } catch (e) {
    document.getElementById("q-error").textContent = "Failed to load question.";
    showOnly(questionScreen);
  }
}

function startPendingPoll() {
  if (pendingPollTimer) clearInterval(pendingPollTimer);
  pendingPollTimer = setInterval(async () => {
    try {
      const res = await fetch(`/api/learner/${learnerId}/question_status/${currentInteractionId}`);
      const data = await res.json();
      if (data.status === "approved") {
        clearInterval(pendingPollTimer);
        renderQuestion(data);
      } else if (data.status === "rejected") {
        clearInterval(pendingPollTimer);
        loadNextQuestion();
      }
    } catch (e) {
      // transient network hiccup -- keep polling
    }
  }, 2000);
}

function renderQuestion(data) {
  currentInteractionId = data.interaction_id;
  document.getElementById("skill-badge").textContent = data.skill_name;
  document.getElementById("difficulty-badge").textContent = data.difficulty;
  document.getElementById("review-badge").classList.toggle("hidden", !data.is_review);
  document.getElementById("scenario-text").textContent = data.scenario;
  document.getElementById("question-text").textContent = data.question;

  const optionsList = document.getElementById("options-list");
  optionsList.innerHTML = "";
  data.options.forEach((opt, idx) => {
    const btn = document.createElement("button");
    btn.className = "option-btn";
    btn.textContent = opt;
    btn.addEventListener("click", () => submitAnswer(idx));
    optionsList.appendChild(btn);
  });

  document.getElementById("q-error").textContent = "";
  questionStartTime = Date.now();
  showOnly(questionScreen);
}

async function submitAnswer(chosenIndex) {
  const buttons = document.querySelectorAll("#options-list .option-btn");
  buttons.forEach(b => b.disabled = true);
  const responseTimeMs = Date.now() - questionStartTime;

  try {
    const res = await fetch(`/api/learner/${learnerId}/answer`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        interaction_id: currentInteractionId,
        chosen_index: chosenIndex,
        response_time_ms: responseTimeMs,
      }),
    });
    const data = await res.json();

    buttons[chosenIndex].classList.add(data.is_correct ? "selected-correct" : "selected-incorrect");
    if (!data.is_correct) buttons[data.correct_index].classList.add("selected-correct");

    setTimeout(() => {
      document.getElementById("feedback-heading").textContent = data.is_correct ? "✅ Correct!" : "❌ Not quite.";
      document.getElementById("feedback-explanation").textContent = data.explanation;
      document.getElementById("feedback-citation").textContent = data.citation ? `Source: ${data.citation}` : "";
      document.getElementById("mastery-value").textContent = data.new_mastery;
      document.getElementById("uncertainty-value").textContent = data.uncertainty;
      showOnly(feedbackScreen);
    }, 700);
  } catch (e) {
    document.getElementById("q-error").textContent = "Failed to submit answer.";
  }
}

document.getElementById("next-btn").addEventListener("click", loadNextQuestion);

// ---------------- Patient Simulator mode ----------------

async function startSimulation() {
  stopAllPolling();
  showOnly(loadingScreen);
  try {
    const res = await fetch(`/api/learner/${learnerId}/simulation/start`);
    const data = await res.json();

    if (data.insufficient_context) {
      showOnly(simIntroScreen);
      document.getElementById("sim-intro-text").textContent = "";
      document.getElementById("sim-prompt-text").textContent = data.message;
      document.getElementById("sim-actions-list").innerHTML = "";
      return;
    }

    if (data.pending_review) {
      currentSimId = data.sim_id;
      showOnly(pendingScreen);
      startSimPendingPoll();
      return;
    }

    renderSimStep(data);
  } catch (e) {
    showOnly(modeScreen);
  }
}

function startSimPendingPoll() {
  if (simPendingPollTimer) clearInterval(simPendingPollTimer);
  simPendingPollTimer = setInterval(async () => {
    try {
      const res = await fetch(`/api/learner/${learnerId}/simulation/${currentSimId}/status`);
      const data = await res.json();
      if (data.status === "approved") {
        clearInterval(simPendingPollTimer);
        renderSimStep(data);
      } else if (data.status === "rejected") {
        clearInterval(simPendingPollTimer);
        startSimulation();
      }
    } catch (e) {
      // transient network hiccup -- keep polling
    }
  }, 2000);
}

function renderSimStep(data) {
  currentSimId = data.sim_id;
  lastSimActions = data.actions;

  document.getElementById("sim-skill-badge").textContent = data.skill_name;
  document.getElementById("sim-difficulty-badge").textContent = data.difficulty;
  document.getElementById("sim-intro-text").textContent = data.patient_intro || "";
  document.getElementById("sim-prompt-text").textContent = data.prompt;

  const actionsList = document.getElementById("sim-actions-list");
  actionsList.innerHTML = "";
  data.actions.forEach((label, idx) => {
    const btn = document.createElement("button");
    btn.className = "option-btn";
    btn.textContent = label;
    btn.addEventListener("click", () => submitSimAction(idx));
    actionsList.appendChild(btn);
  });

  showOnly(simIntroScreen);
}

async function submitSimAction(actionIndex) {
  const buttons = document.querySelectorAll("#sim-actions-list .option-btn");
  buttons.forEach(b => b.disabled = true);

  try {
    const res = await fetch(`/api/learner/${learnerId}/simulation/${currentSimId}/action`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action_index: actionIndex }),
    });
    const data = await res.json();

    buttons[actionIndex].classList.add(data.is_correct ? "selected-correct" : "selected-incorrect");

    setTimeout(() => {
      if (data.completed) {
        document.getElementById("sim-resolution-text").textContent = data.resolution;
        document.getElementById("sim-score-text").textContent = data.score;
        showOnly(simCompleteScreen);
      } else {
        document.getElementById("sim-outcome-result").textContent =
          data.is_correct ? "✅ Good call." : "❌ Not the best choice.";
        document.getElementById("sim-outcome-text").textContent = data.outcome;
        showOnly(simOutcomeScreen);

        document.getElementById("sim-continue-btn").onclick = () => {
          renderSimStep({
            sim_id: currentSimId,
            skill_name: document.getElementById("sim-skill-badge").textContent,
            difficulty: document.getElementById("sim-difficulty-badge").textContent,
            patient_intro: null,
            prompt: data.next_prompt,
            actions: data.next_actions,
          });
        };
      }
    }, 500);
  } catch (e) {
    // leave buttons disabled; a real deployment would show a retry option here
  }
}
