(() => {
  function toggleGroup(name, action) {
    document.querySelectorAll(`input[type="checkbox"][name="${name}"]`).forEach((box) => {
      box.checked = action === "all";
    });
  }
  document.querySelectorAll(".toggle-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const target = btn.getAttribute("data-target");
      const action = btn.getAttribute("data-action");
      if (target && action) toggleGroup(target, action);
    });
  });

  const statsBtn = document.getElementById("stats-button");
  const statsOverlay = document.getElementById("stats-overlay");
  const statsCancel = document.getElementById("stats-cancel");
  function show(el, on) {
    if (!el) return;
    el.classList.toggle("show", on);
  }
  if (statsBtn) statsBtn.addEventListener("click", (e) => { e.preventDefault(); show(statsOverlay, true); });
  if (statsCancel) statsCancel.addEventListener("click", () => show(statsOverlay, false));
})();
