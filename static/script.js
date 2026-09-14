const topicInput = document.getElementById("topic");
const constraintsInput = document.getElementById("constraints");
const startBtn = document.getElementById("startBtn");
const formError = document.getElementById("formError");
const logEl = document.getElementById("log");
const statusPill = document.getElementById("statusPill");
const resultBox = document.getElementById("resultBox");
const resultPreview = document.getElementById("resultPreview");
const downloadLink = document.getElementById("downloadLink");
const historyList = document.getElementById("historyList");

let currentSource = null;

function setStatus(text, cls) {
  statusPill.textContent = text;
  statusPill.className = "pill " + cls;
}

function clearLog() {
  logEl.innerHTML = "";
}

function appendLog(message, cls) {
  const p = document.createElement("p");
  if (cls) p.className = cls;
  p.textContent = message;
  logEl.appendChild(p);
  logEl.scrollTop = logEl.scrollHeight;
}

function showError(message) {
  formError.textContent = message;
  formError.hidden = false;
}

function hideError() {
  formError.hidden = true;
  formError.textContent = "";
}

async function loadHistory() {
  try {
    const res = await fetch("/api/history");
    if (!res.ok) return;
    const items = await res.json();
    historyList.innerHTML = "";
    if (!items.length) {
      historyList.innerHTML = '<li class="muted">No reports yet.</li>';
      return;
    }
    for (const item of items) {
      const li = document.createElement("li");
      const date = new Date(item.created_at + "Z");
      const label = item.topic.length > 42 ? item.topic.slice(0, 42) + "…" : item.topic;
      li.innerHTML = `<a href="/api/download/${item.id}">${label}</a><br><span class="muted">${date.toLocaleDateString()}</span>`;
      historyList.appendChild(li);
    }
  } catch (e) {
    // Silently ignore — history is a convenience, not critical path.
  }
}

function startResearch() {
  hideError();
  const topic = topicInput.value.trim();
  const constraints = constraintsInput.value.trim();

  if (!topic) {
    showError("Enter a topic to research.");
    topicInput.focus();
    return;
  }
  if (topic.length > 300) {
    showError("Topic is too long — keep it under 300 characters.");
    return;
  }

  if (currentSource) {
    currentSource.close();
  }

  startBtn.disabled = true;
  resultBox.hidden = true;
  clearLog();
  setStatus("running", "running");
  appendLog("Starting agent run...", "step-title");

  const params = new URLSearchParams({ topic, constraints });
  const source = new EventSource(`/api/research?${params.toString()}`);
  currentSource = source;

  source.addEventListener("progress", (e) => {
    const data = JSON.parse(e.data);
    appendLog(data.message);
  });

  source.addEventListener("done", (e) => {
    const data = JSON.parse(e.data);
    setStatus("done", "done");
    appendLog("Report generated successfully.", "step-title");
    resultPreview.textContent = data.preview + (data.preview.length >= 800 ? "…" : "");
    downloadLink.href = data.download_url;
    resultBox.hidden = false;
    source.close();
    currentSource = null;
    startBtn.disabled = false;
    loadHistory();
  });

  source.addEventListener("error", (e) => {
    let message = "Connection to the agent was lost.";
    if (e.data) {
      try {
        message = JSON.parse(e.data).message || message;
      } catch (_) {
        /* keep default message */
      }
    }
    setStatus("error", "error");
    appendLog(message, "error-line");
    source.close();
    currentSource = null;
    startBtn.disabled = false;
  });
}

startBtn.addEventListener("click", startResearch);
topicInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") startResearch();
});

loadHistory();
