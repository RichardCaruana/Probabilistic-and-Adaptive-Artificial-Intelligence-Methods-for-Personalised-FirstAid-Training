const SKILLS = window.__SKILLS__ || [];
let selectedTopics = new Set();
let currentCourseId = null;

function renderTopicChips(active) {
  selectedTopics = new Set(active);
  const container = document.getElementById("topic-list");
  container.innerHTML = "";
  const courseSkills = SKILLS.filter((s) => s.course_id === currentCourseId);
  courseSkills.forEach((skill) => {
    const chip = document.createElement("div");
    chip.className =
      "topic-chip" + (selectedTopics.has(skill.id) ? " active" : "");
    chip.textContent = `U${skill.unit} · ${skill.name}`;
    chip.addEventListener("click", () => {
      if (selectedTopics.has(skill.id)) selectedTopics.delete(skill.id);
      else selectedTopics.add(skill.id);
      chip.classList.toggle("active");
    });
    container.appendChild(chip);
  });
}

document.getElementById("cohort-input").addEventListener("change", (e) => {
  const cohort = e.target.value.trim();
  document.getElementById("qr-img").src =
    "/qr.png" + (cohort ? `?cohort=${encodeURIComponent(cohort)}` : "");
});

async function loadCourseOptions() {
  const res = await fetch("/api/courses");
  const data = await res.json();
  currentCourseId = data.courses.length ? data.courses[0].id : null;
}

async function loadTopic() {
  if (currentCourseId === null) return;
  const res = await fetch(`/api/instructor/topic?course_id=${currentCourseId}`);
  const data = await res.json();
  renderTopicChips(data.active_skill_ids);
  document.getElementById("topic-status").textContent = "";
}

document
  .getElementById("save-topic-btn")
  .addEventListener("click", async () => {
    const res = await fetch("/api/instructor/topic", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        course_id: currentCourseId,
        skill_ids: Array.from(selectedTopics),
      }),
    });
    const data = await res.json();
    document.getElementById("topic-status").textContent =
      data.scope === "all_skills"
        ? "Scope: all skills in this course (no restriction)."
        : `Scope saved: ${data.active_skill_names.join(", ")}`;
  });

async function loadAlerts() {
  const res = await fetch("/api/instructor/alerts");
  const data = await res.json();
  const container = document.getElementById("alerts-list");
  container.innerHTML = "";
  if (data.alerts.length === 0) {
    container.innerHTML = '<p class="muted">No active alerts.</p>';
    return;
  }
  data.alerts.forEach((alert) => {
    const div = document.createElement("div");
    div.className = "alert-item";
    const time = new Date(alert.created_at * 1000).toLocaleTimeString();
    div.innerHTML = `<div>${alert.message}</div><div class="muted">${time}</div>`;
    const btn = document.createElement("button");
    btn.textContent = "Resolve";
    btn.addEventListener("click", async () => {
      await fetch(`/api/instructor/alerts/${alert.id}/resolve`, {
        method: "POST",
      });
      loadAlerts();
    });
    div.appendChild(btn);
    container.appendChild(div);
  });
}

async function loadLearners() {
  const res = await fetch("/api/instructor/overview");
  const data = await res.json();
  const tbody = document.querySelector("#learners-table tbody");
  tbody.innerHTML = "";
  data.learners.forEach((learner) => {
    const tr = document.createElement("tr");
    tr.className = "clickable";
    tr.innerHTML = `
      <td>${learner.name}</td>
      <td>${learner.course_name || "—"}</td>
      <td>${learner.profession || "—"}</td>
      <td>${learner.cohort || "—"}</td>
      <td>${learner.condition || "adaptive"}</td>
      <td>${learner.turns}</td>
      <td>${learner.avg_mastery !== null ? learner.avg_mastery : "—"}</td>
      <td>View →</td>
    `;
    tr.addEventListener("click", () => loadLearnerDetail(learner.id));
    tbody.appendChild(tr);
  });
}

async function loadLearnerDetail(learnerId) {
  const res = await fetch(`/api/instructor/learner/${learnerId}`);
  const data = await res.json();
  document.getElementById(
    "detail-name"
  ).textContent = `${data.learner.name} (${data.learner.course_name}) — Skill Mastery`;
  const tbody = document.getElementById("detail-skills");
  tbody.innerHTML = "";
  data.skills.forEach((skill) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${skill.name}</td>
      <td>${skill.unit}</td>
      <td>${skill.mastery !== null ? skill.mastery : "—"}</td>
      <td>${skill.attempts}</td>
      <td>${
        skill.flagged ? '<span class="flag-yes">⚠ Flagged</span>' : "—"
      }</td>
    `;
    tbody.appendChild(tr);
  });
  document.getElementById("learner-detail").classList.remove("hidden");
  document
    .getElementById("learner-detail")
    .scrollIntoView({ behavior: "smooth" });
}

// ---------------- Human-in-the-Loop Review mode (confidence-based) ----------------

function updateThresholdLabel(value) {
  document.getElementById("threshold-label").textContent = `${Math.round(
    value * 100
  )}%`;
}

function updateThresholdRowVisibility(mode) {
  document
    .getElementById("confidence-threshold-row")
    .classList.toggle("hidden", mode !== "confidence");
}

async function loadReviewMode() {
  const res = await fetch("/api/instructor/review_mode");
  const data = await res.json();
  document.getElementById("review-mode-select").value = data.mode;
  document.getElementById("confidence-threshold").value = data.threshold;
  updateThresholdLabel(data.threshold);
  updateThresholdRowVisibility(data.mode);
}

document
  .getElementById("review-mode-select")
  .addEventListener("change", (e) => {
    updateThresholdRowVisibility(e.target.value);
  });

document
  .getElementById("confidence-threshold")
  .addEventListener("input", (e) => {
    updateThresholdLabel(e.target.value);
  });

document
  .getElementById("save-review-mode-btn")
  .addEventListener("click", async () => {
    const mode = document.getElementById("review-mode-select").value;
    const threshold = parseFloat(
      document.getElementById("confidence-threshold").value
    );

    const res = await fetch("/api/instructor/review_mode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        mode,
        threshold: mode === "confidence" ? threshold : undefined,
      }),
    });
    const data = await res.json();

    document.getElementById("review-mode-status").textContent =
      `Saved: ${data.mode}` +
      (data.mode === "confidence"
        ? ` (threshold ${Math.round(data.threshold * 100)}%)`
        : "");

    loadReviewQueue();
    loadReviewQueueSimulations();
  });

function confidenceBadgeHtml(item) {
  if (item.confidence_score === null || item.confidence_score === undefined)
    return "";
  const pct = Math.round(item.confidence_score * 100);
  const reasonLabel =
    item.review_reason === "low_confidence"
      ? "weak grounding"
      : item.review_reason === "manual"
      ? "manual review"
      : "";
  return `<span class="badge confidence-badge">Confidence: ${pct}%${
    reasonLabel ? " · " + reasonLabel : ""
  }</span>`;
}

async function loadReviewQueue() {
  const res = await fetch("/api/instructor/review_queue");
  const data = await res.json();
  const card = document.getElementById("review-queue-card");
  const list = document.getElementById("review-queue-list");

  if (data.pending.length === 0) {
    card.classList.add("hidden");
    list.innerHTML = "";
    return;
  }
  card.classList.remove("hidden");
  list.innerHTML = "";

  data.pending.forEach((item) => {
    const div = document.createElement("div");
    div.className = "review-item";

    const optionsHtml = item.options
      .map(
        (opt, idx) => `
      <div class="option-edit-row">
        <input type="radio" name="correct-${
          item.interaction_id
        }" value="${idx}" ${idx === item.correct_index ? "checked" : ""} />
        <input type="text" value="${escapeAttr(opt)}" data-opt-idx="${idx}" />
      </div>
    `
      )
      .join("");

    div.innerHTML = `
      <div class="meta-row">
        <span class="badge">${item.learner_name}</span>
        <span class="badge">${item.skill_name}</span>
        <span class="badge">${item.difficulty}</span>
        ${confidenceBadgeHtml(item)}
      </div>
      ${
        item.safety_flag
          ? `<div class="safety-warning">⚠ Safety filter flagged: ${
              item.safety_reason || "unspecified"
            }</div>`
          : ""
      }
      <p class="scenario">${escapeHtml(item.scenario)}</p>
      <label class="muted">Question</label>
      <textarea rows="2" class="question-field">${escapeHtml(
        item.question
      )}</textarea>
      <label class="muted">Options (select the correct one)</label>
      ${optionsHtml}
      <label class="muted">Explanation</label>
      <textarea rows="2" class="explanation-field">${escapeHtml(
        item.explanation
      )}</textarea>
      <div class="review-actions">
        <button class="approve-btn">Approve</button>
        <button class="reject-btn">Reject</button>
      </div>
    `;

    div.querySelector(".approve-btn").addEventListener("click", async () => {
      const options = Array.from(div.querySelectorAll("[data-opt-idx]")).map(
        (inp) => inp.value
      );
      const correctIndex = parseInt(
        div.querySelector(
          `input[name="correct-${item.interaction_id}"]:checked`
        ).value,
        10
      );
      const question = div.querySelector(".question-field").value;
      const explanation = div.querySelector(".explanation-field").value;
      await fetch(`/api/instructor/review/${item.interaction_id}/approve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question,
          options,
          correct_index: correctIndex,
          explanation,
        }),
      });
      loadReviewQueue();
    });

    div.querySelector(".reject-btn").addEventListener("click", async () => {
      await fetch(`/api/instructor/review/${item.interaction_id}/reject`, {
        method: "POST",
      });
      loadReviewQueue();
    });

    list.appendChild(div);
  });
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str || "";
  return div.innerHTML;
}
function escapeAttr(str) {
  return (str || "").replace(/"/g, "&quot;");
}

async function loadReviewQueueSimulations() {
  const res = await fetch("/api/instructor/review_queue_simulations");
  const data = await res.json();
  const card = document.getElementById("review-sim-queue-card");
  const list = document.getElementById("review-sim-queue-list");

  if (data.pending.length === 0) {
    card.classList.add("hidden");
    list.innerHTML = "";
    return;
  }
  card.classList.remove("hidden");
  list.innerHTML = "";

  data.pending.forEach((sim) => {
    const div = document.createElement("div");
    div.className = "review-item";

    const stepsHtml = sim.steps
      .map(
        (step, i) => `
      <div style="margin-bottom:10px; padding-left:10px; border-left:2px solid var(--border);">
        <strong>Step ${i + 1}:</strong> ${escapeHtml(step.prompt)}
        <ul style="margin:6px 0 0 18px; padding:0;">
          ${step.actions
            .map(
              (a) =>
                `<li>${a.correct ? "✅" : "▫️"} ${escapeHtml(
                  a.label
                )} <span class="muted">— ${escapeHtml(a.outcome)}</span></li>`
            )
            .join("")}
        </ul>
      </div>
    `
      )
      .join("");

    div.innerHTML = `
      <div class="meta-row">
        <span class="badge">${sim.learner_name}</span>
        <span class="badge">${sim.skill_name}</span>
        <span class="badge">${sim.difficulty}</span>
        ${confidenceBadgeHtml(sim)}
      </div>
      ${
        sim.safety_flag
          ? `<div class="safety-warning">⚠ Safety filter flagged: ${
              sim.safety_reason || "unspecified"
            }</div>`
          : ""
      }
      <p class="scenario">${escapeHtml(sim.patient_intro)}</p>
      ${stepsHtml}
      <p class="muted"><strong>Resolution:</strong> ${escapeHtml(
        sim.resolution
      )}</p>
      <div class="review-actions">
        <button class="approve-btn">Approve</button>
        <button class="reject-btn">Reject</button>
      </div>
    `;

    div.querySelector(".approve-btn").addEventListener("click", async () => {
      await fetch(`/api/instructor/review_simulation/${sim.sim_id}/approve`, {
        method: "POST",
      });
      loadReviewQueueSimulations();
    });
    div.querySelector(".reject-btn").addEventListener("click", async () => {
      await fetch(`/api/instructor/review_simulation/${sim.sim_id}/reject`, {
        method: "POST",
      });
      loadReviewQueueSimulations();
    });

    list.appendChild(div);
  });
}

async function refreshAll() {
  await Promise.all([
    loadAlerts(),
    loadLearners(),
    loadReviewQueue(),
    loadReviewQueueSimulations(),
  ]);
}

loadCourseOptions().then(loadTopic);
loadReviewMode();
refreshAll();
setInterval(refreshAll, 3000);
