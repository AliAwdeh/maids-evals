(() => {
  const copyBtn = document.getElementById("copy-token");
  if (copyBtn) {
    copyBtn.addEventListener("click", async () => {
      const targetId = copyBtn.getAttribute("data-copy-target");
      const el = targetId ? document.getElementById(targetId) : null;
      if (!el) return;
      const text = el.textContent || "";
      const done = () => {
        const original = copyBtn.textContent;
        copyBtn.textContent = "Copied";
        setTimeout(() => {
          copyBtn.textContent = original;
        }, 1500);
      };
      try {
        await navigator.clipboard.writeText(text);
        done();
      } catch (e) {
        const range = document.createRange();
        range.selectNodeContents(el);
        const sel = window.getSelection();
        if (sel) {
          sel.removeAllRanges();
          sel.addRange(range);
        }
      }
    });
  }

  document.querySelectorAll("form.js-revoke").forEach((form) => {
    form.addEventListener("submit", (e) => {
      const name = form.getAttribute("data-username") || "this user";
      if (!window.confirm(`Remove ${name}? Their token stops working on the next request.`)) {
        e.preventDefault();
      }
    });
  });

  document.querySelectorAll("form.js-reset").forEach((form) => {
    form.addEventListener("submit", (e) => {
      const input = form.querySelector('input[name="username"]');
      const name = input ? input.value : "this user";
      if (
        !window.confirm(
          `Reset the token for ${name}? Their current token stops working immediately and a new one is shown once.`
        )
      ) {
        e.preventDefault();
      }
    });
  });
})();
