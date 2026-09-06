(() => {
  // Shared spreadsheet upload. Picking a file starts it, the response is data
  // rather than a redirect, and the page patches itself -- so nothing anyone
  // has half-typed is thrown away.
  //
  // Both New run and Build a prompt use this; each passes an onLoaded that
  // knows how to update its own page.

  window.autoUpload = function autoUpload(opts) {
    const input = document.getElementById(opts.input);
    const form = opts.form ? document.getElementById(opts.form) : null;
    const status = opts.status ? document.getElementById(opts.status) : null;
    const busyId = opts.busy || null;
    if (!input) return;

    function say(msg, isError) {
      if (!status) return;
      status.textContent = msg || "";
      status.style.color = isError ? "var(--bad)" : "var(--muted)";
    }

    async function send(file) {
      if (!file) return;
      say(`Reading ${file.name}…`);
      if (busyId) window.setBusy(busyId, true, "Reading…");
      const body = new FormData();
      body.append("csv_file", file);
      body.append("next", opts.next || "/");
      body.append("json_response", "1");
      try {
        const resp = await fetch("/upload", {
          method: "POST",
          body,
          credentials: "same-origin",
          headers: { Accept: "application/json" },
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
          say(data.error || "Could not read this file.", true);
          return;
        }
        const bits = [`${data.rows} rows`, `${(data.columns || []).length} columns`];
        if (data.sheet) bits.push(`sheet “${data.sheet}”`);
        say(`${file.name} loaded — ${bits.join(" · ")}.${data.note ? " " + data.note : ""}`);
        if (typeof opts.onLoaded === "function") opts.onLoaded(data);
      } catch (err) {
        say("Upload failed. Check your connection and try again.", true);
      } finally {
        if (busyId) window.setBusy(busyId, false);
      }
    }

    input.addEventListener("change", () => send(input.files && input.files[0]));
    form?.addEventListener("submit", (ev) => {
      ev.preventDefault();
      send(input.files && input.files[0]);
    });

    // Dropping a file on the picker should work the same way.
    const zone = form || input;
    ["dragover", "dragenter"].forEach((name) =>
      zone.addEventListener(name, (ev) => {
        ev.preventDefault();
        zone.classList.add("is-dropping");
      })
    );
    ["dragleave", "drop"].forEach((name) =>
      zone.addEventListener(name, () => zone.classList.remove("is-dropping"))
    );
    zone.addEventListener("drop", (ev) => {
      ev.preventDefault();
      const file = ev.dataTransfer?.files?.[0];
      if (file) send(file);
    });
  };
})();
