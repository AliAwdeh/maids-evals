(() => {
  const cfg = window.MAIDS_TEST || {};
  const sid = cfg.sidToken || "";
  const noteBox = document.getElementById("test-note");
  const fixerStatus = document.getElementById("fixer-status");
  const credsForm = document.getElementById("llm-credentials");
  const llmPass = document.getElementById("llm-password");
  const fixerProvider = document.getElementById("fixer-provider");
  const keySave = document.getElementById("key-save");
  const keyForget = document.getElementById("key-forget");
  const keyStatus = document.getElementById("key-status");
  const acceptBtn = document.getElementById("accept-button");
  const discardBtn = document.getElementById("discard-button");
  const proposedCard = document.getElementById("proposed-card");
  const proposedDiff = document.getElementById("proposed-diff");
  const analystLine = document.getElementById("analyst-line");
  const criticLine = document.getElementById("critic-line");
  const againApiKey = document.getElementById("again-api-key");
  const againForm = document.getElementById("again-form");
  const KEY_PROVIDERS = ["openai", "langcc", "gemini"];
  let verdict = "";

  credsForm?.addEventListener("submit", (e) => e.preventDefault());

  function setStatus(el, msg, ok = true) {
    if (!el) return;
    el.textContent = msg || "";
    el.style.color = ok ? "var(--ok)" : "var(--bad)";
  }

  function withSid(body) {
    if (sid) body.append("sid_token", sid);
    return body;
  }

  async function loadKey() {
    const provider = (fixerProvider?.value || "").toLowerCase();
    if (!KEY_PROVIDERS.includes(provider) || (llmPass && llmPass.value)) return;
    try {
      const resp = await fetch(`/credentials?provider=${encodeURIComponent(provider)}`, { credentials: "same-origin" });
      if (!resp.ok) return;
      const data = await resp.json();
      if (llmPass && data.saved && !llmPass.value) llmPass.placeholder = "Saved key in use — leave blank";
      if (keyForget) keyForget.disabled = !data.saved;
      if (data.saved) setStatus(keyStatus, "Saved for your account only.");
    } catch (_) {}
  }

  keySave?.addEventListener("click", async (e) => {
    e.preventDefault();
    const provider = (fixerProvider?.value || "").toLowerCase();
    const key = (llmPass?.value || "").trim();
    if (!KEY_PROVIDERS.includes(provider)) return setStatus(keyStatus, "This provider does not use a key.", false);
    if (!key) return setStatus(keyStatus, "Enter an API key to save.", false);
    const body = new FormData();
    body.append("provider", provider);
    body.append("api_key", key);
    const resp = await fetch("/credentials", { method: "POST", body, credentials: "same-origin" });
    if (!resp.ok) return setStatus(keyStatus, "Could not save key.", false);
    if (keyForget) keyForget.disabled = false;
    setStatus(keyStatus, "Saved for your account only.");
  });

  keyForget?.addEventListener("click", async (e) => {
    e.preventDefault();
    const provider = (fixerProvider?.value || "").toLowerCase();
    const body = new FormData();
    body.append("provider", provider);
    const resp = await fetch("/credentials/forget", { method: "POST", body, credentials: "same-origin" });
    if (!resp.ok) return setStatus(keyStatus, "Could not forget key.", false);
    if (llmPass) llmPass.value = "";
    if (keyForget) keyForget.disabled = true;
    setStatus(keyStatus, "Saved key removed from your account.");
  });

  loadKey();

  document.querySelectorAll(".verdicts button").forEach((btn) => {
    btn.addEventListener("click", () => {
      verdict = btn.getAttribute("data-verdict") || "";
      document.querySelectorAll(".verdicts button").forEach((other) => {
        other.setAttribute("aria-pressed", other === btn ? "true" : "false");
      });
    });
  });

  // Carry the typed key into the "Another random row" post so re-tests work.
  againForm?.addEventListener("submit", () => {
    if (againApiKey) againApiKey.value = llmPass?.value || "";
  });

  function showProposed(data) {
    if (proposedDiff) proposedDiff.innerHTML = data.diff_html || "";
    if (proposedCard) proposedCard.style.display = data.diff_html ? "block" : "none";
    if (analystLine) {
      analystLine.textContent = data.analysis ? "Analyst: " + data.analysis : "";
      analystLine.style.display = data.analysis ? "block" : "none";
    }
    if (criticLine) {
      criticLine.textContent = data.critic ? "Critic: " + data.critic : "";
      criticLine.style.display = data.critic ? "block" : "none";
    }
    if (acceptBtn) acceptBtn.disabled = !data.proposed;
    if (discardBtn) discardBtn.disabled = !data.proposed;
  }

  async function pollFixer() {
    const qs = sid ? `?sid_token=${encodeURIComponent(sid)}` : "";
    const resp = await fetch(`/test/improve/status${qs}`, { credentials: "same-origin" });
    if (!resp.ok) return { status: "error" };
    const data = await resp.json();
    setStatus(fixerStatus, data.message || data.status || "", data.status !== "error");
    if (data.status === "done") showProposed(data);
    return data;
  }

  document.getElementById("improve-button")?.addEventListener("click", async () => {
    const note = (noteBox?.value || "").trim();
    if (!note && !verdict) {
      setStatus(fixerStatus, "Add a note or pick a verdict first.", false);
      return;
    }
    const body = withSid(new FormData());
    body.append("provider", fixerProvider?.value || cfg.provider || "openai");
    body.append("model", document.getElementById("fixer-model")?.value || cfg.model || "");
    body.append("api_key", llmPass?.value || "");
    body.append("verdict", verdict);
    body.append("note", note);
    setStatus(fixerStatus, "Running analyst → editor → critic… this can take a minute.", true);
    window.setBusy("improve-button", true, "Working…");
    try {
      const resp = await fetch("/test/improve", { method: "POST", body, credentials: "same-origin" });
      if (!resp.ok) {
        setStatus(fixerStatus, await resp.text(), false);
        return;
      }
      let data = { status: "running" };
      while (data.status === "running") {
        await new Promise((r) => setTimeout(r, 800));
        data = await pollFixer();
      }
    } finally {
      window.setBusy("improve-button", false);
    }
  });

  acceptBtn?.addEventListener("click", async () => {
    const base = prompt("Shared prompt name for the new version?", cfg.acceptedName || "prompt");
    if (!base) return;
    const body = withSid(new FormData());
    body.append("prompt_name", base);
    const resp = await fetch("/test/accept", { method: "POST", body, credentials: "same-origin" });
    if (!resp.ok) {
      setStatus(fixerStatus, await resp.text(), false);
      return;
    }
    const data = await resp.json();
    setStatus(fixerStatus, `Saved shared prompt ${data.name}. See it under Prompts.`, true);
    if (acceptBtn) acceptBtn.disabled = true;
    if (discardBtn) discardBtn.disabled = true;
  });

  discardBtn?.addEventListener("click", async () => {
    const body = withSid(new FormData());
    const resp = await fetch("/test/discard", { method: "POST", body, credentials: "same-origin" });
    if (!resp.ok) {
      setStatus(fixerStatus, await resp.text(), false);
      return;
    }
    if (proposedCard) proposedCard.style.display = "none";
    if (acceptBtn) acceptBtn.disabled = true;
    if (discardBtn) discardBtn.disabled = true;
    setStatus(fixerStatus, "Discarded. It stays in the fix history.", true);
  });
})();
