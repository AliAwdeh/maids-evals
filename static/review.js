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
    keyStatus.style.color = ok ? "var(--ok)" : "var(--bad)";
  }

  async function loadReviewKey() {
    // Only asks whether a key is saved. The fixer route resolves the value
    // server-side, so the secret never has to reach this page.
    const provider = (fixerProvider?.value || "").toLowerCase();
    if (!KEY_PROVIDERS.includes(provider)) return;
    try {
      const resp = await fetch(`/credentials?provider=${encodeURIComponent(provider)}`, { credentials: "same-origin" });
      if (!resp.ok) return;
      const data = await resp.json();
      if (keyForget) keyForget.disabled = !data.saved;
      if (data.saved) {
        setKeyLine("Saved key in use for your account.");
        if (llmPass && !llmPass.value) llmPass.placeholder = "Saved key in use — leave blank";
      }
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

  document.getElementById("fixer-settings-toggle")?.addEventListener("click", () => {
    const box = document.getElementById("fixer-settings");
    if (box) { box.open = !box.open; if (box.open) box.scrollIntoView({ block: "nearest" }); }
  });

  function setStatus(el, msg, ok = true) {
    if (!el) return;
    el.textContent = msg;
    el.style.color = ok ? "var(--ok)" : "var(--bad)";
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
    const bad = data.status === "error" || data.unchanged || (data.warnings || []).length > 0;
    setStatus(fixerStatus, data.message || data.status || "", !bad);
    return data.status;
  }

  document.getElementById("improve-button")?.addEventListener("click", async () => {
    await saveNote();
    const body = new FormData();
    body.append("provider", document.getElementById("fixer-provider")?.value || "openai");
    body.append("model", document.getElementById("fixer-model")?.value || "");
    body.append("api_key", llmPass?.value || "");
    body.append("prompt_draft", promptBox?.value || "");
    setStatus(fixerStatus, "Reading your notes, grouping the problems, and rewriting… about a minute.", true);
    window.setBusy("improve-button", true, "Working…");
    try {
      const resp = await fetch("/review/improve", { method: "POST", body, credentials: "same-origin" });
      if (!resp.ok) {
        setStatus(fixerStatus, await resp.text(), false);
        return;
      }
      let status = "running";
      // Bounded wait. The fixer runs in a background thread; if that thread
      // dies without writing a status the old loop span forever with the
      // button stuck on "Working…".
      const deadline = Date.now() + 6 * 60 * 1000;
      while (status === "running" && Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 800));
        status = await pollFixer();
      }
      if (status === "running") {
        setStatus(fixerStatus, "The fixer is taking longer than six minutes. Reload this page to check on it.", false);
        return;
      }
      if (status === "done") window.location.reload();
    } finally {
      window.setBusy("improve-button", false);
    }
  });

  document.getElementById("accept-button")?.addEventListener("click", async () => {
    const base = window.prompt("Name this version:", cfg.acceptedName || "prompt");
    if (!base) return;
    const body = new FormData();
    body.append("prompt_name", base);
    window.setBusy("accept-button", true, "Saving…");
    try {
      const resp = await fetch("/review/accept", { method: "POST", body, credentials: "same-origin" });
      if (!resp.ok) {
        setStatus(fixerStatus, await resp.text(), false);
        return;
      }
      const data = await resp.json();
      const where = data.catalogue_mode === "updated"
        ? "Updated the shared question — everyone who uses it gets this now."
        : "Saved as your own copy under Prompts.";
      setStatus(fixerStatus, `${where} The old text is kept and can be put back.`, true);
    } finally {
      window.setBusy("accept-button", false);
    }
  });

  /* ======================================================================
     Re-check: run the proposed question on the rows that were marked.
     A diff says what the wording became; only this says what it does.
     ====================================================================== */

  const recheckStatus = document.getElementById("recheck-status");

  function renderRecheck(data) {
    const box = document.getElementById("recheck-result");
    const host = document.getElementById("recheck-rows");
    if (!box || !host) return;
    const s = data.summary || {};
    box.hidden = false;
    host.innerHTML = "";

    const summaryEl = box.querySelector(".recheck-summary");
    if (summaryEl) {
      summaryEl.innerHTML = `
        <div class="good"><span class="k">Fixed</span><span class="v">${s.moved || 0}/${s.targets || 0}</span></div>
        <div class="${s.unmoved ? "warn" : ""}"><span class="k">Still wrong</span><span class="v">${s.unmoved || 0}</span></div>
        <div class="${s.regressions ? "bad" : ""}"><span class="k">Broke</span><span class="v">${s.regressions || 0}</span></div>
        <div><span class="k">Checked</span><span class="v">${s.checked || 0}</span></div>`;
    }

    (data.rows || []).forEach((r) => {
      const d = document.createElement("details");
      d.className = "recheck-row";
      const verdictClass = r.verdict === "ok" ? "ok" : r.verdict === "wrong" ? "bad" : r.verdict === "unclear" ? "warn" : "";
      d.innerHTML = `
        <summary>
          <span class="tag ${verdictClass}"></span>
          <span class="tag ${r.changed ? "ai" : ""}">${r.changed ? "changed" : "same answer"}</span>
          <span class="note"></span>
        </summary>
        <div class="recheck-body">
          <div class="recheck-cell"><div class="k">Before</div><pre></pre></div>
          <div class="recheck-cell after"><div class="k">After this fix</div><pre></pre></div>
        </div>`;
      d.querySelector("summary .tag").textContent = `row ${r.row + 1} · you said ${r.verdict || "—"}`;
      d.querySelector(".note").textContent = r.note || "";
      const cells = d.querySelectorAll("pre");
      cells[0].textContent = r.before || "";
      cells[1].textContent = r.error || r.after || "";
      // Open the rows that need a human eye: a regression, or a flagged row
      // the fix did not move.
      if ((r.verdict === "ok" && r.changed) || (r.verdict !== "ok" && !r.changed)) d.open = true;
      host.appendChild(d);
    });
  }

  document.getElementById("recheck-button")?.addEventListener("click", async () => {
    const body = new FormData();
    body.append("provider", document.getElementById("fixer-provider")?.value || "openai");
    body.append("model", document.getElementById("fixer-model")?.value || "");
    body.append("api_key", llmPass?.value || "");
    setStatus(recheckStatus, "Running the new question on your marked rows…", true);
    window.setBusy("recheck-button", true, "Running…");
    try {
      const resp = await fetch("/review/recheck", { method: "POST", body, credentials: "same-origin" });
      if (!resp.ok) {
        setStatus(recheckStatus, await resp.text(), false);
        return;
      }
      const deadline = Date.now() + 6 * 60 * 1000;
      let data = { status: "running" };
      while (data.status === "running" && Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 900));
        const poll = await fetch("/review/recheck/status", { credentials: "same-origin" });
        if (!poll.ok) break;
        data = await poll.json();
      }
      if (data.status === "done") {
        renderRecheck(data);
        setStatus(recheckStatus, data.message || "", !(data.summary || {}).regressions);
      } else if (data.status === "error") {
        setStatus(recheckStatus, data.message || "The re-check failed.", false);
      } else {
        setStatus(recheckStatus, "Still running. Reload this page to see the result.", false);
      }
    } finally {
      window.setBusy("recheck-button", false);
    }
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
