(() => {
  const cfg = window.MAIDS_REVIEW || {};
  const noteBox = document.getElementById("review-note");
  const promptBox = document.getElementById("review-prompt");
  const noteStatus = document.getElementById("note-status");
  const fixerStatus = document.getElementById("fixer-status");
  const credsForm = document.getElementById("llm-credentials");
  const llmPass = document.getElementById("llm-password");
  let verdict = document.querySelector('.verdicts button[aria-pressed="true"]')?.getAttribute("data-verdict") || "";
  let timer = null;

  credsForm?.addEventListener("submit", (e) => e.preventDefault());

  const KEY_PROVIDERS = ["openai", "langcc", "gemini"];
  const fixerProvider = document.getElementById("fixer-provider");
  const keySave = document.getElementById("key-save");
  const keyForget = document.getElementById("key-forget");
  const keyStatus = document.getElementById("key-status");

  function setKeyLine(msg, ok = true) {
    if (!keyStatus) return;
    keyStatus.textContent = msg || "";
    keyStatus.style.color = ok ? "#2a6b4e" : "#8d2c2c";
  }

  async function loadReviewKey() {
    const provider = (fixerProvider?.value || "").toLowerCase();
    if (!KEY_PROVIDERS.includes(provider) || (llmPass && llmPass.value)) return;
    try {
      const resp = await fetch(`/credentials?provider=${encodeURIComponent(provider)}`, { credentials: "same-origin" });
      if (!resp.ok) return;
      const data = await resp.json();
      if (llmPass && data.api_key) llmPass.value = data.api_key;
      if (keyForget) keyForget.disabled = !data.saved;
      if (data.saved) setKeyLine("Saved for your account only.");
    } catch (_) {}
  }

  keySave?.addEventListener("click", async (e) => {
    e.preventDefault();
    const provider = (fixerProvider?.value || "").toLowerCase();
    const key = (llmPass?.value || "").trim();
    if (!KEY_PROVIDERS.includes(provider)) return setKeyLine("This provider does not use an API key.", false);
    if (!key) return setKeyLine("Enter an API key to save.", false);
    const body = new FormData();
    body.append("provider", provider);
    body.append("api_key", key);
    const resp = await fetch("/credentials", { method: "POST", body, credentials: "same-origin" });
    if (!resp.ok) return setKeyLine("Could not save key.", false);
    if (keyForget) keyForget.disabled = false;
    setKeyLine("Saved for your account only.");
  });

  keyForget?.addEventListener("click", async (e) => {
    e.preventDefault();
    const provider = (fixerProvider?.value || "").toLowerCase();
    const body = new FormData();
    body.append("provider", provider);
    const resp = await fetch("/credentials/forget", { method: "POST", body, credentials: "same-origin" });
    if (!resp.ok) return setKeyLine("Could not forget key.", false);
    if (llmPass) llmPass.value = "";
    if (keyForget) keyForget.disabled = true;
    setKeyLine("Saved key removed from your account.");
  });

  loadReviewKey();

  function setStatus(el, msg, ok = true) {
    if (!el) return;
    el.textContent = msg;
    el.style.color = ok ? "#2a6b4e" : "#8d2c2c";
  }

  async function saveNote() {
    if (cfg.rowIdx === null || cfg.rowIdx === undefined) return;
    const body = new FormData();
    body.append("row_idx", String(cfg.rowIdx));
    body.append("verdict", verdict);
    body.append("note", noteBox?.value || "");
    body.append("prompt_draft", promptBox?.value || "");
    const resp = await fetch("/review/notes", { method: "POST", body, credentials: "same-origin" });
    if (!resp.ok) setStatus(noteStatus, "Could not save note.", false);
    else setStatus(noteStatus, "Saved on this run.");
  }

  function queueSave() {
    if (timer) clearTimeout(timer);
    timer = setTimeout(saveNote, 350);
  }

  document.querySelectorAll(".verdicts button").forEach((btn) => {
    btn.addEventListener("click", () => {
      verdict = btn.getAttribute("data-verdict") || "";
      document.querySelectorAll(".verdicts button").forEach((other) => {
        other.setAttribute("aria-pressed", other === btn ? "true" : "false");
      });
      queueSave();
    });
  });
  noteBox?.addEventListener("input", queueSave);
  promptBox?.addEventListener("input", queueSave);

  document.addEventListener("keydown", (e) => {
    const tag = (e.target && e.target.tagName) || "";
    if (tag === "TEXTAREA" || tag === "INPUT" || tag === "SELECT") return;
    if (e.key === "ArrowRight" || e.key === "j" || e.key === "J") {
      if (!cfg.atEnd) window.location.href = cfg.nextUrl;
    } else if (e.key === "ArrowLeft" || e.key === "k" || e.key === "K") {
      if (!cfg.atStart) window.location.href = cfg.prevUrl;
    } else if (e.key === "1") {
      document.querySelector('[data-verdict="ok"]')?.click();
    } else if (e.key === "2") {
      document.querySelector('[data-verdict="wrong"]')?.click();
    } else if (e.key === "3") {
      document.querySelector('[data-verdict="unclear"]')?.click();
    }
  });

  async function pollFixer() {
    const resp = await fetch("/review/improve/status", { credentials: "same-origin" });
    if (!resp.ok) return "error";
    const data = await resp.json();
    setStatus(fixerStatus, data.message || data.status || "", data.status !== "error");
    return data.status;
  }

  document.getElementById("improve-button")?.addEventListener("click", async () => {
    await saveNote();
    const body = new FormData();
    body.append("provider", document.getElementById("fixer-provider")?.value || "openai");
    body.append("model", document.getElementById("fixer-model")?.value || "");
    body.append("api_key", llmPass?.value || "");
    body.append("prompt_draft", promptBox?.value || "");
    setStatus(fixerStatus, "Running analyst → editor → critic…", true);
    const resp = await fetch("/review/improve", { method: "POST", body, credentials: "same-origin" });
    if (!resp.ok) {
      setStatus(fixerStatus, await resp.text(), false);
      return;
    }
    let status = "running";
    while (status === "running") {
      await new Promise((r) => setTimeout(r, 800));
      status = await pollFixer();
    }
    if (status === "done") window.location.reload();
  });

  document.getElementById("accept-button")?.addEventListener("click", async () => {
    const base = prompt("Shared prompt name for the new version?", cfg.acceptedName || "prompt");
    if (!base) return;
    const body = new FormData();
    body.append("prompt_name", base);
    const resp = await fetch("/review/accept", { method: "POST", body, credentials: "same-origin" });
    if (!resp.ok) {
      setStatus(fixerStatus, await resp.text(), false);
      return;
    }
    const data = await resp.json();
    setStatus(fixerStatus, `Saved shared prompt ${data.name}. See it under Prompts.`, true);
  });

  document.getElementById("discard-button")?.addEventListener("click", async () => {
    if (!confirm("Discard this proposed update? It stays in the fix history marked as discarded.")) return;
    const resp = await fetch("/review/discard", { method: "POST", credentials: "same-origin" });
    if (!resp.ok) {
      setStatus(fixerStatus, await resp.text(), false);
      return;
    }
    window.location.reload();
  });
})();
