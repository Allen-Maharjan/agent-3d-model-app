"use strict";

/* Code editor support for the workspace: a line diff and a small wrapper
   around the vendored CodeMirror 5, with a plain <textarea> fallback when
   CodeMirror failed to load. Loaded before app.js; defines globals only. */

const DIFF_MAX_LINES = 1500; // the LCS table is (n+1)*(m+1) ints, ~9 MB at this size

/* Lines of `b` that are not in the longest common subsequence with `a`.
   Returns { added: Set<lineIndexInB>, removed: number, exact: boolean };
   `exact` is false when the inputs were too large to diff. */
function lineDiff(aText, bText) {
  const a = aText ? aText.split("\n") : [];
  const b = bText ? bText.split("\n") : [];
  if (a.length > DIFF_MAX_LINES || b.length > DIFF_MAX_LINES) {
    return { added: new Set(b.map((_, i) => i)), removed: 0, exact: false };
  }
  const n = a.length;
  const m = b.length;
  // Suffix LCS lengths, one flat row-major table.
  const w = m + 1;
  const table = new Int32Array((n + 1) * w);
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      table[i * w + j] = a[i] === b[j] ? table[(i + 1) * w + j + 1] + 1 : Math.max(table[(i + 1) * w + j], table[i * w + j + 1]);
    }
  }
  const added = new Set();
  let removed = 0;
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { i++; j++; }
    else if (table[(i + 1) * w + j] >= table[i * w + j + 1]) { removed++; i++; }
    else { added.add(j); j++; }
  }
  removed += n - i;
  for (; j < m; j++) added.add(j);
  return { added, removed, exact: true };
}

/* Editor adapter. Options: value, onChange(text), onRun(), onEscape(), label. */
function createCodeEditor(host, opts) {
  const reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (!window.CodeMirror) {
    const ta = document.createElement("textarea");
    ta.className = "textarea code-fallback";
    ta.spellcheck = false;
    ta.value = opts.value || "";
    ta.setAttribute("aria-label", opts.label || "Edit script");
    ta.addEventListener("input", () => opts.onChange?.(ta.value));
    ta.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); opts.onRun?.(); }
    });
    host.append(ta);
    return {
      getValue: () => ta.value,
      setValue: (v) => { ta.value = v; },
      markLines() {},
      markError() {},
      clearError() {},
      refresh() {},
      focus: () => ta.focus(),
      lineCount: () => ta.value.split("\n").length,
    };
  }

  const cm = window.CodeMirror(host, {
    value: opts.value || "",
    mode: "python",
    lineNumbers: true,
    indentUnit: 4,
    tabSize: 4,
    indentWithTabs: false,
    gutters: ["cm-change-gutter", "CodeMirror-linenumbers"],
    screenReaderLabel: opts.label || "Edit script",
    extraKeys: {
      Tab: (ed) => (ed.somethingSelected() ? ed.execCommand("indentMore") : ed.replaceSelection("    ")),
      "Shift-Tab": (ed) => ed.execCommand("indentLess"),
      "Ctrl-Enter": () => opts.onRun?.(),
      "Cmd-Enter": () => opts.onRun?.(),
      // Tab indents inside the editor, so Escape is the way out for keyboard users.
      Esc: () => opts.onEscape?.(),
    },
  });
  let silent = false;
  cm.on("change", () => { if (!silent) opts.onChange?.(cm.getValue()); });

  const marked = [];
  let errorLine = null;
  let carveTimer = null;

  function clearMarks() {
    for (const line of marked) {
      cm.removeLineClass(line, "wrap");
      cm.setGutterMarker(line, "cm-change-gutter", null);
    }
    marked.length = 0;
  }

  return {
    getValue: () => cm.getValue(),
    setValue(v) {
      silent = true;
      cm.setValue(v);
      cm.clearHistory();
      silent = false;
    },
    /* Mark lines: kind "added" (from the parent revision) or "edited" (unsaved).
       `animate` plays the carve: a staggered sweep across each changed line and
       a scan line down the editor. */
    markLines(lines, kind, animate) {
      clearTimeout(carveTimer);
      clearMarks();
      const carve = animate && !reduceMotion;
      let order = 0;
      for (const n of [...lines].sort((x, y) => x - y)) {
        if (n >= cm.lineCount()) continue;
        const handle = cm.getLineHandle(n);
        const cls = [`cm-line-${kind}`];
        if (carve) cls.push("cm-carve", `cm-carve-d${Math.min(order++, 23)}`);
        cm.addLineClass(handle, "wrap", cls.join(" "));
        const mark = document.createElement("div");
        mark.className = `cm-change-mark cm-change-${kind}${carve ? " is-carving" : ""}`;
        cm.setGutterMarker(handle, "cm-change-gutter", mark);
        marked.push(handle);
      }
      if (carve) {
        const wrapper = cm.getWrapperElement();
        wrapper.classList.remove("is-scanning");
        void wrapper.offsetWidth; // restart the scan animation
        wrapper.classList.add("is-scanning");
        carveTimer = setTimeout(() => {
          wrapper.classList.remove("is-scanning");
          for (const handle of marked) {
            cm.removeLineClass(handle, "wrap", "cm-carve");
            for (let d = 0; d < 24; d++) cm.removeLineClass(handle, "wrap", `cm-carve-d${d}`);
          }
          for (const el of cm.getWrapperElement().querySelectorAll(".cm-change-mark.is-carving")) el.classList.remove("is-carving");
        }, 1800);
      }
      if (lines.size && animate) {
        const first = Math.min(...lines);
        cm.scrollIntoView({ line: first, ch: 0 }, 80);
      }
    },
    markError(line) {
      this.clearError();
      if (!line || line < 1 || line > cm.lineCount()) return;
      errorLine = cm.getLineHandle(line - 1);
      cm.addLineClass(errorLine, "wrap", "cm-line-error");
      cm.addLineClass(errorLine, "gutter", "cm-gutter-error");
      cm.scrollIntoView({ line: line - 1, ch: 0 }, 80);
    },
    clearError() {
      if (errorLine) {
        cm.removeLineClass(errorLine, "wrap", "cm-line-error");
        cm.removeLineClass(errorLine, "gutter", "cm-gutter-error");
        errorLine = null;
      }
    },
    refresh: () => cm.refresh(),
    focus: () => cm.focus(),
    lineCount: () => cm.lineCount(),
  };
}
