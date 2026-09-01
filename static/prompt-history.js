(() => {
  const overlay = document.getElementById("version-overlay");
  const title = document.getElementById("version-title");
  const body = document.getElementById("version-body");
  const close = document.getElementById("version-close");

  function show(on) {
    if (overlay) overlay.classList.toggle("show", on);
  }

  document.querySelectorAll(".view-version").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const name = btn.getAttribute("data-name") || "";
      if (title) title.textContent = name;
      if (body) body.textContent = "Loading…";
      show(true);
      try {
        const resp = await fetch(`/prompts/raw?name=${encodeURIComponent(name)}`, { credentials: "same-origin" });
        if (!resp.ok) {
          if (body) body.textContent = "Failed to load prompt text.";
          return;
        }
        const data = await resp.json();
        if (body) body.textContent = data.content || "(empty)";
      } catch (_) {
        if (body) body.textContent = "Failed to load prompt text.";
      }
    });
  });

  close?.addEventListener("click", () => show(false));
  overlay?.addEventListener("click", (e) => {
    if (e.target === overlay) show(false);
  });
})();
