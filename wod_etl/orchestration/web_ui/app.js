"use strict";
// wod-etl in a browser: Run and Confirm an accession, review decisions.toml in between.

const $ = (id) => document.getElementById(id);
const ui = { acc: null, mtime: null, dirty: false, timer: null, running: false, status: null };
const ta = $("decisions");

async function api(path, options = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  const type = res.headers.get("content-type") || "";
  const body = type.includes("json") ? await res.json() : await res.text();
  if (!res.ok) {
    const err = new Error((body && body.detail) || body || res.statusText);
    err.status = res.status;
    throw err;
  }
  return body;
}

const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const base = (p) => String(p).split("/").pop();
const fmtSize = (n) => n == null ? null : n < 1024 ? `${n} B` : n < 1048576 ?
  `${(n / 1024).toFixed(0)} KB` : `${(n / 1048576).toFixed(1)} MB`;

function say(el, text, kind = "") {
  $(el).textContent = text || "";
  $(el).className = (el === "outcome" ? "outcome " : "msg ") + kind;
}

// ------------------------------------------------------------------ status
const ICON = { done: "✓", running: "…", failed: "✗", blocked: "⏸", pending: "·" };

function renderStatus(s) {
  ui.status = s;
  $("stages").innerHTML = s.stages.map((st) => {
    const cls = ["stage", st.status || "", st.name === "check" ? "first-confirm" : "",
                 st.name === "report" ? "report" : ""].join(" ");
    return `<li class="${cls}" title="${esc(st.summary || "not run yet")}">` +
           `${esc(st.name)} ${ICON[st.status] || ""}</li>`;
  }).join("");

  const job = s.job;
  if (job && job.running) {
    say("outcome", `${job.command} running…`, "wait");
  } else if (s.busy) {
    say("outcome", `busy: ${s.busy.command} ${s.busy.accession} is running`, "wait");
  } else if (job && job.error) {
    say("outcome", `${job.command} raised ${job.error}`, "bad");
  } else if (s.outcome) {
    const kind = { 0: "ok", 2: "wait", 3: "wait" }[s.last_exit_code] ?? "bad";
    say("outcome", `${s.last_command}: ${s.outcome}`, kind);
  } else {
    say("outcome", s.exists ? "" : "Not run yet: Run it to triage its files.");
  }

  $("problems").innerHTML = (s.problems || []).map((p) =>
    `<li class="${p.blocking ? "" : "note"}"><strong>${esc(p.what)}</strong>` +
    (p.why ? ` — ${esc(p.why)}` : "") +
    (p.how_to_fix ? `<br><span class="how">→ ${esc(p.how_to_fix)}</span>` : "") + "</li>").join("");

  if (job) {
    $("lines").textContent = (job.lines || []).join("\n");
    $("activity-state").textContent = job.running ? "(running)" :
      `(${job.command} finished${job.exit_code != null ? `, exit code ${job.exit_code}` : ""})`;
  }
  setButtons();
}

function setButtons() {
  const busy = !!(ui.status && ui.status.busy);
  $("run").disabled = busy || !$("source").value.trim();
  $("confirm").disabled = busy || !ui.acc || !(ui.status && ui.status.exists);
  $("save").disabled = busy || !ui.acc || !ui.dirty;
  $("reload").disabled = busy || !ui.acc;
  ta.readOnly = busy;
}

async function poll() {
  clearTimeout(ui.timer);
  if (!ui.acc) return;
  let s;
  try {
    s = await api(`/api/status/${ui.acc}`);
  } catch (e) {
    ui.timer = setTimeout(poll, 5000);
    return;
  }
  const running = !!(s.job && s.job.running) || !!s.busy;
  renderStatus(s);
  if (ui.running && !running) {               // a command just finished: show what it did
    await Promise.all([refreshFiles(), refreshOutputs(), loadDecisions(false), loadRecent()]);
  }
  ui.running = running;
  ui.timer = setTimeout(poll, running ? 1200 : 5000);
}

// ------------------------------------------------------------------ input files
const SHORT = {
  check: "Check: decide these", in: "In: written to WOD", review_out: "Out: left out by the review",
  later: "Out: WOD takes these, not written yet", not_wod: "Out: not stored by WOD",
  sidecar: "Out: read to locate casts", docs: "Documentation",
};

async function refreshFiles() {
  const r = await api(`/api/files/${ui.acc}`);
  const list = $("file-list");
  let n = 0;
  if (!r.sections.length) {
    list.innerHTML = `<p class="empty">No files triaged yet: Run it.</p>`;
  } else {
    list.innerHTML = (r.decisions_error ? `<p class="msg bad">${esc(r.decisions_error)}</p>` : "") +
      r.sections.map((sec) => {
        n += sec.files.length;
        return `<div class="group ${sec.section}"><h3>${esc(SHORT[sec.section] || sec.heading)}` +
          ` (${sec.files.length})</h3>` + sec.files.map((f) =>
            `<div class="file" data-key="${esc(f.decided_by || "")}" ` +
            `title="${esc(f.path + (f.why ? "\n\n" + f.why : "") +
                          (f.depth_note ? "\n\nDepth: " + f.depth_note : ""))}">` +
            `<span class="name">${esc(f.name)}</span>` +
            (f.include ? `<span class="badge">${esc(f.dataset || "in")}</span>`
                       : `<span class="badge out">out</span>`) +
            (f.depth != null ? `<span class="badge depth">${esc(f.depth.toFixed(1))} m</span>` : "") +
            (f.data ? `<div class="data">${esc(f.data)}</div>` : "") + "</div>").join("") +
          "</div>";
      }).join("");
  }
  $("inputs-count").textContent = n ? String(n) : "";
}

$("file-list").addEventListener("click", (e) => {
  const el = e.target.closest(".file");
  if (el) jumpTo(el.dataset.key);
});

function tomlKey(k) {
  return /^[A-Za-z0-9_-]+$/.test(k) ? k : JSON.stringify(k);
}

function jumpTo(key) {
  if (!key) {
    say("editor-msg", "This file has no entry yet: Run again to add one.", "wait");
    return;
  }
  let at = ta.value.indexOf(`[files.${tomlKey(key)}]`);
  if (at < 0) at = ta.value.indexOf(`"${key}"]`);
  if (at < 0) {
    say("editor-msg", `No [files."${key}"] entry in the editor.`, "wait");
    return;
  }
  selectLine(ta.value.slice(0, at).split("\n").length);
}

function selectLine(line) {
  const lines = ta.value.split("\n");
  const start = lines.slice(0, line - 1).reduce((n, l) => n + l.length + 1, 0);
  ta.focus();
  ta.setSelectionRange(start, start + (lines[line - 1] || "").length);
  const lh = parseFloat(getComputedStyle(ta).lineHeight) || 18;
  ta.scrollTop = Math.max(0, (line - 1) * lh - ta.clientHeight / 3);
  syncGutter();
}

// ------------------------------------------------------------------ decisions.toml
function renderGutter() {
  const n = ta.value.split("\n").length;
  $("gutter").textContent = Array.from({ length: n }, (_, i) => i + 1).join("\n");
  // The trailing space keeps a last empty line as tall as the textarea draws it.
  const hl = $("highlight");
  hl.innerHTML = highlightToml(ta.value) + "\n ";
  // The textarea's scrollbars take room the <pre> has not got: give the <pre> the same box,
  // or the colours drift a scrollbar's width from the text at the far right.
  hl.style.right = `${ta.offsetWidth - ta.clientWidth}px`;
  hl.style.bottom = `${ta.offsetHeight - ta.clientHeight}px`;
  syncGutter();
}
window.addEventListener("resize", () => renderGutter());
function syncGutter() {
  $("gutter").scrollTop = ta.scrollTop;
  $("highlight").scrollTop = ta.scrollTop;
  $("highlight").scrollLeft = ta.scrollLeft;
}

// TOML, a line at a time: comments, [tables], keys, strings (a multi-line one carries over to
// the next line), numbers, dates and booleans -- `include = true` and `false` stand out.
const span = (cls, text) => `<span class="t-${cls}">${esc(text)}</span>`;
const KEY = String.raw`(?:[A-Za-z0-9_-]+|"(?:[^"\\]|\\.)*"|'[^']*')`;
const KEY_LINE = new RegExp(String.raw`^(\s*)(${KEY}(?:\s*\.\s*${KEY})*)(\s*=)(.*)$`);
const INLINE_KEY = new RegExp(String.raw`^${KEY}(?:\s*\.\s*${KEY})*(?=\s*=)`);
const TABLE = new RegExp(String.raw`^(\s*)(\[\[?(?:[^\]"']|"(?:[^"\\]|\\.)*"|'[^']*')*\]\]?)(.*)$`);
const TRIPLE = ['"""', "'''"];

// Where a multi-line string that opened before `from` closes: the index just past its
// delimiter, or -1. In a basic (""") one a delimiter after an odd run of backslashes is
// escaped; a literal (''') one has no escapes.
function closing(text, quote, from) {
  for (let i = text.indexOf(quote, from); i >= 0; i = text.indexOf(quote, i + 1)) {
    let slashes = 0;
    for (let j = i - 1; j >= 0 && text[j] === "\\"; j--) slashes++;
    if (quote === "'''" || slashes % 2 === 0) return i + 3;
  }
  return -1;
}

function highlightToml(text) {
  // A multi-line string still open, and how deep inside a multi-line array a line is.
  const state = { open: null, depth: 0 };
  return text.split("\n").map((line) => highlightLine(line, state)).join("\n");
}

function highlightLine(line, state) {
  if (state.open) {
    const end = closing(line, state.open, 0);
    if (end < 0) return span("string", line);
    state.open = null;
    return span("string", line.slice(0, end)) + highlightValue(line.slice(end), state);
  }
  const trimmed = line.trimStart();
  if (trimmed.startsWith("#")) {
    return span(/^# ?====/.test(trimmed) ? "comment t-heading" : "comment", line);
  }
  if (state.depth > 0) return highlightValue(line, state);   // inside an array: values
  const table = TABLE.exec(line);
  if (table) return esc(table[1]) + span("table", table[2]) + highlightValue(table[3], state);
  const kv = KEY_LINE.exec(line);
  if (kv) {
    return esc(kv[1]) + span("key", kv[2]) + span("punct", kv[3]) + highlightValue(kv[4], state);
  }
  return highlightValue(line, state);
}

function highlightValue(s, state) {
  let out = "";
  let i = 0;
  while (i < s.length) {
    const rest = s.slice(i);
    const c = s[i];
    const triple = TRIPLE.find((q) => rest.startsWith(q));
    let m;
    if (c === "#") {
      return out + span("comment", rest);
    } else if (triple) {
      const end = closing(rest, triple, 3);
      if (end < 0) {
        state.open = triple;
        return out + span("string", rest);
      }
      out += span("string", rest.slice(0, end));
      i += end;
    } else if (c === '"') {
      let j = 1;
      while (j < rest.length && rest[j] !== '"') j += rest[j] === "\\" ? 2 : 1;
      out += span("string", rest.slice(0, j + 1));
      i += j + 1;
    } else if (c === "'") {
      const end = rest.indexOf("'", 1);
      const n = end < 0 ? rest.length : end + 1;
      out += span("string", rest.slice(0, n));
      i += n;
    } else if ((m = /^(true|false)\b/.exec(rest))) {
      out += span(m[1], m[1]);
      i += m[1].length;
    } else if ((m = /^\d{4}-\d{2}-\d{2}(?:[Tt ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?)?/.exec(rest))) {
      out += span("date", m[0]);
      i += m[0].length;
    } else if ((m = /^[+-]?(?:0x[0-9A-Fa-f_]+|0o[0-7_]+|0b[01_]+|inf\b|nan\b|\d[\d_]*(?:\.[\d_]+)?(?:[eE][+-]?\d+)?)/.exec(rest))) {
      out += span("number", m[0]);
      i += m[0].length;
    } else if ((m = INLINE_KEY.exec(rest))) {
      out += span("key", m[0]);                 // a key inside an inline table
      i += m[0].length;
    } else if ("[]{},=".includes(c)) {
      if (c === "[") state.depth += 1;
      if (c === "]") state.depth = Math.max(0, state.depth - 1);
      out += span("punct", c);
      i += 1;
    } else {
      m = /^[^#"'[\]{},=\s\d+-]+|^\s+|^./.exec(rest);
      out += esc(m[0]);
      i += m[0].length;
    }
  }
  return out;
}

function setDirty(d) {
  ui.dirty = d;
  $("dirty").hidden = !d;
  setButtons();
}

async function loadDecisions(force) {
  if (!ui.acc) return;
  const d = await api(`/api/decisions/${ui.acc}`);
  if (ui.dirty && !force) {
    if (d.mtime !== ui.mtime) {
      say("editor-msg", "decisions.toml changed on disk: Save keeps your version, Reload takes " +
                        "the new one.", "wait");
    }
    return;
  }
  ta.value = d.text;
  ui.mtime = d.mtime;
  ta.disabled = !d.exists;
  setDirty(false);
  renderGutter();
  say("editor-msg", d.exists ? "" : "No decisions.toml yet: Run first.");
}

async function save() {
  if (!ui.acc || !ui.dirty) return true;
  try {
    const r = await api(`/api/decisions/${ui.acc}`, {
      method: "PUT", body: JSON.stringify({ text: ta.value, mtime: ui.mtime }) });
    ui.mtime = r.mtime;
    setDirty(false);
    say("editor-msg", "Saved.", "ok");
    refreshFiles();
    return true;
  } catch (e) {
    say("editor-msg", e.message, "bad");
    const m = /line (\d+)/.exec(e.message);
    if (m) selectLine(Number(m[1]));
    return false;
  }
}

ta.addEventListener("input", () => { setDirty(true); renderGutter(); });
ta.addEventListener("scroll", syncGutter);
ta.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {
    e.preventDefault();
    save();
  } else if (e.key === "Tab" && !e.shiftKey && !ta.readOnly) {
    e.preventDefault();
    ta.setRangeText("    ", ta.selectionStart, ta.selectionEnd, "end");
    setDirty(true);
    renderGutter();
  }
});
$("save").onclick = save;
$("reload").onclick = () => {
  if (!ui.dirty || window.confirm("Discard your unsaved edits to decisions.toml?")) {
    loadDecisions(true);
  }
};

// ------------------------------------------------------------------ output files
async function refreshOutputs() {
  const r = await api(`/api/outputs/${ui.acc}`);
  const list = $("output-list");
  $("outputs-count").textContent = r.files.length ? String(r.files.length) : "";
  if (!r.files.length) {
    list.innerHTML = `<p class="empty">Confirm writes the WOD files here.</p>`;
    return;
  }
  const verified = r.verified === true ?
    `<div class="verified ok">Verified: every value is within WOD's ranges</div>` :
    r.verified === false ? `<div class="verified bad">NOT verified: see the problems above</div>` : "";
  list.innerHTML = verified + r.files.map((f) => {
    const meta = [f.dataset, f.year, f.casts != null ? `${f.casts} casts` : null,
                  fmtSize(f.size)].filter(Boolean).join(" · ");
    const from = f.sources.length ?
      `<div class="meta" title="${esc(f.sources.join("\n"))}">from ${esc(f.sources.slice(0, 3)
        .map(base).join(", "))}${f.sources.length > 3 ? ` and ${f.sources.length - 3} more` : ""}</div>` : "";
    return `<div class="out"><a href="/api/outputs/${encodeURIComponent(ui.acc)}/` +
      `${encodeURIComponent(f.name)}" download>${esc(f.name)}</a>` +
      `<div class="meta">${esc(meta)}</div>${from}</div>`;
  }).join("") + `<p class="meta">In ${esc(r.folder)}` +
    (r.written_at ? `, written ${esc(r.written_at)}` : "") + "</p>";
}

// ------------------------------------------------------------------ commands
async function open(acc, keepSource = false) {
  if (ui.dirty && acc !== ui.acc && !window.confirm("Discard your unsaved edits to decisions.toml?")) {
    return;
  }
  ui.acc = acc;
  ui.dirty = false;
  history.replaceState(null, "", `#${acc}`);
  $("report-link").href = `/api/report/${encodeURIComponent(acc)}`;
  $("log-link").href = `/api/log/${encodeURIComponent(acc)}`;
  const s = await api(`/api/status/${acc}`);
  if (!keepSource) $("source").value = s.input || acc;
  renderStatus(s);
  ui.running = !!(s.job && s.job.running) || !!s.busy;
  await Promise.all([refreshFiles(), refreshOutputs(), loadDecisions(true)]);
  poll();
}

$("run").onclick = async () => {
  const source = $("source").value.trim();
  if (!source || !(await save())) return;
  try {
    const r = await api("/api/run", { method: "POST", body: JSON.stringify({ source }) });
    ui.running = true;
    await open(r.accession, true);
    $("activity").open = true;
  } catch (e) {
    say("outcome", e.message, "bad");
  }
};

$("confirm").onclick = async () => {
  if (!ui.acc || !(await save())) return;
  try {
    await api(`/api/confirm/${encodeURIComponent(ui.acc)}`, { method: "POST" });
    ui.running = true;
    $("activity").open = true;
    poll();
  } catch (e) {
    say("outcome", e.message, "bad");
  }
};

$("source").addEventListener("input", setButtons);
$("source").addEventListener("keydown", (e) => { if (e.key === "Enter") $("run").click(); });

async function loadRecent() {
  const list = await api("/api/accessions");
  $("recent").innerHTML = `<option value="">Open…</option>` + list.map((a) =>
    `<option value="${esc(a.accession)}">${esc(a.accession)}` +
    `${a.last_command ? ` (${esc(a.last_command)}, exit ${esc(a.last_exit_code)})` : ""}</option>`).join("");
}
$("recent").onchange = (e) => {
  if (e.target.value) open(e.target.value);
  e.target.value = "";
};

loadRecent();
if (location.hash.length > 1) open(decodeURIComponent(location.hash.slice(1)));
setButtons();
