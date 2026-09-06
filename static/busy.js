(() => {
  function asList(target) {
    if (!target) return [];
    const raw = Array.isArray(target) ? target : [target];
    return raw.map((item) => {
      if (typeof item === "string") return document.getElementById(item);
      return item;
    }).filter(Boolean);
  }

  window.setBusy = function setBusy(target, busy, busyLabel) {
    asList(target).forEach((el) => {
      if (busy) {
        if (!el.dataset.label) el.dataset.label = el.textContent;
        el.disabled = true;
        el.classList.add("is-busy");
        el.setAttribute("aria-busy", "true");
        if (busyLabel) el.textContent = busyLabel;
      } else {
        el.disabled = !!el.dataset.keepDisabled;
        el.classList.remove("is-busy");
        el.removeAttribute("aria-busy");
        if (el.dataset.label) el.textContent = el.dataset.label;
      }
    });
  };

  window.withBusy = async function withBusy(target, busyLabel, fn) {
    window.setBusy(target, true, busyLabel);
    try {
      return await fn();
    } finally {
      window.setBusy(target, false);
    }
  };
})();
