(() => {
  const cfg = window.MAIDS_CATALOGUE || {};
  let current = null;
  let proposed = null;
  let page = 1;
  const people = { viewers: [], editors: [] };

  function setMsg(id, msg, isError) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = msg || "";
    el.style.color = isError ? "#8d2c2c" : "";
  }

  function renderChat(history) {
    const log = document.getElementById("chat-log");
    if (!log) return;
    log.innerHTML = "";
    (history || []).forEach((item) => {
      const p = document.createElement("p");
      p.className = item.role === "user" ? "chat-user" : "chat-bot";
      p.textContent = (item.role === "user" ? "You: " : "Helper: ") + (item.text || "");
      log.appendChild(p);
    });
    log.scrollTop = log.scrollHeight;
  }

  function rowButton(item, extra) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "prompt-row";
    btn.dataset.id = item.id;
    const why = extra || item.why || "";
    btn.innerHTML = `<strong></strong><span></span>`;
    btn.querySelector("strong").textContent = item.name || item.id;
    btn.querySelector("span").textContent = why
      ? `${item.owner} · ${item.role || ""} · ${why}`
      : `${item.owner} · ${item.visibility} · ${item.role || ""}`;
    btn.addEventListener("click", () => openPrompt(item.id));
    return btn;
  }

  function renderList(data) {
    const host = document.getElementById("cat-list");
    const pager = document.getElementById("cat-pager");
    if (!host) return;
    host.innerHTML = "";
    const items = data.items || [];
    if (!items.length) {
      host.innerHTML = `<p class="small">No prompts match.</p>`;
    } else {
      items.forEach((item) => host.appendChild(rowButton(item)));
    }
    if (pager) {
      pager.innerHTML = "";
      if ((data.pages || 1) > 1) {
        const prev = document.createElement("button");
        prev.type = "button";
        prev.className = "secondary";
        prev.textContent = "Previous";
        prev.disabled = data.page <= 1;
        prev.addEventListener("click", () => loadList(data.page - 1));
        const next = document.createElement("button");
        next.type = "button";
        next.className = "secondary";
        next.textContent = "Next";
        next.disabled = data.page >= data.pages;
        next.addEventListener("click", () => loadList(data.page + 1));
        const info = document.createElement("span");
        info.className = "small";
        info.textContent = `Page ${data.page} of ${data.pages} · ${data.total} prompts`;
        pager.appendChild(prev);
        pager.appendChild(info);
        pager.appendChild(next);
      }
    }
  }

  async function loadList(nextPage) {
    page = nextPage || 1;
    const q = document.getElementById("cat-search")?.value || "";
    const owner = document.getElementById("cat-owner")?.value || "";
    const access = document.getElementById("cat-access")?.value || "";
    setMsg("cat-list-status", "Loading…");
    const params = new URLSearchParams({ q, owner, access, page: String(page) });
    const resp = await fetch(`/catalogue/search?${params}`, { credentials: "same-origin", headers: { Accept: "application/json" } });
    if (!resp.ok) {
      setMsg("cat-list-status", await resp.text(), true);
      return;
    }
    const data = await resp.json();
    renderList(data);
    setMsg("cat-list-status", data.total ? "" : "Nothing here yet.");
  }

  function renderPeople() {
    const host = document.getElementById("acl-people");
    if (!host) return;
    host.innerHTML = "";
    const add = (name, role) => {
      const row = document.createElement("div");
      row.className = "acl-row";
      row.innerHTML = `<strong></strong> <select></select> <button type="button" class="ghost">Remove</button>`;
      row.querySelector("strong").textContent = name;
      const sel = row.querySelector("select");
      ["viewer", "editor"].forEach((r) => {
        const opt = document.createElement("option");
        opt.value = r;
        opt.textContent = r === "editor" ? "Editor (run + edit)" : "Viewer (run)";
        if (r === role) opt.selected = true;
        sel.appendChild(opt);
      });
      sel.addEventListener("change", () => {
        people.viewers = people.viewers.filter((n) => n !== name);
        people.editors = people.editors.filter((n) => n !== name);
        if (sel.value === "editor") people.editors.push(name);
        else people.viewers.push(name);
      });
      row.querySelector("button").addEventListener("click", () => {
        people.viewers = people.viewers.filter((n) => n !== name);
        people.editors = people.editors.filter((n) => n !== name);
        renderPeople();
      });
      host.appendChild(row);
    };
    people.editors.forEach((n) => add(n, "editor"));
    people.viewers.forEach((n) => add(n, "viewer"));
  }

  function renderRequests(list) {
    const host = document.getElementById("acl-requests");
    if (!host) return;
    host.innerHTML = "";
    (list || []).forEach((name) => {
      const row = document.createElement("div");
      row.className = "acl-row";
      row.innerHTML = `<strong></strong><span class="small">wants edit</span><button type="button" class="secondary">Grant</button><button type="button" class="ghost">Deny</button>`;
      row.querySelector("strong").textContent = name;
      row.querySelector(".secondary").addEventListener("click", () => grantEdit(name, true));
      row.querySelector(".ghost").addEventListener("click", () => grantEdit(name, false));
      host.appendChild(row);
    });
  }

  function addPerson(name, role) {
    if (!name) return;
    people.viewers = people.viewers.filter((n) => n !== name);
    people.editors = people.editors.filter((n) => n !== name);
    if (role === "editor") people.editors.push(name);
    else people.viewers.push(name);
    renderPeople();
  }

  async function grantEdit(name, grant) {
    if (!current) return;
    const form = new FormData();
    form.append("username", name);
    form.append("grant", grant ? "1" : "0");
    const resp = await fetch(`/catalogue/${encodeURIComponent(current.id)}/grant-edit`, { method: "POST", body: form, credentials: "same-origin" });
    if (!resp.ok) return setMsg("d-status", await resp.text(), true);
    const card = await resp.json();
    people.viewers = (card.viewers || []).slice();
    people.editors = (card.editors || []).slice();
    renderPeople();
    renderRequests(card.edit_requests || []);
    setMsg("d-status", grant ? `Granted edit to ${name}.` : `Denied ${name}.`);
  }


  // A shared prompt other people run should never change on the strength of
  // "the helper proposed an edit". Show the lines that move before applying.
  function lineDiff(before, after) {
    const a = (before || "").split("\n");
    const b = (after || "").split("\n");
    const seen = new Map();
    b.forEach((line) => seen.set(line, (seen.get(line) || 0) + 1));
    const kept = new Map();
    a.forEach((line) => {
      const left = seen.get(line) || 0;
      if (left > 0) {
        seen.set(line, left - 1);
        kept.set(line, (kept.get(line) || 0) + 1);
      }
    });
    const out = [];
    const pool = new Map(kept);
    a.forEach((line) => {
      const left = pool.get(line) || 0;
      if (left > 0) pool.set(line, left - 1);
      else out.push({ sign: "-", text: line });
    });
    const pool2 = new Map(kept);
    b.forEach((line) => {
      const left = pool2.get(line) || 0;
      if (left > 0) {
        pool2.set(line, left - 1);
        out.push({ sign: " ", text: line });
      } else {
        out.push({ sign: "+", text: line });
      }
    });
    return out.filter((row, i, all) => row.sign !== " " || all.some((r, j) => Math.abs(i - j) < 3 && r.sign !== " "));
  }

  function renderDiff(before, after) {
    const panel = document.getElementById("chat-diff-panel");
    const host = document.getElementById("chat-diff");
    if (!panel || !host) return;
    const rows = lineDiff(before, after);
    if (!rows.length) {
      panel.hidden = true;
      return;
    }
    host.innerHTML = "";
    rows.forEach((row) => {
      const span = document.createElement("span");
      if (row.sign === "+") span.className = "add";
      else if (row.sign === "-") span.className = "del";
      span.textContent = row.sign + row.text + "\n";
      host.appendChild(span);
    });
    panel.hidden = false;
  }

  function hideProposal() {
    proposed = null;
    const apply = document.getElementById("chat-apply");
    const reject = document.getElementById("chat-reject");
    const panel = document.getElementById("chat-diff-panel");
    if (apply) apply.hidden = true;
    if (reject) reject.hidden = true;
    if (panel) panel.hidden = true;
  }

  function markDirty() {
    const save = document.getElementById("d-save");
    if (!current || !current.can_edit || !save) return;
    const changed =
      (document.getElementById("d-prompt")?.value || "") !== (current.prompt || "") ||
      (document.getElementById("d-input")?.value || "") !== (current.input_template || "");
    save.hidden = !changed;
  }

  async function openPrompt(id) {
    const resp = await fetch(`/catalogue/${encodeURIComponent(id)}`, { credentials: "same-origin", headers: { Accept: "application/json" } });
    if (!resp.ok) {
      setMsg("d-status", await resp.text(), true);
      return;
    }
    current = await resp.json();
    proposed = null;
    document.getElementById("detail-empty").hidden = true;
    document.getElementById("detail").hidden = false;
    document.getElementById("d-name").textContent = current.name || "Prompt";
    document.getElementById("d-role").textContent = current.role || "";
    document.getElementById("d-meta").textContent = `Owner ${current.owner} · ${current.visibility} · updated ${current.updated_at || ""}`;
    document.getElementById("d-purpose").textContent = current.purpose
      ? `This prompt: ${current.purpose}`
      : "";
    const promptBox = document.getElementById("d-prompt");
    const inputBox = document.getElementById("d-input");
    promptBox.value = current.prompt || "";
    inputBox.value = current.input_template || "";
    promptBox.readOnly = !current.can_edit;
    inputBox.readOnly = !current.can_edit;
    document.getElementById("d-required").textContent = (current.required_inputs || []).length
      ? "Required inputs: " + current.required_inputs.join(", ")
      : "No extra required inputs.";
    document.getElementById("d-delete").hidden = !current.can_delete;
    document.getElementById("acl-box").hidden = !current.can_acl;
    document.getElementById("d-request").hidden = current.can_edit;
    document.getElementById("acl-visibility").value = current.visibility || "personal";
    document.getElementById("acl-everyone-role").value = current.everyone_role || "viewer";
    people.viewers = (current.viewers || []).slice();
    people.editors = (current.editors || []).slice();
    renderPeople();
    renderRequests(current.edit_requests || []);
    renderChat(current.chat || []);
    hideProposal();
    const saveBtn = document.getElementById("d-save");
    if (saveBtn) saveBtn.hidden = true;
    const versionPanel = document.getElementById("d-version-panel");
    if (versionPanel) versionPanel.hidden = true;
    setMsg("d-status", "");
    setMsg("chat-status", "");
  }

  document.getElementById("d-prompt")?.addEventListener("input", markDirty);
  document.getElementById("d-input")?.addEventListener("input", markDirty);

  // Editable textareas with no way to save meant a hand-written fix was lost on
  // the next click. Persist straight to the prompt, snapshotting the old text.
  document.getElementById("d-save")?.addEventListener("click", async () => {
    if (!current || !current.can_edit) return;
    const summary = window.prompt("One line describing this edit (kept in the change log):", "") || "";
    const form = new FormData();
    form.append("prompt", document.getElementById("d-prompt").value);
    form.append("input_template", document.getElementById("d-input").value);
    form.append("summary", summary);
    window.setBusy("d-save", true, "Saving…");
    try {
      const resp = await fetch(`/catalogue/${encodeURIComponent(current.id)}/apply`, { method: "POST", body: form, credentials: "same-origin" });
      if (!resp.ok) return setMsg("d-status", await resp.text(), true);
      const item = await resp.json();
      current = { ...current, ...item };
      document.getElementById("d-required").textContent = (item.required_inputs || []).length
        ? "Required inputs: " + item.required_inputs.join(", ")
        : "No extra required inputs.";
      document.getElementById("d-save").hidden = true;
      setMsg("d-status", "Saved. The previous text is kept under Earlier versions.");
    } finally {
      window.setBusy("d-save", false);
    }
  });

  document.getElementById("d-versions")?.addEventListener("click", async () => {
    if (!current) return;
    const panel = document.getElementById("d-version-panel");
    const host = document.getElementById("d-version-list");
    if (!panel || !host) return;
    if (!panel.hidden) {
      panel.hidden = true;
      return;
    }
    const resp = await fetch(`/catalogue/${encodeURIComponent(current.id)}/versions`, { credentials: "same-origin", headers: { Accept: "application/json" } });
    if (!resp.ok) return setMsg("d-status", await resp.text(), true);
    const data = await resp.json();
    host.innerHTML = "";
    const versions = data.versions || [];
    if (!versions.length) {
      host.innerHTML = `<p class="small">No earlier versions yet. One is kept every time this prompt is edited.</p>`;
    }
    versions.forEach((v) => {
      const row = document.createElement("div");
      row.className = "acl-row";
      row.innerHTML = `<strong></strong><span class="small"></span><button type="button" class="ghost">Preview</button><button type="button" class="secondary">Restore</button>`;
      row.querySelector("strong").textContent = v.at || "";
      row.querySelector("span").textContent = `${v.by || "unknown"} · ${v.chars} chars`;
      row.querySelectorAll("button")[0].addEventListener("click", () => {
        document.getElementById("d-prompt").value = v.prompt || "";
        document.getElementById("d-input").value = v.input_template || "";
        markDirty();
        setMsg("d-status", "Previewing an old version. Save changes to keep it, or reopen the prompt to drop it.");
      });
      const restore = row.querySelectorAll("button")[1];
      restore.disabled = !current.can_edit;
      restore.addEventListener("click", async () => {
        if (!window.confirm("Put this version back? The current text is kept as a version.")) return;
        const form = new FormData();
        form.append("index", String(v.index));
        const r = await fetch(`/catalogue/${encodeURIComponent(current.id)}/restore`, { method: "POST", body: form, credentials: "same-origin" });
        if (!r.ok) return setMsg("d-status", await r.text(), true);
        openPrompt(current.id);
        setMsg("d-status", "Restored.");
      });
      host.appendChild(row);
    });
    panel.hidden = false;
  });

  document.getElementById("cat-search-btn")?.addEventListener("click", () => loadList(1));
  document.getElementById("cat-search")?.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") loadList(1);
  });
  document.getElementById("cat-owner")?.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") loadList(1);
  });
  document.getElementById("cat-access")?.addEventListener("change", () => loadList(1));

  document.getElementById("d-use")?.addEventListener("click", async () => {
    if (!current) return;
    const resp = await fetch(`/catalogue/${encodeURIComponent(current.id)}/use`, { method: "POST", credentials: "same-origin" });
    if (!resp.ok) return setMsg("d-status", await resp.text(), true);
    window.location.href = "/";
  });

  document.getElementById("d-clone")?.addEventListener("click", async () => {
    if (!current) return;
    setMsg("d-status", "Cloning… the copy gets its own conversation.");
    window.setBusy("d-clone", true, "Cloning…");
    try {
      const resp = await fetch(`/catalogue/${encodeURIComponent(current.id)}/clone`, { method: "POST", credentials: "same-origin" });
      if (!resp.ok) {
        setMsg("d-status", await resp.text(), true);
        return;
      }
      const data = await resp.json();
      await loadList(page);
      if (data.id) openPrompt(data.id);
    } finally {
      window.setBusy("d-clone", false);
    }
  });

  document.getElementById("d-request")?.addEventListener("click", async () => {
    if (!current) return;
    const resp = await fetch(`/catalogue/${encodeURIComponent(current.id)}/request-edit`, { method: "POST", credentials: "same-origin" });
    if (!resp.ok) return setMsg("d-status", await resp.text(), true);
    setMsg("d-status", "Edit access requested from the owner.");
  });

  document.getElementById("d-delete")?.addEventListener("click", async () => {
    if (!current || !window.confirm("Delete this prompt? Only you can do this.")) return;
    const resp = await fetch(`/catalogue/${encodeURIComponent(current.id)}/delete`, { method: "POST", credentials: "same-origin" });
    if (!resp.ok) return setMsg("d-status", await resp.text(), true);
    current = null;
    document.getElementById("detail").hidden = true;
    document.getElementById("detail-empty").hidden = false;
    loadList(page);
  });

  document.getElementById("acl-save")?.addEventListener("click", async () => {
    if (!current) return;
    const form = new FormData();
    form.append("visibility", document.getElementById("acl-visibility").value);
    form.append("everyone_role", document.getElementById("acl-everyone-role").value);
    form.append("viewers", JSON.stringify(people.viewers));
    form.append("editors", JSON.stringify(people.editors));
    const resp = await fetch(`/catalogue/${encodeURIComponent(current.id)}/acl`, { method: "POST", body: form, credentials: "same-origin" });
    if (!resp.ok) return setMsg("d-status", await resp.text(), true);
    setMsg("d-status", "Access saved.");
  });

  let searchTimer = null;
  document.getElementById("user-search")?.addEventListener("input", () => {
    const q = document.getElementById("user-search").value.trim();
    clearTimeout(searchTimer);
    searchTimer = setTimeout(async () => {
      const hits = document.getElementById("user-hits");
      if (!hits) return;
      hits.innerHTML = "";
      if (!q) return;
      const resp = await fetch(`/users/search?q=${encodeURIComponent(q)}`, { credentials: "same-origin" });
      if (!resp.ok) return;
      const data = await resp.json();
      (data.users || []).forEach((name) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "chip";
        btn.textContent = name + " · viewer";
        btn.addEventListener("click", () => addPerson(name, "viewer"));
        const edit = document.createElement("button");
        edit.type = "button";
        edit.className = "chip";
        edit.textContent = name + " · editor";
        edit.addEventListener("click", () => addPerson(name, "editor"));
        hits.appendChild(btn);
        hits.appendChild(edit);
      });
    }, 200);
  });

  function noEditMessage() {
    return "You don't have edit access. Clone this prompt or request edit access from the owner.";
  }

  document.getElementById("chat-send")?.addEventListener("click", async () => {
    if (!current) return;
    const message = (document.getElementById("chat-input")?.value || "").trim();
    if (!message) return setMsg("chat-status", "Write a message.", true);
    setMsg("chat-status", "Thinking… this can take a minute.");
    window.setBusy(["chat-send", "chat-apply"], true, "Thinking…");
    const form = new FormData();
    form.append("message", message);
    form.append("provider", cfg.provider || "");
    form.append("model", cfg.model || "");
    try {
      const resp = await fetch(`/catalogue/${encodeURIComponent(current.id)}/chat`, { method: "POST", body: form, credentials: "same-origin" });
      if (!resp.ok) {
        setMsg("chat-status", await resp.text(), true);
        return;
      }
      const data = await resp.json();
      document.getElementById("chat-input").value = "";
      renderChat(data.chat || []);
      hideProposal();
      if (data.updated_prompt || data.updated_input) {
        if (current.can_edit) {
          proposed = {
            prompt: data.updated_prompt || "",
            input: data.updated_input || "",
            summary: data.change_summary || "",
            purpose: data.purpose || "",
          };
          document.getElementById("chat-apply").hidden = false;
          document.getElementById("chat-reject").hidden = false;
          renderDiff(current.prompt || "", proposed.prompt || current.prompt || "");
          setMsg("chat-status", data.change_summary
            ? `Proposed: ${data.change_summary}. Read the diff, then apply or reject.`
            : "The helper proposed an edit. Read the diff, then apply or reject.");
        } else {
          setMsg("chat-status", noEditMessage(), true);
        }
      } else if (data.wants_edit && !current.can_edit) {
        setMsg("chat-status", noEditMessage(), true);
      } else {
        setMsg("chat-status", "");
      }
    } finally {
      window.setBusy(["chat-send", "chat-apply"], false);
    }
  });

  document.getElementById("chat-apply")?.addEventListener("click", async () => {
    if (!current || !proposed) return;
    if (!current.can_edit) return setMsg("chat-status", noEditMessage(), true);
    setMsg("chat-status", "Applying the edit…");
    window.setBusy("chat-apply", true, "Applying…");
    const form = new FormData();
    form.append("prompt", proposed.prompt || document.getElementById("d-prompt").value);
    form.append("input_template", proposed.input || document.getElementById("d-input").value);
    form.append("summary", proposed.summary || "");
    form.append("purpose", proposed.purpose || "");
    try {
      const resp = await fetch(`/catalogue/${encodeURIComponent(current.id)}/apply`, { method: "POST", body: form, credentials: "same-origin" });
      if (!resp.ok) {
        setMsg("chat-status", await resp.text(), true);
        return;
      }
      const item = await resp.json();
      document.getElementById("d-prompt").value = item.prompt || "";
      document.getElementById("d-input").value = item.input_template || "";
      document.getElementById("d-required").textContent = (item.required_inputs || []).length
        ? "Required inputs: " + item.required_inputs.join(", ")
        : "No extra required inputs.";
      current = { ...current, ...item, can_edit: true };
      hideProposal();
      markDirty();
      setMsg("chat-status", "Edit applied. The previous text is kept under Earlier versions.");
    } finally {
      window.setBusy("chat-apply", false);
    }
  });

  document.getElementById("chat-reject")?.addEventListener("click", () => {
    hideProposal();
    setMsg("chat-status", "Proposal dropped. The prompt is unchanged.");
  });

  document.getElementById("find-send")?.addEventListener("click", async () => {
    const message = (document.getElementById("find-input")?.value || "").trim();
    if (!message) return setMsg("find-status", "Write what you need.", true);
    setMsg("find-status", "Looking through prompts you can use…");
    window.setBusy("find-send", true, "Looking…");
    const form = new FormData();
    form.append("message", message);
    form.append("owner", document.getElementById("cat-owner")?.value || "");
    form.append("provider", cfg.provider || "");
    form.append("model", cfg.model || "");
    try {
      const resp = await fetch("/catalogue/find", { method: "POST", body: form, credentials: "same-origin" });
      if (!resp.ok) {
        setMsg("find-status", await resp.text(), true);
        return;
      }
      const data = await resp.json();
      const reply = document.getElementById("find-reply");
      reply.hidden = false;
      reply.innerHTML = "";
      const p = document.createElement("p");
      p.className = "chat-bot";
      p.textContent = data.reply || "";
      reply.appendChild(p);
      const host = document.getElementById("find-matches");
      host.innerHTML = "";
      (data.matches || []).forEach((item) => host.appendChild(rowButton(item, item.why)));
      setMsg("find-status", (data.matches || []).length ? "" : "No matching prompt you can use.");
    } finally {
      window.setBusy("find-send", false);
    }
  });

  loadList(1);
})();
