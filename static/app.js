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

/* ==========================================================================
   Answer-value filters

   The second dropdown used to offer only True/False. The batch's own answers
   are known, so offer those -- with counts, so you can see how big a slice you
   are about to take before you take it.
   ========================================================================== */
(() => {
  const OPTIONS = window.MAIDS_KEY_VALUES || {};

  function fillValues(row) {
    const keySel = row.querySelector(".filter-key");
    const valSel = row.querySelector(".filter-val");
    if (!keySel || !valSel) return;
    const wanted = valSel.dataset.selected || valSel.value || "";
    const values = OPTIONS[keySel.value] || [];
    valSel.innerHTML = "";
    const any = document.createElement("option");
    any.value = "";
    any.textContent = values.length ? "Any value" : "Pick a field first";
    valSel.appendChild(any);
    values.forEach((item) => {
      const opt = document.createElement("option");
      opt.value = item.label;
      opt.textContent = `${item.label} · ${item.count}`;
      if (item.label === wanted) opt.selected = true;
      valSel.appendChild(opt);
    });
    valSel.disabled = !values.length;
  }

  document.querySelectorAll(".filter-row").forEach((row) => {
    fillValues(row);
    row.querySelector(".filter-key")?.addEventListener("change", () => {
      // A value from the previous field is meaningless against a new one.
      const valSel = row.querySelector(".filter-val");
      if (valSel) valSel.dataset.selected = "";
      fillValues(row);
    });
  });
})();
