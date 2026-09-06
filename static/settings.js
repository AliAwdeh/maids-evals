(() => {
  function setMsg(id, msg, isError) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = msg || "";
    el.style.color = isError ? "#8d2c2c" : "";
  }

  function collectTools() {
    const tools = {};
    document.querySelectorAll("[data-tool][data-field]").forEach((el) => {
      const tool = el.dataset.tool;
      const field = el.dataset.field;
      if (!tools[tool]) tools[tool] = {};
      tools[tool][field] = el.value || "";
    });
    return tools;
  }

  document.getElementById("set-save")?.addEventListener("click", async () => {
    setMsg("set-status", "Saving…");
    const resp = await fetch("/settings/save", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({
        default_provider: document.getElementById("set-provider")?.value || "langcc",
        default_model: document.getElementById("set-model")?.value || "",
        default_key_id: document.getElementById("set-key")?.value || "",
      }),
    });
    if (!resp.ok) return setMsg("set-status", await resp.text(), true);
    setMsg("set-status", "Defaults saved.");
  });

  document.getElementById("tools-save")?.addEventListener("click", async () => {
    setMsg("tools-status", "Saving…");
    const resp = await fetch("/settings/save", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ tools: collectTools() }),
    });
    if (!resp.ok) return setMsg("tools-status", await resp.text(), true);
    setMsg("tools-status", "Helper models saved.");
  });

  document.getElementById("key-form")?.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const form = new FormData();
    form.append("label", document.getElementById("key-label")?.value || "");
    form.append("provider", document.getElementById("key-provider")?.value || "langcc");
    form.append("api_key", document.getElementById("key-secret")?.value || "");
    setMsg("key-status", "Saving key…");
    const resp = await fetch("/settings/keys", { method: "POST", body: form, credentials: "same-origin" });
    if (!resp.ok) return setMsg("key-status", await resp.text(), true);
    window.location.reload();
  });

  document.querySelectorAll(".key-forget").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const form = new FormData();
      form.append("key_id", btn.dataset.id || "");
      const resp = await fetch("/settings/keys/forget", { method: "POST", body: form, credentials: "same-origin" });
      if (!resp.ok) return setMsg("key-status", await resp.text(), true);
      window.location.reload();
    });
  });
})();
