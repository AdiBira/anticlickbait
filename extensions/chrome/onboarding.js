function sw(msg) {
  return new Promise((resolve) => chrome.runtime.sendMessage(msg, resolve));
}

document.getElementById("signin").addEventListener("click", async () => {
  const status = document.getElementById("status");
  status.textContent = "Opening Google sign-in...";
  const r = await sw({ type: "auth:signin" });
  status.textContent = r?.auth?.email ? `Signed in as ${r.auth.email}. You are all set.` : "Sign-in failed. You can still use cached scores.";
});
