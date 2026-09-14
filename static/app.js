const messagesEl = document.getElementById("messages");
const formEl = document.getElementById("chat-form");
const inputEl = document.getElementById("msg");
const profileBox = document.getElementById("profile-box");
const resetBtn = document.getElementById("reset-btn");

function addMessage(text, who) {
  const div = document.createElement("div");
  div.className = "msg " + who;
  div.textContent = text;
  messagesEl.appendChild(div);
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
    body: JSON.stringify({ message: text }),
  });
  const data = await res.json();
  if (data.error) {
    addMessage("error: " + data.error, "bot");
    return;
  }
  addMessage(data.reply, "bot");
  profileBox.textContent = data.profile_brief + "\n\n" + JSON.stringify(data.profile, null, 2);
});

resetBtn.addEventListener("click", async () => {
  await fetch("/api/reset", { method: "POST" });
  messagesEl.innerHTML = "";
  profileBox.textContent = "no messages yet";
});
