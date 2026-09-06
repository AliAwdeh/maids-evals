(() => {
  const form = document.getElementById("context-form");
  const box = document.getElementById("context-text");
  const status = document.getElementById("context-status");
  const saveBtn = document.getElementById("context-save");
  const copyBtn = document.getElementById("context-copy");

  function setStatus(text, isError) {
    if (!status) return;
    status.textContent = text || "";
    status.classList.toggle("error", Boolean(isError));
  }

  form?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = new FormData();
    body.append("content", box ? box.value : "");
    if (saveBtn) saveBtn.disabled = true;
    setStatus("Saving…");
    try {
      const resp = await fetch("/context/save", {
        method: "POST",
        body,
        credentials: "same-origin",
      });
      if (!resp.ok) {
        setStatus(await resp.text(), true);
        return;
      }
      const data = await resp.json();
      setStatus(data.message || "Saved.");
    } catch (err) {
      setStatus("Could not save company info.", true);
    } finally {
      if (saveBtn) saveBtn.disabled = false;
    }
  });

  copyBtn?.addEventListener("click", async () => {
    const text = box ? box.value : "";
    const original = copyBtn.textContent;
    const done = () => {
      copyBtn.textContent = "Copied";
      setTimeout(() => {
        copyBtn.textContent = original;
      }, 1500);
    };
    try {
      await navigator.clipboard.writeText(text);
      done();
    } catch (err) {
      if (box) {
        box.focus();
        box.select();
      }
    }
  });
})();
