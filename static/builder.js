(() => {
  const cfg = window.MAIDS_BUILDER || {};
  const providerSel = document.getElementById("provider-select");
  const modelSlot = document.getElementById("model-slot");
  const modelInput = document.getElementById("model-input");
  const modelSelect = document.getElementById("model-select");
  const modelSearch = document.getElementById("model-search");
  const modelRefresh = document.getElementById("model-refresh");
  const credsForm = document.getElementById("llm-credentials");
  const llmUser = document.getElementById("llm-username");
  const llmPass = document.getElementById("llm-password");
  const keySave = document.getElementById("key-save");
  const keyForget = document.getElementById("key-forget");
  const keyStatus = document.getElementById("key-status");
  const providerTag = document.getElementById("provider-tag");
  const contentInput = document.getElementById("content-col");
  const contentTag = document.getElementById("content-tag");
  const fallbackOllama = ["llama3", "llama3:70b", "gemma3", "mistral-small"];
  const fallbackLangcc = ["gpt-5-mini", "gpt-5", "gpt-4.1-mini", "o4-mini"];
  const KEY_PROVIDERS = ["openai", "langcc", "gemini"];
  const PROVIDER_LABELS = { openai: "OpenAI", langcc: "LangCC", gemini: "Gemini", ollama: "Ollama" };
  let providerModels = {};
  let modelsLoading = {};
  let lastModelKey = "";
  let loadingKeyFor = "";
  let lastTestRow = cfg.lastTestRow;
  let discoverMode = "quick";

  function currentProvider() {
    return (providerSel?.value || "langcc").toLowerCase();
  }
  function providerLabel(provider) {
    return PROVIDER_LABELS[provider] || provider;
  }
  function usesModelList() {
    const p = currentProvider();
    return p === "ollama" || p === "langcc";
  }
  function modelFetchNeedsKey(provider) {
    return provider === "langcc";
  }
  let savedKeyExists = false;

  function apiKey() {
    return llmPass?.value || "";
  }

  function haveKey() {
    return !!apiKey().trim() || savedKeyExists;
  }
  function currentModel() {
    if (usesModelList()) return (modelSelect?.value || modelInput?.value || "").trim();
    return (modelInput?.value || "").trim();
  }
  function setMsg(id, msg, isError) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = msg || "";
    el.style.color = isError ? "var(--bad)" : "";
  }
  function setBusy(ids, busy, busyLabel) {
    (ids || []).forEach((id) => {
      const el = document.getElementById(id);
      if (!el) return;
      if (busy) {
        if (!el.dataset.label) el.dataset.label = el.textContent;
        el.disabled = true;
        el.classList.add("is-busy");
        if (busyLabel) el.textContent = busyLabel;
      } else {
        el.disabled = false;
        el.classList.remove("is-busy");
        if (el.dataset.label) el.textContent = el.dataset.label;
      }
    });
  }
  function statusNearQuestions(msg, isError) {
    setMsg("questions-status", msg, isError);
    setMsg("discover-status", msg, isError);
    const host = document.getElementById("step-questions");
    if (host && msg) host.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
  function showModelError(msg) {
    setMsg("model-error", msg, !!msg);
  }
  function syncSelectToInput() {
    if (modelSelect && modelInput && modelSelect.value) modelInput.value = modelSelect.value;
  }
  function modelCacheKey(provider) {
    const keyPart = provider === "langcc" || provider === "openai" ? (apiKey() || (savedKeyExists ? "saved" : "")) : "";
    return `${provider}:${keyPart}`;
  }
  function setKeyStatus(msg, isError) {
    if (!keyStatus) return;
    keyStatus.textContent = msg || "";
    keyStatus.style.color = isError ? "var(--bad)" : "var(--ok)";
  }
  function setKeyActions(saved) {
    if (keyForget) keyForget.disabled = !saved;
  }
  function applyKeyField(provider, key, saved) {
    const needs = KEY_PROVIDERS.includes(provider);
    if (llmPass) {
      llmPass.disabled = !needs;
      llmPass.placeholder = !needs
        ? "Not required for Ollama"
        : saved
        ? "Saved key in use — leave blank"
        : "Required for OpenAI, LangCC, Gemini";
      llmPass.value = needs ? (key || "") : "";
    }
    if (llmUser && !llmUser.dataset.touched) llmUser.value = provider;
    savedKeyExists = !!saved && needs;
    setKeyActions(savedKeyExists);
    if (!needs) setKeyStatus("Ollama does not use an API key.");
    else if (saved) setKeyStatus("Saved for your account only.");
    else setKeyStatus("Optional: save to this account.");
  }
  function maybeLoadModelsFor(provider) {
    if (currentProvider() === provider && usesModelList()) loadProviderModels(provider);
  }
  async function loadSavedKey(provider) {
    loadingKeyFor = provider;
    if (!KEY_PROVIDERS.includes(provider)) {
      applyKeyField(provider, "", false);
      maybeLoadModelsFor(provider);
      return;
    }
    try {
      const resp = await fetch(`/credentials?provider=${encodeURIComponent(provider)}`, { credentials: "same-origin" });
      if (loadingKeyFor !== provider) return;
      if (resp.ok) {
        const data = await resp.json();
        applyKeyField(provider, "", !!data.saved);
      }
    } catch (_) { /* keep field */ }
    maybeLoadModelsFor(provider);
  }
  async function saveCurrentKey() {
    const provider = currentProvider();
    if (!KEY_PROVIDERS.includes(provider)) return setKeyStatus("Ollama does not use an API key.");
    const key = apiKey().trim();
    if (!key) return setKeyStatus("Enter an API key to save.", true);
    const form = new FormData();
    form.append("provider", provider);
    form.append("api_key", key);
    const resp = await fetch("/credentials", { method: "POST", body: form, credentials: "same-origin" });
    if (!resp.ok) return setKeyStatus("Could not save key.", true);
    setKeyActions(true);
    setKeyStatus("Saved for your account only.");
    if (usesModelList()) loadProviderModels(provider, { force: true });
  }
  async function forgetCurrentKey() {
    const provider = currentProvider();
    if (!KEY_PROVIDERS.includes(provider)) return;
    const form = new FormData();
    form.append("provider", provider);
    const resp = await fetch("/credentials/forget", { method: "POST", body: form, credentials: "same-origin" });
    if (!resp.ok) return setKeyStatus("Could not forget key.", true);
    if (llmPass) llmPass.value = "";
    setKeyActions(false);
    setKeyStatus("Saved key removed from your account.");
  }
  function setModelOptions(models) {
    if (!modelSelect) return;
    const current = modelInput?.value || "";
    const q = (modelSearch?.value || "").trim().toLowerCase();
    const visible = q ? models.filter((m) => m.toLowerCase().includes(q)) : models;
    modelSelect.innerHTML = "";
    visible.forEach((m) => {
      const opt = document.createElement("option");
      opt.value = m;
      opt.textContent = m;
      modelSelect.appendChild(opt);
    });
    if (visible.length) {
      modelSelect.value = visible.includes(current) ? current : visible[0];
      syncSelectToInput();
    } else {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "No matching models";
      modelSelect.appendChild(opt);
    }
  }
  function setModelPlaceholder(text) {
    if (!modelSelect) return;
    modelSelect.innerHTML = "";
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = text;
    modelSelect.appendChild(opt);
  }
  async function loadProviderModels(provider, opts = {}) {
    const force = !!opts.force;
    if (!usesModelList()) return;
    if (modelFetchNeedsKey(provider) && !haveKey()) {
      setModelPlaceholder("Add an API key, then Refresh");
      showModelError(`Enter or save your ${providerLabel(provider)} API key, then use Refresh.`);
      return;
    }
    const cacheKey = modelCacheKey(provider);
    lastModelKey = cacheKey;
    if (!force && providerModels[cacheKey]) {
      setModelOptions(providerModels[cacheKey]);
      showModelError("");
      return;
    }
    if (!force && modelsLoading[cacheKey]) return;
    modelsLoading[cacheKey] = true;
    if (modelRefresh) modelRefresh.textContent = "Refreshing…";
    setModelPlaceholder("Loading…");
    try {
      const form = new FormData();
      form.append("provider", provider);
      form.append("api_key", apiKey());
      const resp = await fetch("/provider/models", { method: "POST", body: form, credentials: "same-origin" });
      if (!resp.ok) throw new Error("failed");
      const data = await resp.json();
      const models = Array.isArray(data?.models) ? data.models : [];
      if (!models.length) throw new Error("empty");
      providerModels[cacheKey] = models;
      if (lastModelKey === cacheKey) {
        setModelOptions(models);
        showModelError("");
      }
    } catch (_) {
      providerModels[cacheKey] = provider === "ollama" ? fallbackOllama : fallbackLangcc;
      if (lastModelKey === cacheKey) {
        setModelOptions(providerModels[cacheKey]);
        showModelError("Using a short fallback list. Refresh after the key is in.");
      }
    } finally {
      modelsLoading[cacheKey] = false;
      if (modelRefresh) modelRefresh.textContent = "Refresh";
    }
  }
  function syncModelSlot() {
    const list = usesModelList();
    if (modelSlot) {
      modelSlot.classList.toggle("is-list", list);
      modelSlot.classList.toggle("is-text", !list);
    }
    if (modelInput) modelInput.disabled = list;
    if (modelSelect) modelSelect.disabled = !list;
    if (providerTag) providerTag.textContent = currentProvider();
    if (!list && modelInput && !modelInput.value) modelInput.value = "gpt-5-mini";
  }

  credsForm?.addEventListener("submit", (e) => e.preventDefault());
  llmUser?.addEventListener("input", () => { llmUser.dataset.touched = "1"; });
  keySave?.addEventListener("click", saveCurrentKey);
  keyForget?.addEventListener("click", forgetCurrentKey);
  providerSel?.addEventListener("change", () => {
    syncModelSlot();
    loadSavedKey(currentProvider());
  });
  modelSelect?.addEventListener("change", syncSelectToInput);
  modelSearch?.addEventListener("input", () => {
    const cached = providerModels[modelCacheKey(currentProvider())];
    if (cached) setModelOptions(cached);
  });
  modelRefresh?.addEventListener("click", () => loadProviderModels(currentProvider(), { force: true }));
  syncModelSlot();
  loadSavedKey(currentProvider());

  function pickColumn(btn) {
    document.querySelectorAll("#column-chips .chip").forEach((c) => c.classList.remove("is-picked"));
    btn.classList.add("is-picked");
    if (contentInput) contentInput.value = btn.getAttribute("data-col") || "";
    if (contentTag) {
      contentTag.textContent = contentInput.value;
      contentTag.classList.remove("warn");
      contentTag.classList.add("ok");
    }
  }

  document.querySelectorAll("#column-chips .chip[data-col]").forEach((btn) => {
    btn.addEventListener("click", () => pickColumn(btn));
  });

  /* ======================================================================
     Uploading. Picking a file starts it, and the reply is data rather than
     a redirect, so the goal someone has already written stays on screen.
     ====================================================================== */

  function applyBuilderDataset(data) {
    const columns = data.columns || [];
    cfg.hasFile = (data.rows || 0) > 0;

    const section = document.getElementById("step-data");
    if (section) section.classList.add("done");
    const tag = section?.querySelector(".step-tag");
    if (tag) {
      tag.textContent = `${data.rows} rows · ${columns.length} columns`;
      tag.className = "tag ok step-tag";
    }

    const preview = document.getElementById("builder-columns-preview");
    if (preview) {
      preview.innerHTML = "";
      columns.slice(0, 14).forEach((name) => {
        const chip = document.createElement("span");
        chip.className = "tag";
        chip.textContent = name;
        preview.appendChild(chip);
      });
      if (columns.length > 14) {
        const more = document.createElement("span");
        more.className = "chip-empty";
        more.textContent = `+${columns.length - 14} more`;
        preview.appendChild(more);
      }
      preview.hidden = !columns.length;
    }

    // The column you click to say where the text lives.
    const chips = document.getElementById("column-chips");
    if (chips) {
      const previous = contentInput?.value || "";
      chips.innerHTML = "";
      columns.forEach((name) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "chip";
        btn.dataset.col = name;
        btn.textContent = name;
        btn.addEventListener("click", () => pickColumn(btn));
        chips.appendChild(btn);
      });
      // A new sheet may not have the column that was chosen for the old one.
      if (previous && columns.includes(previous)) {
        const same = chips.querySelector(`.chip[data-col="${CSS.escape(previous)}"]`);
        if (same) pickColumn(same);
      } else if (contentInput) {
        contentInput.value = "";
        if (contentTag) {
          contentTag.textContent = "Pick a column";
          contentTag.classList.remove("ok");
          contentTag.classList.add("warn");
        }
      }
    }

    ["discover-button", "deep-button", "availability-button"].forEach((id) => {
      const btn = document.getElementById(id);
      if (btn) btn.disabled = false;
    });
    const inline = document.getElementById("avail-inline");
    if (inline) inline.hidden = true;
  }

  window.autoUpload({
    input: "builder-file",
    form: "builder-upload-form",
    status: "builder-upload-status",
    busy: "builder-upload-button",
    next: "/builder",
    onLoaded: applyBuilderDataset,
  });

  function requireReady() {
    if (!cfg.hasFile) {
      statusNearQuestions("Upload a spreadsheet first.", true);
      return false;
    }
    const goal = (document.getElementById("builder-goal")?.value || "").trim();
    if (!goal) {
      statusNearQuestions("Say what you want to find out.", true);
      return false;
    }
    if (!(contentInput?.value || "").trim()) {
      statusNearQuestions("Click the column that holds the main text.", true);
      return false;
    }
    if (!currentModel()) {
      showModelError("Pick a model first.");
      statusNearQuestions("Pick a model first.", true);
      return false;
    }
    return true;
  }

  function commonForm() {
    const form = new FormData();
    form.append("provider", currentProvider());
    form.append("model", currentModel());
    form.append("api_key", apiKey());
    form.append("goal", document.getElementById("builder-goal")?.value || "");
    form.append("content_col", contentInput?.value || "");
    form.append("meanings", document.getElementById("builder-meanings")?.value || "");
    return form;
  }

  function setCoverage(plan) {
    const row = document.getElementById("coverage-row");
    const fill = document.getElementById("coverage-fill");
    const label = document.getElementById("coverage-label");
    if (!row) return;
    const show = discoverMode === "deep" && plan && (plan.coverage != null || plan.ready);
    row.hidden = !show;
    if (!show) return;
    const pct = Math.max(0, Math.min(100, Number(plan.coverage) || (plan.ready ? 90 : 0)));
    if (fill) fill.style.width = `${pct}%`;
    if (label) {
      label.textContent = plan.ready
        ? `About ${pct}% — enough to write a detailed prompt.`
        : `About ${pct}% of what is needed. Answer and keep going.`;
    }
  }

  function fillQuestions(questions, opts = {}) {
    const host = document.getElementById("question-slots");
    if (!host) return;
    host.innerHTML = "";
    (questions || []).forEach((q, i) => {
      const slot = document.createElement("div");
      slot.className = "q-slot";
      const label = document.createElement("label");
      label.className = "q-label";
      label.textContent = q.text || "";
      const input = document.createElement("input");
      input.type = "text";
      input.className = "q-answer";
      input.dataset.qid = q.id || `q${i + 1}`;
      input.value = q.answer || "";
      slot.appendChild(label);
      slot.appendChild(input);
      host.appendChild(slot);
    });
    const hint = document.getElementById("questions-hint");
    if (hint) {
      if (discoverMode === "deep") {
        hint.textContent = questions.length
          ? "Deep analysis: answer these, then keep going until the helper has enough for a detailed prompt."
          : "No more questions. You can write the prompt now.";
      } else {
        hint.textContent = questions.length
          ? "At most three questions, and only if something is unclear. Blank answers are fine."
          : "No extra questions this time. You can write the prompt now.";
      }
    }
    const next = document.getElementById("deep-next-button");
    if (next) next.hidden = !(discoverMode === "deep" && questions.length && !opts.ready);
  }

  function fillPlan(plan) {
    const approach = document.getElementById("plan-approach");
    const list = document.getElementById("plan-guesses");
    if (approach) approach.textContent = plan.approach || "";
    if (list) {
      list.innerHTML = "";
      (plan.column_guesses || []).forEach((g) => {
        const li = document.createElement("li");
        li.innerHTML = `<strong></strong> — `;
        li.querySelector("strong").textContent = g.column || "";
        li.appendChild(document.createTextNode(g.likely || ""));
        list.appendChild(li);
      });
      (plan.condition_columns || []).forEach((col) => {
        const li = document.createElement("li");
        li.textContent = `Condition in instructions: {${col}}`;
        list.appendChild(li);
      });
    }
    fillQuestions(plan.questions || [], { ready: !!plan.ready });
    setCoverage(plan);
    const gen = document.getElementById("generate-button");
    if (gen) gen.disabled = false;
  }

  async function runDiscover(mode) {
    if (!requireReady()) return;
    discoverMode = mode;
    const busyIds = ["discover-button", "deep-button", "deep-next-button", "generate-button"];
    setBusy(busyIds, true, mode === "deep" ? "Working…" : "Studying…");
    statusNearQuestions(mode === "deep" ? "Deep pass: studying rows and listing what is still unclear…" : "Looking at a few rows…");
    try {
      const form = commonForm();
      form.append("mode", mode);
      const resp = await fetch("/builder/discover", { method: "POST", body: form, credentials: "same-origin" });
      if (!resp.ok) {
        statusNearQuestions(await resp.text(), true);
        return;
      }
      const data = await resp.json();
      fillPlan(data.plan || {});
      // Scoring the sheet ran alongside the study, so show it here instead of
      // hiding it behind a button that has to be discovered.
      if (data.report) {
        fillAvailability(data.report);
        showAvailabilityInline(data.report, data.stats || {});
      }
      const drawn = `${data.sample_size || 0} rows picked at random out of ${(data.stats || {}).total_rows || 0}`;
      statusNearQuestions(mode === "deep"
        ? `Deep pass on ${drawn}. Answer the questions below, then click Answer and keep going.`
        : `Studied ${drawn}.`);
    } catch (err) {
      statusNearQuestions(err?.message || "Could not reach the helper.", true);
    } finally {
      setBusy(busyIds, false);
    }
  }

  document.getElementById("discover-button")?.addEventListener("click", () => runDiscover("quick"));
  document.getElementById("deep-button")?.addEventListener("click", () => runDiscover("deep"));
  document.getElementById("deep-next-button")?.addEventListener("click", async () => {
    if (!requireReady()) {
      statusNearQuestions("Fill the goal, pick the conversation column, and choose a model first.", true);
      return;
    }
    const busyIds = ["discover-button", "deep-button", "deep-next-button", "generate-button"];
    setBusy(busyIds, true, "Asking…");
    statusNearQuestions("Using your answers and asking the next questions. This can take a minute…");
    try {
      const form = commonForm();
      form.append("answers_json", JSON.stringify(collectAnswers()));
      const resp = await fetch("/builder/deep-next", { method: "POST", body: form, credentials: "same-origin" });
      if (!resp.ok) {
        statusNearQuestions(await resp.text(), true);
        return;
      }
      const data = await resp.json();
      fillPlan(data.plan || {});
      const plan = data.plan || {};
      statusNearQuestions(plan.ready
        ? "Ready enough. Write the prompt when you want."
        : "Next questions are ready. Answer them and keep going, or write the prompt now.");
    } catch (err) {
      statusNearQuestions(err?.message || "The helper did not respond. Try again.", true);
    } finally {
      setBusy(busyIds, false);
    }
  });

  function collectAnswers() {
    const answers = {};
    document.querySelectorAll(".q-answer").forEach((input) => {
      const id = input.dataset.qid;
      if (id) answers[id] = input.value || "";
    });
    return answers;
  }

  document.getElementById("generate-button")?.addEventListener("click", async () => {
    if (!requireReady()) return;
    setMsg("generate-status", "Writing the prompt… this can take a minute.");
    setBusy(["generate-button", "discover-button", "deep-button"], true, "Writing…");
    const form = commonForm();
    form.append("answers_json", JSON.stringify(collectAnswers()));
    let resp;
    try {
      resp = await fetch("/builder/generate", { method: "POST", body: form, credentials: "same-origin" });
    } finally {
      setBusy(["generate-button", "discover-button", "deep-button"], false);
    }
    if (!resp.ok) {
      setMsg("generate-status", await resp.text(), true);
      return;
    }
    const data = await resp.json();
    const box = document.getElementById("builder-prompt");
    if (box) box.value = data.prompt || "";
    const inputBox = document.getElementById("builder-input");
    if (inputBox) inputBox.value = data.input_template || "";
    const tag = document.getElementById("prompt-name-tag");
    if (tag) {
      tag.textContent = data.name || "Saved";
      tag.classList.add("ok");
      tag.classList.remove("warn");
    }
    const summary = document.getElementById("prompt-summary");
    if (summary) summary.textContent = data.summary || `Draft ready as ${data.name}. Save it to the catalogue when you want.`;
    const go = document.getElementById("go-run");
    if (go && data.name) go.textContent = `Use ${data.name} on a full run`;
    ["test-button", "pick-test-button", "fix-button", "save-catalogue"].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.disabled = false;
    });
    setMsg("generate-status", `Draft ready as ${data.name}. Not saved yet, and no row was tested.`);
  });


  // Sheet score, shown in the flow. The numbers in the second line are counted
  // over the whole sheet in code -- they are facts, not the model's impression
  // of five rows.
  function showAvailabilityInline(report, stats) {
    const box = document.getElementById("avail-inline");
    if (!box) return;
    const score = report.score != null ? report.score : null;
    const good = score != null && score >= 80;
    const poor = score != null && score < 60;
    box.className = "fixer-callout " + (poor ? "bad" : good ? "good" : "warn");
    const ico = document.getElementById("avail-inline-ico");
    if (ico) ico.textContent = poor ? "!" : good ? "✓" : "!";
    const head = document.getElementById("avail-inline-head");
    if (head) {
      head.textContent = score == null
        ? "Sheet checked. "
        : `This sheet scores ${score}/100 for what you asked. `;
    }
    const body = document.getElementById("avail-inline-body");
    if (body) body.textContent = report.summary || "";

    const line = document.getElementById("avail-inline-stats");
    if (line) {
      const bits = [];
      if (stats.total_rows) bits.push(`${stats.total_rows} rows`);
      if (stats.content_coverage != null) {
        bits.push(`${stats.content_coverage}% have text in ${stats.content_column}`);
      }
      if (stats.median_content_chars) bits.push(`typical length ${stats.median_content_chars} chars`);
      if ((stats.empty_columns || []).length) {
        bits.push(`always empty: ${stats.empty_columns.slice(0, 4).join(", ")}`);
      }
      line.textContent = bits.join(" · ");
    }

    const more = document.getElementById("avail-more");
    if (more) {
      const hasDetail = (report.helpful_missing || []).length || (report.have || []).length;
      more.hidden = !hasDetail;
    }
    box.hidden = false;
  }

  document.getElementById("avail-more")?.addEventListener("click", () => {
    const overlay = document.getElementById("avail-overlay");
    if (overlay) overlay.style.display = "flex";
  });

  function fillAvailability(report) {
    const score = document.getElementById("avail-score");
    const summary = document.getElementById("avail-summary");
    const have = document.getElementById("avail-have");
    const missing = document.getElementById("avail-missing");
    const qs = document.getElementById("avail-questions");
    if (score) score.textContent = report.score != null ? String(report.score) : "—";
    if (summary) summary.textContent = report.summary || (report.ready ? "The sheet looks complete enough." : "Some extra fields would help.");
    if (have) {
      have.innerHTML = "";
      (report.have || []).forEach((item) => {
        const li = document.createElement("li");
        li.textContent = item;
        have.appendChild(li);
      });
    }
    if (missing) {
      missing.innerHTML = "";
      (report.helpful_missing || []).forEach((item) => {
        const li = document.createElement("li");
        li.textContent = `${item.field}: ${item.why || ""}${item.likely_column ? ` (maybe ${item.likely_column})` : ""}`;
        missing.appendChild(li);
      });
    }
    if (qs) {
      qs.innerHTML = "";
      (report.questions || []).forEach((q) => {
        const slot = document.createElement("div");
        slot.className = "q-slot";
        const label = document.createElement("label");
        label.className = "q-label";
        label.textContent = q.text || "";
        slot.appendChild(label);
        qs.appendChild(slot);
      });
    }
  }

  document.getElementById("availability-button")?.addEventListener("click", async () => {
    if (!requireReady()) return;
    const overlay = document.getElementById("avail-overlay");
    if (overlay) overlay.style.display = "flex";
    setMsg("avail-status", "Scoring how useful this sheet is…");
    setBusy("availability-button", true, "Scoring…");
    let resp;
    try {
      resp = await fetch("/builder/availability", { method: "POST", body: commonForm(), credentials: "same-origin" });
    } finally {
      setBusy("availability-button", false);
    }
    if (!resp.ok) {
      setMsg("avail-status", await resp.text(), true);
      return;
    }
    const data = await resp.json();
    fillAvailability(data.report || {});
    showAvailabilityInline(data.report || {}, (data.report || {}).stats || {});
    setMsg("avail-status", (data.report || {}).ready
      ? "Everything important looks available. You can build the prompt."
      : "Read the missing fields. Add them to the file if you can, or build anyway.");
  });
  document.getElementById("avail-close")?.addEventListener("click", () => {
    const overlay = document.getElementById("avail-overlay");
    if (overlay) overlay.style.display = "none";
  });
  document.getElementById("avail-build")?.addEventListener("click", () => {
    const overlay = document.getElementById("avail-overlay");
    if (overlay) overlay.style.display = "none";
    document.getElementById("generate-button")?.click();
  });

  function renderPreview(preview) {
    const host = document.getElementById("test-preview");
    if (!host) return;
    host.innerHTML = "";
    Object.entries(preview || {}).forEach(([k, v]) => {
      const block = document.createElement("div");
      block.className = "col-block";
      const name = document.createElement("div");
      name.className = "col-name";
      name.textContent = k;
      const val = document.createElement("div");
      val.className = "col-val";
      val.textContent = v == null ? "" : String(v);
      block.appendChild(name);
      block.appendChild(val);
      host.appendChild(block);
    });
  }

  function renderOutput(text) {
    const host = document.getElementById("test-output-host");
    if (!host) return;
    host.innerHTML = "";
    const wrap = document.createElement("div");
    wrap.className = "ai-output";
    wrap.dataset.mode = "inline";
    const raw = document.createElement("div");
    raw.className = "ai-output__raw";
    raw.hidden = true;
    raw.textContent = text || "";
    wrap.appendChild(raw);
    host.appendChild(wrap);
    if (window.initAiOutputs) window.initAiOutputs(host);
  }

  async function runTest(rowIdx) {
    setMsg("test-status", "Testing one row… this can take a minute.");
    setBusy(["test-button", "retest-button", "pick-test-button"], true, "Testing…");
    const form = new FormData();
    form.append("provider", currentProvider());
    form.append("model", currentModel());
    form.append("api_key", apiKey());
    if (rowIdx !== "" && rowIdx !== null && rowIdx !== undefined) {
      form.append("test_row_idx", String(rowIdx));
    }
    let resp;
    try {
      resp = await fetch("/builder/test", { method: "POST", body: form, credentials: "same-origin" });
    } finally {
      setBusy(["test-button", "retest-button", "pick-test-button"], false);
    }
    if (!resp.ok) {
      setMsg("test-status", await resp.text(), true);
      return;
    }
    const data = await resp.json();
    lastTestRow = data.row_idx;
    const tag = document.getElementById("test-tag");
    if (tag) tag.textContent = `Row ${(data.row_idx || 0) + 1} of ${data.total || cfg.totalRows}`;
    const retest = document.getElementById("retest-button");
    if (retest) retest.disabled = lastTestRow === null || lastTestRow === undefined;
    const pick = document.getElementById("row-pick");
    if (pick) pick.value = String((data.row_idx || 0) + 1);
    renderPreview(data.preview || {});
    renderOutput(data.output || "");
    if (data.error) setMsg("test-status", data.error, true);
    else setMsg("test-status", `Done in ${data.latency || 0}s.`);
  }

  document.getElementById("test-button")?.addEventListener("click", () => runTest(""));
  document.getElementById("retest-button")?.addEventListener("click", () => {
    if (lastTestRow === null || lastTestRow === undefined) return;
    runTest(lastTestRow);
  });
  document.getElementById("pick-test-button")?.addEventListener("click", () => {
    const n = Number(document.getElementById("row-pick")?.value || 1);
    runTest(Math.max(0, n - 1));
  });
  document.getElementById("fix-button")?.addEventListener("click", async () => {
    const note = (document.getElementById("fix-note")?.value || "").trim();
    if (!note) return setMsg("fix-status", "Write what you want changed.", true);
    setMsg("fix-status", "Updating the prompt only. This can take a minute…");
    setBusy("fix-button", true, "Fixing…");
    const form = commonForm();
    form.append("note", note);
    let resp;
    try {
      resp = await fetch("/builder/fix", { method: "POST", body: form, credentials: "same-origin" });
    } finally {
      setBusy("fix-button", false);
    }
    if (!resp.ok) {
      setMsg("fix-status", await resp.text(), true);
      return;
    }
    const data = await resp.json();
    const box = document.getElementById("builder-prompt");
    if (box) box.value = data.prompt || "";
    setMsg("fix-status", "Prompt updated. Save it to the catalogue if you want to keep this version.");
  });
  document.getElementById("save-catalogue")?.addEventListener("click", async () => {
    setMsg("save-status", "Saving as only you can see and edit…");
    const form = new FormData();
    form.append("name", document.getElementById("prompt-name-tag")?.textContent || "analysis");
    const resp = await fetch("/builder/save", { method: "POST", body: form, credentials: "same-origin" });
    if (!resp.ok) {
      setMsg("save-status", await resp.text(), true);
      return;
    }
    const data = await resp.json();
    setMsg("save-status", `Saved "${data.name}" to the catalogue (only you). Change access on the Catalogue tab.`);
  });

  document.getElementById("copy-company")?.addEventListener("click", async (ev) => {
    const btn = ev.currentTarget;
    const original = btn.textContent;
    setMsg("copy-company-status", "Copying…");
    try {
      const resp = await fetch("/context/text", { credentials: "same-origin" });
      if (!resp.ok) {
        setMsg("copy-company-status", await resp.text(), true);
        return;
      }
      const data = await resp.json();
      const text = data.text || "";
      try {
        await navigator.clipboard.writeText(text);
      } catch (err) {
        const box = document.getElementById("builder-prompt");
        if (box) {
          const prev = box.value;
          const wasReadOnly = box.readOnly;
          box.readOnly = false;
          box.value = text;
          box.focus();
          box.select();
          document.execCommand("copy");
          box.value = prev;
          box.readOnly = wasReadOnly;
        }
      }
      btn.textContent = "Copied";
      setMsg("copy-company-status", "Copied. Paste it into a prompt only if you want it there.");
      setTimeout(() => {
        btn.textContent = original;
      }, 1500);
    } catch (err) {
      setMsg("copy-company-status", "Could not copy company info.", true);
    }
  });
})();
