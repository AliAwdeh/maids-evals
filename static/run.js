(() => {
  const providerSel = document.getElementById("provider-select");
  const modelSlot = document.getElementById("model-slot");
  const modelInput = document.getElementById("model-input");
  const modelSelect = document.getElementById("model-select");
  const modelSearch = document.getElementById("model-search");
  const modelRefresh = document.getElementById("model-refresh");
  const promptBox = document.getElementById("prompt-template");
  const inputBox = document.getElementById("input-template");
  const chipTargetStatus = document.getElementById("chip-target-status");
  let insertTarget = inputBox || promptBox;
  const promptSelect = document.getElementById("prompt-select");
  const promptName = document.getElementById("prompt-name");
  const promptLoad = document.getElementById("prompt-load");
  const promptSave = document.getElementById("prompt-save");
  const promptStatus = document.getElementById("prompt-status");
  const runNameInput = document.getElementById("run-name");
  const runForm = document.getElementById("run-form");
  const credsForm = document.getElementById("llm-credentials");
  const llmUser = document.getElementById("llm-username");
  const llmPass = document.getElementById("llm-password");
  const keySave = document.getElementById("key-save");
  const keyForget = document.getElementById("key-forget");
  const keyStatus = document.getElementById("key-status");
  const providerTag = document.getElementById("provider-tag");
  const maxWorkersInput = document.getElementById("max-workers");
  const sendModelParamsInput = document.getElementById("send-model-params");
  const modelParamsBox = document.getElementById("model-params-box");
  const testBtn = document.getElementById("test-button");
  const retestBtn = document.getElementById("retest-button");
  const overlay = document.getElementById("progress-overlay");
  const testOverlay = document.getElementById("test-overlay");
  const divideBtn = document.getElementById("divide-button");
  const divideOverlay = document.getElementById("divide-overlay");
  const sidToken = runForm?.dataset?.sidToken || "";
  const fallbackOllama = ["llama3", "llama3:70b", "gemma3", "mistral-small"];
  const fallbackLangcc = ["gpt-5-mini", "gpt-5", "gpt-4.1-mini", "o4-mini"];
  const KEY_PROVIDERS = ["openai", "langcc", "gemini"];
  const PROVIDER_LABELS = { openai: "OpenAI", langcc: "LangCC", gemini: "Gemini", ollama: "Ollama" };
  let providerModels = {};
  let modelsLoading = {};
  let lastModelKey = "";
  let loadingKeyFor = "";
  let keyModelTimer = null;

  function currentProvider() {
    return (providerSel?.value || "langcc").toLowerCase();
  }

  function providerLabel(provider) {
    return PROVIDER_LABELS[provider] || provider;
  }

  function usesModelList() {
    const provider = currentProvider();
    return provider === "ollama" || provider === "langcc";
  }

  // Which list-based providers require an API key to enumerate models.
  function modelFetchNeedsKey(provider) {
    return provider === "langcc";
  }

  function currentModel() {
    if (usesModelList()) {
      return (modelSelect?.value || modelInput?.value || "").trim();
    }
    return (modelInput?.value || "").trim();
  }

  function showModelError(msg) {
    const el = document.getElementById("model-error");
    if (el) el.textContent = msg || "";
    const progressErr = document.getElementById("progress-error");
    if (progressErr && msg) progressErr.textContent = msg;
  }

  function requireModel() {
    const model = currentModel();
    if (model) {
      showModelError("");
      if (modelInput) modelInput.value = model;
      if (modelSelect && modelSelect.value !== model) {
        const hasOpt = Array.from(modelSelect.options).some((opt) => opt.value === model);
        if (hasOpt) modelSelect.value = model;
      }
      return model;
    }
    showModelError("Model is required. Enter a model name or wait for the list to load.");
    return "";
  }

  let savedKeyExists = false;

  function apiKey() {
    return llmPass?.value || "";
  }

  function haveKey() {
    return !!apiKey().trim() || savedKeyExists;
  }

  function show(el, on) {
    if (!el) return;
    el.classList.toggle("show", !!on);
  }

  function syncSelectToInput() {
    if (modelSelect && modelInput && modelSelect.value) modelInput.value = modelSelect.value;
  }

  function modelCacheKey(provider) {
    const keyPart = provider === "langcc" || provider === "openai" ? (apiKey() || (savedKeyExists ? "saved" : "")) : "";
    return `${provider}:${keyPart}`;
  }

  function setKeyStatus(msg, isError = false) {
    if (!keyStatus) return;
    keyStatus.textContent = msg || "";
    keyStatus.style.color = isError ? "#8d2c2c" : "#2a6b4e";
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
      if (needs) llmPass.value = key || "";
      else llmPass.value = "";
    }
    if (llmUser && !llmUser.dataset.touched) llmUser.value = provider;
    savedKeyExists = !!saved && needs;
    setKeyActions(savedKeyExists);
    if (!needs) setKeyStatus("Ollama does not use an API key.");
    else if (saved) setKeyStatus("Saved for your account only.");
    else setKeyStatus("Optional: save to this account, or let your password manager remember it.");
  }

  function maybeLoadModelsFor(provider) {
    // Only fetch if the user is still on this provider and it uses the list picker.
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
      if (resp.status === 401) {
        setKeyStatus("Log in to load a saved key.", true);
      } else if (resp.ok) {
        const data = await resp.json();
        applyKeyField(provider, "", !!data.saved);
      }
    } catch (_) {
      /* keep whatever is already in the field */
    }
    // Once the saved key is in place, populate the model list for key-based providers.
    maybeLoadModelsFor(provider);
  }

  async function saveCurrentKey() {
    const provider = currentProvider();
    if (!KEY_PROVIDERS.includes(provider)) {
      setKeyStatus("Ollama does not use an API key.");
      return;
    }
    const key = apiKey().trim();
    if (!key) {
      setKeyStatus("Enter an API key to save.", true);
      return;
    }
    const form = new FormData();
    form.append("provider", provider);
    form.append("api_key", key);
    const resp = await fetch("/credentials", { method: "POST", body: form, credentials: "same-origin" });
    if (resp.status === 401) return setKeyStatus("Log in to save a key.", true);
    if (!resp.ok) return setKeyStatus("Could not save key.", true);
    savedKeyExists = true;
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
    if (resp.status === 401) return setKeyStatus("Log in to change saved keys.", true);
    if (!resp.ok) return setKeyStatus("Could not forget key.", true);
    if (llmPass) llmPass.value = "";
    savedKeyExists = false;
    setKeyActions(false);
    setKeyStatus("Saved key removed from your account.");
    if (usesModelList()) loadProviderModels(provider);
  }

  function setModelOptions(models, note) {
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
      opt.textContent = note || "No matching models";
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

  function setRefreshBusy(busy) {
    if (!modelRefresh) return;
    modelRefresh.disabled = busy;
    modelRefresh.textContent = busy ? "Refreshing…" : "Refresh";
  }

  async function loadProviderModels(provider, opts = {}) {
    const force = !!opts.force;
    if (!usesModelList()) return;
    // Providers that need a key can't list models until one is available.
    if (modelFetchNeedsKey(provider) && !haveKey()) {
      setModelPlaceholder("Add an API key, then Refresh");
      showModelError(`Enter or save your ${providerLabel(provider)} API key to load models, then use Refresh.`);
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
    if (force) delete providerModels[cacheKey];
    modelsLoading[cacheKey] = true;
    setRefreshBusy(true);
    setModelPlaceholder("Loading…");
    showModelError("");
    try {
      const form = new FormData();
      form.append("provider", provider);
      form.append("api_key", apiKey());
      const resp = await fetch("/provider/models", { method: "POST", body: form, credentials: "same-origin" });
      if (!resp.ok) throw new Error("request failed");
      const data = await resp.json();
      const models = Array.isArray(data?.models) ? data.models : [];
      if (!models.length) throw new Error("no models");
      providerModels[cacheKey] = models;
      if (lastModelKey === cacheKey) {
        setModelOptions(models);
        showModelError("");
      }
    } catch (err) {
      if (lastModelKey === cacheKey) {
        const fallback = provider === "ollama" ? fallbackOllama : (provider === "langcc" ? fallbackLangcc : null);
        if (fallback) {
          setModelOptions(fallback);
          showModelError("Could not reach the model list — showing defaults. Check the API key or connection, then Refresh.");
        } else {
          setModelPlaceholder("No models — Refresh to retry");
          showModelError("Could not load models. Check the API key or connection, then Refresh.");
        }
      }
    } finally {
      modelsLoading[cacheKey] = false;
      setRefreshBusy(false);
    }
  }

  function refreshModels() {
    const provider = currentProvider();
    if (!usesModelList()) return;
    loadProviderModels(provider, { force: true });
  }

  function syncParamVisibility() {
    const showOpenAI = currentProvider() === "openai" || currentProvider() === "langcc";
    if (modelParamsBox) modelParamsBox.classList.toggle("is-openai", showOpenAI);
  }

  function syncModelForProvider() {
    const provider = currentProvider();
    if (llmUser && !llmUser.dataset.touched) llmUser.value = provider;
    if (providerTag) providerTag.textContent = provider;
    const usesList = usesModelList();
    if (modelSlot) {
      modelSlot.classList.toggle("is-list", usesList);
      modelSlot.classList.toggle("is-text", !usesList);
    }
    if (modelInput) {
      modelInput.disabled = usesList;
      modelInput.name = usesList ? "" : "model";
    }
    if (modelSelect) {
      modelSelect.disabled = !usesList;
      modelSelect.name = usesList ? "model" : "";
    }
    showModelError("");
    syncParamVisibility();
    if (usesList) loadProviderModels(provider);
    else if (modelInput && !modelInput.value) modelInput.value = "gpt-5-mini";
  }

  if (llmUser) {
    llmUser.addEventListener("input", () => { llmUser.dataset.touched = "1"; });
  }
  if (credsForm) {
    credsForm.addEventListener("submit", (e) => e.preventDefault());
  }
  keySave?.addEventListener("click", (e) => { e.preventDefault(); saveCurrentKey(); });
  keyForget?.addEventListener("click", (e) => { e.preventDefault(); forgetCurrentKey(); });
  providerSel?.addEventListener("change", () => {
    // Drop the previous provider's key so we never list models with a stale key.
    if (llmPass) llmPass.value = "";
    syncModelForProvider();
    loadSavedKey(currentProvider());
    queueSaveState();
  });
  modelSelect?.addEventListener("change", () => {
    syncSelectToInput();
    queueSaveState();
  });
  modelSearch?.addEventListener("input", () => {
    setModelOptions(providerModels[modelCacheKey(currentProvider())] || []);
  });
  modelRefresh?.addEventListener("click", (e) => { e.preventDefault(); refreshModels(); });
  llmPass?.addEventListener("input", () => {
    const provider = currentProvider();
    if (!(usesModelList() && modelFetchNeedsKey(provider))) return;
    if (keyModelTimer) clearTimeout(keyModelTimer);
    keyModelTimer = setTimeout(() => loadProviderModels(provider, { force: true }), 500);
  });
  sendModelParamsInput?.addEventListener("change", () => {
    syncParamVisibility();
    queueSaveState();
  });
  syncModelForProvider();
  loadSavedKey(currentProvider());

  function setInsertTarget(el) {
    if (!el) return;
    insertTarget = el;
    if (chipTargetStatus) {
      chipTargetStatus.textContent = el === inputBox
        ? "Chips insert into Input data."
        : "Chips insert into Instructions.";
    }
  }
  promptBox?.addEventListener("focus", () => setInsertTarget(promptBox));
  inputBox?.addEventListener("focus", () => setInsertTarget(inputBox));

  document.querySelectorAll(".chip").forEach((btn) => {
    btn.addEventListener("click", () => {
      const box = insertTarget || inputBox || promptBox;
      if (!box) return;
      const text = btn.getAttribute("data-insert") || "";
      const start = box.selectionStart ?? box.value.length;
      const end = box.selectionEnd ?? box.value.length;
      box.value = box.value.slice(0, start) + text + box.value.slice(end);
      box.focus();
      box.setSelectionRange(start + text.length, start + text.length);
      setInsertTarget(box);
      queueSaveState();
    });
  });

  ["input", "change"].forEach((evt) => {
    promptBox?.addEventListener(evt, () => queueSaveState());
    inputBox?.addEventListener(evt, () => queueSaveState());
    promptName?.addEventListener(evt, () => queueSaveState());
    runNameInput?.addEventListener(evt, () => queueSaveState());
    modelInput?.addEventListener(evt, () => queueSaveState());
    maxWorkersInput?.addEventListener(evt, () => queueSaveState());
  });

  let saveTimer = null;
  function buildStateForm() {
    if (!runForm) return null;
    const form = new FormData();
    if (sidToken) form.append("sid_token", sidToken);
    if (providerSel) form.append("provider", providerSel.value);
    form.append("model", currentModel());
    if (promptBox) form.append("prompt_template", promptBox.value);
    if (inputBox) form.append("input_template", inputBox.value);
    const jsonMode = document.querySelector('input[name="json_mode"]');
    form.append("json_mode", jsonMode?.checked ? "1" : "0");
    if (maxWorkersInput) form.append("max_workers", maxWorkersInput.value);
    if (promptName) form.append("prompt_name", promptName.value);
    if (runNameInput) form.append("run_name", runNameInput.value);
    form.append("send_model_params", sendModelParamsInput?.checked ? "1" : "0");
    document.querySelectorAll('#model-params-box input[name="enabled_params"]:checked').forEach((el) => form.append("enabled_params", el.value));
    document.querySelectorAll('#model-params-box input:not([name="enabled_params"]), #model-params-box select').forEach((el) => {
      if (el.name) form.append(el.name, el.value);
    });
    return form;
  }

  function queueSaveState(immediate = false) {
    if (saveTimer) clearTimeout(saveTimer);
    const send = () => {
      const form = buildStateForm();
      if (!form) return;
      fetch("/session/save", { method: "POST", body: form, credentials: "same-origin", keepalive: immediate });
    };
    if (immediate) send();
    else saveTimer = setTimeout(send, 400);
  }

  function setPromptStatus(msg, isError = false) {
    if (!promptStatus) return;
    promptStatus.textContent = msg;
    promptStatus.style.color = isError ? "#8d2c2c" : (msg ? "#2a6b4e" : "");
  }

  const PLACEHOLDER_RE = /\{([a-zA-Z_][a-zA-Z0-9_]*)\}/g;
  let columnMap = Object.assign({}, (window.MAIDS || {}).columnMap || {});

  function placeholderNames() {
    const text = `${promptBox?.value || ""}\n${inputBox?.value || ""}`;
    const names = [];
    let match;
    PLACEHOLDER_RE.lastIndex = 0;
    while ((match = PLACEHOLDER_RE.exec(text))) {
      const name = match[1];
      if (name === "row_json" || names.includes(name)) continue;
      names.push(name);
    }
    return names;
  }

  function fillSelect(sel, name, columns) {
    sel.innerHTML = "";
    const blank = document.createElement("option");
    blank.value = "";
    blank.textContent = "Pick a column";
    sel.appendChild(blank);
    const selected = columnMap[name] || ((columns || []).includes(name) ? name : "");
    (columns || []).forEach((col) => {
      const opt = document.createElement("option");
      opt.value = col;
      opt.textContent = col;
      if (col === selected) opt.selected = true;
      sel.appendChild(opt);
    });
  }

  function renderMapRows(host, names, columns) {
    if (!host) return;
    host.innerHTML = "";
    if (!names.length) {
      host.innerHTML = `<p class="small">No {fields} in the prompt yet.</p>`;
      return;
    }
    names.forEach((name) => {
      const row = document.createElement("div");
      row.className = "map-row";
      const label = document.createElement("strong");
      label.textContent = `{${name}}`;
      const sel = document.createElement("select");
      sel.dataset.placeholder = name;
      fillSelect(sel, name, columns);
      row.appendChild(label);
      row.appendChild(sel);
      host.appendChild(row);
    });
  }

  function setMapTag(needed) {
    const tag = document.getElementById("map-tag");
    if (!tag) return;
    if (needed && needed.length) {
      tag.textContent = "Needs mapping";
      tag.className = "tag warn";
    } else if (Object.keys(columnMap).length) {
      tag.textContent = "Mapped";
      tag.className = "tag ok";
    } else {
      tag.textContent = "Match names or map";
      tag.className = "tag";
    }
  }

  function refreshMappingPanel(needed) {
    const columns = (window.MAIDS || {}).csvCols || [];
    const names = placeholderNames();
    const extra = Object.keys(columnMap).filter((name) => !names.includes(name));
    renderMapRows(document.getElementById("map-inline-rows"), names.concat(extra), columns);
    renderMapRows(document.getElementById("map-rows"), needed && needed.length ? needed : names, columns);
    setMapTag(needed);
  }

  function collectMapping(root) {
    const mapping = Object.assign({}, columnMap);
    (root || document).querySelectorAll("select[data-placeholder]").forEach((sel) => {
      const name = sel.dataset.placeholder;
      if (!name) return;
      if (sel.value) mapping[name] = sel.value;
      else delete mapping[name];
    });
    return mapping;
  }

  function showMapping(needed, columns) {
    if (columns) window.MAIDS.csvCols = columns;
    refreshMappingPanel(needed);
    const overlay = document.getElementById("map-overlay");
    if (overlay) overlay.style.display = needed && needed.length ? "flex" : "none";
  }

  function filterPromptOptions() {
    const q = (document.getElementById("prompt-search")?.value || "").trim().toLowerCase();
    if (!promptSelect) return;
    Array.from(promptSelect.options).forEach((opt, idx) => {
      if (idx === 0) return;
      const hay = `${opt.dataset.name || ""} ${opt.dataset.owner || ""} ${opt.textContent || ""}`.toLowerCase();
      opt.hidden = !!(q && !hay.includes(q));
    });
  }

  document.getElementById("prompt-search")?.addEventListener("input", filterPromptOptions);

  document.getElementById("prompt-last")?.addEventListener("click", async () => {
    const id = (window.MAIDS || {}).lastPromptId || "";
    if (!id) return setPromptStatus("No last used prompt yet.", true);
    if (promptSelect) promptSelect.value = id;
    promptLoad?.click();
  });

  promptLoad?.addEventListener("click", async () => {
    const id = promptSelect?.value || "";
    if (!id) return setPromptStatus("Select a prompt to load.", true);
    setPromptStatus("Loading prompt…");
    await window.withBusy(promptLoad, "Loading…", async () => {
      const resp = await fetch(`/prompts/get?id=${encodeURIComponent(id)}`, { credentials: "same-origin" });
      if (!resp.ok) return setPromptStatus("Failed to load prompt.", true);
      const data = await resp.json();
      if (promptBox) promptBox.value = data.content || "";
      if (inputBox) inputBox.value = data.input_template || "";
      setPromptStatus(`Loaded "${data.name || id}"`);
      showMapping(data.mapping_needed || [], data.columns || (window.MAIDS || {}).csvCols || []);
      if ((data.mapping_needed || []).length) {
        setPromptStatus("This prompt needs columns mapped to your sheet. You can change the mapping below.");
      }
      queueSaveState();
    });
  });

  async function saveMapping(fromOverlay) {
    const root = fromOverlay ? document.getElementById("map-overlay") : document.getElementById("map-panel");
    const mapping = collectMapping(root);
    const btn = fromOverlay ? document.getElementById("map-save") : document.getElementById("map-inline-save");
    const status = document.getElementById(fromOverlay ? "map-status" : "map-inline-status");
    if (status) status.textContent = "Saving mapping…";
    return window.withBusy(btn, "Saving…", async () => {
      const resp = await fetch("/mapping/save", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({
          mapping,
          prompt_template: promptBox?.value || "",
          input_template: inputBox?.value || "",
        }),
      });
      if (!resp.ok) {
        if (status) status.textContent = "Could not save mapping.";
        return;
      }
      const data = await resp.json();
      columnMap = data.mapping || mapping;
      if (status) status.textContent = data.mapping_needed && data.mapping_needed.length
        ? "Still missing: " + data.mapping_needed.join(", ")
        : "Mapping saved. You can change it any time.";
      const other = document.getElementById(fromOverlay ? "map-inline-status" : "map-status");
      if (other && other !== status) other.textContent = status.textContent;
      refreshMappingPanel(data.mapping_needed || []);
      if (fromOverlay && !(data.mapping_needed || []).length) {
        const overlay = document.getElementById("map-overlay");
        if (overlay) overlay.style.display = "none";
      }
    });
  }

  document.getElementById("map-cancel")?.addEventListener("click", () => {
    const overlay = document.getElementById("map-overlay");
    if (overlay) overlay.style.display = "none";
  });
  document.getElementById("map-save")?.addEventListener("click", () => saveMapping(true));
  document.getElementById("map-inline-save")?.addEventListener("click", () => saveMapping(false));

  let mapTimer = null;
  [promptBox, inputBox].forEach((box) => {
    box?.addEventListener("input", () => {
      clearTimeout(mapTimer);
      mapTimer = setTimeout(() => refreshMappingPanel(), 250);
    });
  });
  refreshMappingPanel((window.MAIDS || {}).mappingNeeded || []);

  window.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") queueSaveState(true);
  });

  function attachApiKey(formData) {
    formData.set("api_key", apiKey());
    const keyPick = document.getElementById("key-pick");
    if (keyPick && keyPick.value) formData.set("key_id", keyPick.value);
    if (sidToken) formData.set("sid_token", sidToken);
    if (providerSel) formData.set("provider", providerSel.value);
    formData.set("model", currentModel());
    if (promptBox) formData.set("prompt_template", promptBox.value);
    if (inputBox) formData.set("input_template", inputBox.value);
    if (runNameInput) formData.set("run_name", runNameInput.value);
    if (maxWorkersInput) formData.set("max_workers", maxWorkersInput.value);
    const jsonMode = document.querySelector('input[name="json_mode"]');
    formData.set("json_mode", jsonMode?.checked ? "1" : "0");
    formData.set("send_model_params", sendModelParamsInput?.checked ? "1" : "0");
    return formData;
  }

  function submitTest(rowIdx = "") {
    if (!runForm) return;
    const model = requireModel();
    if (!model) return;
    runForm.querySelectorAll('input[data-test-copy="1"]').forEach((el) => el.remove());
    document.querySelectorAll('#test-overlay input[name="test_cols"]:checked').forEach((chk) => {
      const hidden = document.createElement("input");
      hidden.type = "hidden";
      hidden.name = "test_cols";
      hidden.value = chk.value;
      hidden.setAttribute("data-test-copy", "1");
      runForm.appendChild(hidden);
    });
    if (rowIdx !== "" && rowIdx !== null && rowIdx !== undefined) {
      const hidden = document.createElement("input");
      hidden.type = "hidden";
      hidden.name = "test_row_idx";
      hidden.value = String(rowIdx);
      hidden.setAttribute("data-test-copy", "1");
      runForm.appendChild(hidden);
    }
    const keyField = document.createElement("input");
    keyField.type = "hidden";
    keyField.name = "api_key";
    keyField.value = apiKey();
    keyField.setAttribute("data-test-copy", "1");
    runForm.appendChild(keyField);
    const modelField = document.createElement("input");
    modelField.type = "hidden";
    modelField.name = "model";
    modelField.value = model;
    modelField.setAttribute("data-test-copy", "1");
    runForm.appendChild(modelField);
    if (modelInput) modelInput.disabled = true;
    if (modelSelect) modelSelect.disabled = true;
    runForm.dataset.mode = "test";
    const original = runForm.action;
    runForm.action = "/test";
    runForm.submit();
    show(testOverlay, false);
    setTimeout(() => {
      runForm.action = original;
      runForm.dataset.mode = "";
      syncModelForProvider();
    }, 0);
  }

  testBtn?.addEventListener("click", (e) => { e.preventDefault(); show(testOverlay, true); });
  document.getElementById("test-confirm")?.addEventListener("click", () => {
    window.setBusy("test-confirm", true, "Starting…");
    submitTest("");
  });
  retestBtn?.addEventListener("click", (e) => {
    e.preventDefault();
    if (retestBtn.disabled) return;
    window.setBusy(retestBtn, true, "Testing…");
    submitTest(window.MAIDS?.lastTestRow ?? "");
  });
  document.getElementById("test-cancel")?.addEventListener("click", () => show(testOverlay, false));
  divideBtn?.addEventListener("click", (e) => { e.preventDefault(); show(divideOverlay, true); });
  document.getElementById("divide-cancel")?.addEventListener("click", () => show(divideOverlay, false));

  async function pollProgress() {
    const qs = sidToken ? `?sid_token=${encodeURIComponent(sidToken)}` : "";
    const resp = await fetch(`/progress${qs}`, { credentials: "same-origin" });
    const data = await resp.json();
    const done = data?.done ?? 0;
    const total = data?.total ?? 0;
    const sent = data?.sent ?? done;
    const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
    set("progress-done", done);
    set("progress-total", total);
    set("progress-running", Math.max(sent - done, 0));
    set("progress-pending", Math.max(total - sent, 0));
    const err = document.getElementById("progress-error");
    if (err) {
      if (data?.status === "error") err.textContent = data.error || "";
      else if (data?.status === "cancelled") err.textContent = data.message || "Run stopped.";
      else err.textContent = "";
    }
    return data?.status ?? "idle";
  }

  function showStopControls(running) {
    const stop = document.getElementById("progress-stop");
    const close = document.getElementById("progress-close");
    if (stop) stop.hidden = !running;
    if (close) close.hidden = !!running;
  }

  document.getElementById("progress-stop")?.addEventListener("click", async () => {
    const body = new FormData();
    if (sidToken) body.append("sid_token", sidToken);
    window.setBusy("progress-stop", true, "Stopping…");
    try {
      await fetch("/run/cancel", { method: "POST", body, credentials: "same-origin" });
      const err = document.getElementById("progress-error");
      if (err) err.textContent = "Stopping. Rows already sent will finish.";
    } finally {
      window.setBusy("progress-stop", false);
      showStopControls(false);
    }
  });

  document.getElementById("progress-close")?.addEventListener("click", () => {
    show(overlay, false);
    window.setBusy("run-button", false);
  });

  runForm?.addEventListener("submit", async (e) => {
    if (runForm.dataset.mode === "test") {
      runForm.dataset.mode = "";
      return;
    }
    e.preventDefault();
    const model = requireModel();
    if (!model) return;
    window.setBusy("run-button", true, "Starting…");
    show(overlay, true);
    const formData = attachApiKey(new FormData(runForm));
    formData.set("model", model);
    const resp = await fetch("/run", { method: "POST", body: formData, credentials: "same-origin" }).catch(() => null);
    if (!resp || !resp.ok) {
      // A rejected run used to leave the modal up and the button stuck on
      // "Starting…", so the message explaining why was hidden behind it.
      const err = document.getElementById("progress-error");
      if (err) err.textContent = resp ? await resp.text() : "Run failed.";
      window.setBusy("run-button", false);
      showStopControls(false);
      return;
    }
    showStopControls(true);
    let status = await pollProgress();
    while (status === "running") {
      await new Promise((r) => setTimeout(r, 500));
      status = await pollProgress();
    }
    window.setBusy("run-button", false);
    if (status === "error" || status === "cancelled") {
      showStopControls(false);
      return;
    }
    window.location.href = "/export";
  });
})();
