const messagesEl = document.getElementById("messages");
const formEl = document.getElementById("chat-form");
const inputEl = document.getElementById("msg");
const profileBox = document.getElementById("profile-box");
const resetBtn = document.getElementById("reset-btn");
const compareToggle = document.getElementById("compare-toggle");

function addMessage(text, who) {
  const wrap = document.createElement("div");
  wrap.className = "msg-wrap " + who;
  const div = document.createElement("div");
  div.className = "msg " + who;
  div.textContent = text;
  wrap.appendChild(div);
  messagesEl.appendChild(wrap);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return wrap;
}

function addPromptDetail(container, label, promptText) {
  if (!promptText) return;
  const details = document.createElement("details");
  details.className = "prompt-detail";
  const summary = document.createElement("summary");
  summary.textContent = label;
  const pre = document.createElement("pre");
  pre.textContent = promptText;
  details.appendChild(summary);
  details.appendChild(pre);
  container.appendChild(details);
}

function addComparisonReply(data) {
  const wrap = document.createElement("div");
  wrap.className = "compare-wrap";

  const personalizedCol = document.createElement("div");
  personalizedCol.className = "compare-col personalized";
  const pTitle = document.createElement("h4");
  pTitle.textContent = "Personalized (your profile)";
  const pText = document.createElement("div");
  pText.textContent = data.comparison.personalized_reply;
  personalizedCol.appendChild(pTitle);
  personalizedCol.appendChild(pText);
  addPromptDetail(personalizedCol, "system prompt used", data.comparison.personalized_system_prompt);

  const genericCol = document.createElement("div");
  genericCol.className = "compare-col generic";
  const gTitle = document.createElement("h4");
  gTitle.textContent = "Generic (no profile)";
  const gText = document.createElement("div");
  gText.textContent = data.comparison.generic_reply;
  genericCol.appendChild(gTitle);
  genericCol.appendChild(gText);
  addPromptDetail(genericCol, "system prompt used", data.comparison.generic_system_prompt);

  wrap.appendChild(personalizedCol);
  wrap.appendChild(genericCol);
  messagesEl.appendChild(wrap);

  const divergence = document.createElement("div");
  divergence.className = "divergence";
  const pct = Math.round((data.comparison.divergence_score || 0) * 100);
  divergence.textContent = "Divergence between the two replies: " + pct + "% (word-overlap distance)";
  messagesEl.appendChild(divergence);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

formEl.addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = inputEl.value.trim();
  if (!text) return;
  addMessage(text, "user");
  inputEl.value = "";

  const res = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message: text, compare: compareToggle.checked }),
  });
  const data = await res.json();
  if (data.error) {
    addMessage("error: " + data.error, "bot");
    return;
  }

  if (data.comparison) {
    addComparisonReply(data);
  } else {
    const bubbleWrap = addMessage(data.reply, "bot");
    if (data.comparison_error) {
      const note = document.createElement("div");
      note.className = "divergence";
      note.textContent = data.comparison_error;
      messagesEl.appendChild(note);
    }
    addPromptDetail(bubbleWrap, "system prompt used", data.system_prompt);
  }

  profileBox.textContent = data.profile_brief + "\n\n" + JSON.stringify(data.profile, null, 2);
});

resetBtn.addEventListener("click", async () => {
  await fetch("/api/reset", { method: "POST" });
  messagesEl.innerHTML = "";
  profileBox.textContent = "no messages yet";
});
