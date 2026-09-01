// Popup: settings toggles + account/quota. All state lives in the SW.

function sw(msg) {
  return new Promise((resolve) => chrome.runtime.sendMessage(msg, resolve));
}

const els = {
  enabled: document.getElementById("enabled"),
  retitleMode: document.getElementById("retitleMode"),
  dim: document.getElementById("dim"),
  showDots: document.getElementById("showDots"),
  account: document.getElementById("account"),
};

async function load() {
  const st = await sw({ type: "state:get" });
  const s = st.settings;
  els.enabled.checked = s.enabled;
  els.retitleMode.value = s.retitleMode;
  els.dim.checked = s.dim;
  els.showDots.checked = s.showDots;
  renderAccount(st.auth, st.quota);
}

function renderAccount(auth, quota) {
  if (auth?.email) {
    const q = quota ? `${quota.used}/${quota.limit} evals used today` : "";
    els.account.innerHTML = `<div class="email">${auth.email}</div><div class="quota">${q}</div>
      <button class="gbtn" id="signout">Sign out</button>`;
    document.getElementById("signout").addEventListener("click", async () => {
      await sw({ type: "auth:signout" });
      load();
    });
  } else {
    els.account.innerHTML = `<button class="gbtn" id="signin">Sign in with Google</button>
      <div class="quota">Works without sign-in for already-scored videos.</div>`;
    document.getElementById("signin").addEventListener("click", async () => {
      const r = await sw({ type: "auth:signin" });
      if (r?.auth) load();
    });
  }
}

function patch(key, value) {
  sw({ type: "settings:set", patch: { [key]: value } });
}

els.enabled.addEventListener("change", () => patch("enabled", els.enabled.checked));
els.retitleMode.addEventListener("change", () => patch("retitleMode", els.retitleMode.value));
els.dim.addEventListener("change", () => patch("dim", els.dim.checked));
els.showDots.addEventListener("change", () => patch("showDots", els.showDots.checked));

load();
