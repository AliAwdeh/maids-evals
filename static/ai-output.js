(() => {
  // Shared, user-friendly renderer for raw LLM output.
  // Used identically on Results, Review, and Test so answers look the same
  // everywhere: JSON -> collapsible tree with a Pretty/Raw toggle + valid badge,
  // plain text -> clamped block with an expand control, plus a copy button.

  const CLAMP_PX = 340;

  function parseMaybeJson(text) {
    const raw = (text || "").trim();
    if (!raw) return { ok: false, empty: true, value: null };
    let t = raw;
    if (t.startsWith("```")) {
      t = t.replace(/^```[a-zA-Z]*\s*/, "").replace(/```$/, "").trim();
    }
    try {
      return { ok: true, empty: false, value: JSON.parse(t) };
    } catch (_) {
      const first = Math.min(
        ...["{", "["].map((c) => (t.indexOf(c) === -1 ? Infinity : t.indexOf(c)))
      );
      const last = Math.max(t.lastIndexOf("}"), t.lastIndexOf("]"));
      if (first !== Infinity && last > first) {
        try {
          return { ok: true, empty: false, value: JSON.parse(t.slice(first, last + 1)) };
        } catch (_) {}
      }
      return { ok: false, empty: false, value: null };
    }
  }

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function scalarSpan(value) {
    let cls = "jt-val";
    let text;
    if (value === null) {
      cls += " jt-null";
      text = "null";
    } else if (typeof value === "boolean") {
      cls += value ? " jt-true" : " jt-false";
      text = value ? "true" : "false";
    } else if (typeof value === "number") {
      cls += " jt-num";
      text = String(value);
    } else {
      cls += " jt-str";
      text = String(value);
      if (text === "") text = '""';
    }
    return el("span", cls, text);
  }

  function preferredPretty() {
    try {
      return localStorage.getItem("maids-ai-pretty") !== "0";
    } catch (_) {
      return true;
    }
  }

  function rememberPretty(pretty) {
    try {
      localStorage.setItem("maids-ai-pretty", pretty ? "1" : "0");
    } catch (_) {}
    document.documentElement.dataset.aiPretty = pretty ? "1" : "0";
  }

  function isContainer(v) {
    return v && typeof v === "object";
  }

  function previewCount(value) {
    if (Array.isArray(value)) return `${value.length} item${value.length === 1 ? "" : "s"}`;
    return `${Object.keys(value).length} field${Object.keys(value).length === 1 ? "" : "s"}`;
  }

  function renderNode(value) {
    if (!isContainer(value)) {
      const row = el("div", "jt-leaf");
      row.appendChild(scalarSpan(value));
      return row;
    }
    const entries = Array.isArray(value)
      ? value.map((v, i) => [String(i), v])
      : Object.entries(value);
    const wrap = el("div", "jt-obj");
    if (!entries.length) {
      wrap.appendChild(el("div", "jt-empty", Array.isArray(value) ? "[ ]" : "{ }"));
      return wrap;
    }
    entries.forEach(([key, val]) => {
      if (isContainer(val) && (Array.isArray(val) ? val.length : Object.keys(val).length)) {
        const details = el("details", "jt-node");
        const summary = el("summary");
        summary.appendChild(el("span", "jt-key", key));
        summary.appendChild(el("span", "jt-sep", ": "));
        summary.appendChild(el("span", "jt-preview", previewCount(val)));
        details.appendChild(summary);
        details.appendChild(renderNode(val));
        wrap.appendChild(details);
      } else {
        const row = el("div", "jt-row");
        row.appendChild(el("span", "jt-key", key));
        row.appendChild(el("span", "jt-sep", ": "));
        row.appendChild(isContainer(val) ? el("span", "jt-val jt-empty", Array.isArray(val) ? "[ ]" : "{ }") : scalarSpan(val));
        wrap.appendChild(row);
      }
    });
    return wrap;
  }

  function copyText(btn, raw) {
    const done = () => {
      const old = btn.textContent;
      btn.textContent = "Copied";
      setTimeout(() => (btn.textContent = old), 1200);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(raw).then(done).catch(() => fallback());
    } else {
      fallback();
    }
    function fallback() {
      const ta = document.createElement("textarea");
      ta.value = raw;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand("copy");
        done();
      } catch (_) {}
      document.body.removeChild(ta);
    }
  }

  function buildViewer(raw, parsed, mode) {
    const viewer = el("div", "ai-output__viewer");
    const bar = el("div", "ai-output__bar");
    const sharedToggle = mode === "list";

    let badgeCls = "ai-output__badge";
    let badgeText;
    if (parsed.empty) {
      badgeCls += " is-empty";
      badgeText = "No output";
    } else if (parsed.ok) {
      badgeCls += " is-json";
      badgeText = "Valid JSON";
    } else {
      badgeCls += " is-text";
      badgeText = "Text";
    }
    bar.appendChild(el("span", badgeCls, badgeText));

    const body = el("div", "ai-output__body");
    let prettyView = null;
    const rawView = el("div", "ai-output__view ai-output__view--raw");
    const pre = el("pre", "ai-output__pre");
    pre.textContent = raw || "(empty)";
    rawView.appendChild(pre);

    const setView = (pretty) => {
      if (prettyView) {
        prettyView.hidden = !pretty;
        rawView.hidden = pretty;
      }
      viewer.querySelectorAll(".ai-output__seg button").forEach((btn) => {
        btn.classList.toggle("is-active", btn.dataset.view === (pretty ? "pretty" : "raw"));
      });
    };
    viewer._setPretty = setView;

    if (parsed.ok) {
      prettyView = el("div", "ai-output__view ai-output__view--pretty");
      prettyView.appendChild(renderNode(parsed.value));
      body.appendChild(prettyView);
      body.appendChild(rawView);
      setView(preferredPretty());

      if (!sharedToggle) {
        const seg = el("div", "ai-output__seg");
        const bPretty = el("button", preferredPretty() ? "is-active" : "", "Pretty");
        const bRaw = el("button", preferredPretty() ? "" : "is-active", "Raw");
        bPretty.type = "button";
        bRaw.type = "button";
        bPretty.dataset.view = "pretty";
        bRaw.dataset.view = "raw";
        bPretty.addEventListener("click", () => {
          rememberPretty(true);
          setView(true);
        });
        bRaw.addEventListener("click", () => {
          rememberPretty(false);
          setView(false);
        });
        seg.appendChild(bPretty);
        seg.appendChild(bRaw);
        bar.appendChild(seg);
      }
    } else {
      body.appendChild(rawView);
    }

    const spacer = el("span", "ai-output__spacer");
    bar.appendChild(spacer);

    if (!parsed.empty) {
      const copy = el("button", "ai-output__copy", "Copy");
      copy.type = "button";
      copy.addEventListener("click", () => copyText(copy, raw));
      bar.appendChild(copy);
    }

    viewer.appendChild(bar);
    viewer.appendChild(body);

    if (mode === "inline") {
      requestAnimationFrame(() => {
        if (body.scrollHeight > CLAMP_PX + 40) {
          body.classList.add("is-clamped");
          const more = el("button", "ai-output__more", "Show full output");
          more.type = "button";
          more.addEventListener("click", () => {
            const clamped = body.classList.toggle("is-clamped");
            more.textContent = clamped ? "Show full output" : "Show less";
          });
          viewer.appendChild(more);
        }
      });
    }
    return viewer;
  }

  function initOne(container) {
    if (container.dataset.aiInit === "1") return;
    container.dataset.aiInit = "1";
    const rawNode = container.querySelector(".ai-output__raw");
    const labelNode = container.querySelector(".ai-output__label");
    const raw = rawNode ? rawNode.textContent : "";
    const label = labelNode ? labelNode.textContent : "";
    const mode = container.dataset.mode || "inline";

    if (mode === "list") {
      container.appendChild(buildViewer(raw, parseMaybeJson(raw), "list"));
    } else if (mode === "disclosure") {
      const parsedForBadge = parseMaybeJson(raw);
      const details = el("details", "ai-output__disclosure");
      const summary = el("summary");
      summary.appendChild(el("span", "ai-output__summary-label", label || "Model answer"));
      let badgeCls = "ai-output__badge";
      let badgeText;
      if (parsedForBadge.empty) {
        badgeCls += " is-empty";
        badgeText = "No output";
      } else if (parsedForBadge.ok) {
        badgeCls += " is-json";
        badgeText = "JSON";
      } else {
        badgeCls += " is-text";
        badgeText = "Text";
      }
      summary.appendChild(el("span", badgeCls, badgeText));
      details.appendChild(summary);
      const slot = el("div", "ai-output__slot");
      details.appendChild(slot);
      details.addEventListener("toggle", () => {
        if (details.open && !slot.dataset.built) {
          slot.dataset.built = "1";
          slot.appendChild(buildViewer(raw, parseMaybeJson(raw), "disclosure"));
        }
      });
      container.appendChild(details);
    } else {
      container.appendChild(buildViewer(raw, parseMaybeJson(raw), "inline"));
    }
  }

  function initAll(root) {
    (root || document).querySelectorAll(".ai-output").forEach(initOne);
  }

  window.initAiOutputs = initAll;
  window.setAiOutputPretty = function setAiOutputPretty(pretty) {
    rememberPretty(pretty);
    document.querySelectorAll(".ai-output__viewer").forEach((viewer) => {
      if (typeof viewer._setPretty === "function") viewer._setPretty(pretty);
    });
    document.querySelectorAll("#answers-view [data-pretty]").forEach((btn) => {
      btn.classList.toggle("is-active", (btn.getAttribute("data-pretty") === "1") === pretty);
    });
  };

  function bindAnswersToggle() {
    const bar = document.getElementById("answers-view");
    if (!bar || bar.dataset.bound === "1") return;
    bar.dataset.bound = "1";
    const pretty = preferredPretty();
    bar.querySelectorAll("[data-pretty]").forEach((btn) => {
      btn.classList.toggle("is-active", (btn.getAttribute("data-pretty") === "1") === pretty);
    });
    bar.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-pretty]");
      if (!btn) return;
      window.setAiOutputPretty(btn.getAttribute("data-pretty") === "1");
    });
  }

  function boot() {
    initAll();
    bindAnswersToggle();
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
