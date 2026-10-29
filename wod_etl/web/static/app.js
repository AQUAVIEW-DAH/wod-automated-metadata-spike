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
            (f.taxa ? `<span class="badge taxa">Taxa-Records</span>` : "") +
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
  // Match the textarea's scrollbar space so the highlight stays aligned with the text.
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

// TOML highlighting, a line at a time; a multi-line string carries over to the next line.
const span = (cls, text) => `<span class="t-${cls}">${esc(text)}</span>`;
const KEY = String.raw`(?:[A-Za-z0-9_-]+|"(?:[^"\\]|\\.)*"|'[^']*')`;
const KEY_LINE = new RegExp(String.raw`^(\s*)(${KEY}(?:\s*\.\s*${KEY})*)(\s*=)(.*)$`);
const INLINE_KEY = new RegExp(String.raw`^${KEY}(?:\s*\.\s*${KEY})*(?=\s*=)`);
const TABLE = new RegExp(String.raw`^(\s*)(\[\[?(?:[^\]"']|"(?:[^"\\]|\\.)*"|'[^']*')*\]\]?)(.*)$`);
const TRIPLE = ['"""', "'''"];

// Index just past the delimiter closing a multi-line string, or -1. Basic (""") strings
// honour backslash escapes; literal (''') ones do not.
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
  if (pv.name) {                              // reread a previewed file a command rewrote
    const f = r.files.find((x) => x.name === pv.name);
    if (f) openPreview(f.name, f.kind, pv.index ?? 0);
    else closePreview();
  }
  if (!r.files.length) {
    list.innerHTML = `<p class="empty">Confirm writes the WOD files here.</p>`;
    return;
  }
  const checked = [r.files.some((f) => f.kind !== "taxa") ? "every value is within WOD's ranges" : null,
                   r.files.some((f) => f.kind === "taxa") ? "every Taxa-Record is complete" : null]
    .filter(Boolean).join(", ");
  const verified = r.verified === true ? `<div class="verified ok">Verified: ${checked}</div>` :
    r.verified === false ? `<div class="verified bad">NOT verified: see the problems above</div>` : "";
  list.innerHTML = verified + r.files.map((f) => {
    const meta = (f.kind === "taxa" ?
      ["OSD Taxa-Records", f.year, f.hauls != null ? count(f.hauls, "haul") : null,
       f.records != null ? count(f.records, "record") : null, fmtSize(f.size)] :
      [f.dataset, f.year, f.casts != null ? count(f.casts, "cast") : null, fmtSize(f.size)])
      .filter(Boolean).join(" · ");
    const resolution = f.kind === "taxa" && f.records != null ?
      `<div class="meta">taxa: ${f.resolved} resolved · ${f.higher_rank} one rank up · ` +
      `${f.by_name} by name only</div>` : "";
    const from = f.sources.length ?
      `<div class="meta" title="${esc(f.sources.join("\n"))}">from ${esc(f.sources.slice(0, 3)
        .map(base).join(", "))}${f.sources.length > 3 ? ` and ${f.sources.length - 3} more` : ""}</div>` : "";
    return `<div class="out"><a class="dl" href="${esc(outputUrl(f.name))}" download>download</a>` +
      `<a class="preview" href="#" data-name="${esc(f.name)}" data-kind="${esc(f.kind)}" ` +
      `title="Preview">${esc(f.name)}</a>` +
      `<div class="meta">${esc(meta)}</div>${resolution}${from}</div>`;
  }).join("") +
    (r.taxa_list_unavailable ? `<p class="meta warn">Taxa list unavailable: names kept as ` +
      `written. To resolve them, run online: uv run wod-etl confirm ${esc(ui.acc)} ` +
      `--from process</p>` : "") +
    (r.taxa_not_stated ? `<p class="meta warn" title="${esc(r.taxa_not_stated.join("\n"))}">` +
      `${esc(r.taxa_not_stated.length)} Biological Header field(s) not stated: ask the ` +
      `submitter</p>` : "");
}

// ------------------------------------------------------------------ preview
const PAGE = 500;                    // level rows added to the table at a time
const pv = { name: null, kind: null, file: null, index: null, cast: null, token: 0,
             x: null, y: null, fieldsOpen: true };
const outputUrl = (name) => `/api/outputs/${encodeURIComponent(ui.acc)}/${encodeURIComponent(name)}`;
const previewUrl = (name) => `/api/preview/${encodeURIComponent(ui.acc)}/${encodeURIComponent(name)}`;
const words = () => pv.kind === "taxa" ? { one: "haul", many: "hauls", row: "record" }
                                        : { one: "cast", many: "casts", row: "level" };
const count = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const coord = (v, pos, neg) => v == null ? "?" : `${Math.abs(v).toFixed(4)}°${v < 0 ? neg : pos}`;
const position = (c) => c.lat == null && c.lon == null ? null :
  `${coord(c.lat, "N", "S")} ${coord(c.lon, "E", "W")}`;

async function openPreview(name, kind, index = 0) {
  const token = ++pv.token;
  if (name !== pv.name) Object.assign(pv, { x: null, y: null });
  Object.assign(pv, { name, kind, file: null, index: null, cast: null });
  document.body.classList.add("previewing");
  $("preview-name").textContent = name;
  $("preview-count").textContent = "";
  $("preview-download").href = outputUrl(name);
  $("cast-filter").placeholder = `Filter ${words().many}`;
  $("cast-list").innerHTML = `<p class="empty">Reading…</p>`;
  $("file-info").innerHTML = $("cast-view").innerHTML = "";
  let file;
  try {
    file = await api(previewUrl(name));
  } catch (e) {
    if (token === pv.token) $("cast-list").innerHTML = `<p class="msg bad">${esc(e.message)}</p>`;
    return;
  }
  if (token !== pv.token) return;
  pv.file = file;
  $("preview-count").textContent = count(file.casts.length, words().one);
  renderFileInfo();
  renderCastList();
  if (file.casts.length) showCast(Math.min(index, file.casts.length - 1));
}

function closePreview() {
  pv.token++;
  pv.name = null;
  document.body.classList.remove("previewing");
}

function kvTable(rows) {
  return `<table class="kv">` + rows.map(([key, value, title, units]) => {
    const none = value == null || (Array.isArray(value) && !value.length);
    const text = none ? "—" : Array.isArray(value) ? value.join(", ") : String(value);
    return `<tr><th${title ? ` title="${esc(title)}"` : ""}>${esc(key)}</th>` +
      `<td class="${none ? "none" : ""}">${esc(text)}` +
      (units && !none ? ` <span class="unit">${esc(units)}</span>` : "") + "</td></tr>";
  }).join("") + "</table>";
}

function renderFileInfo() {
  const f = pv.file;
  const dims = Object.entries(f.dimensions).map(([k, n]) => `${k} ${n}`).join(" · ");
  $("file-info").innerHTML = `<details class="section"><summary>File: ` +
    `${Object.keys(f.attributes).length} attributes · ${f.variables.length} variables · ` +
    `${esc(dims)}</summary><h4>Global attributes</h4>` + kvTable(Object.entries(f.attributes)) +
    `<h4>Variables</h4>` + f.variables.map((v) =>
      `<details class="var"><summary><code>${esc(v.name)}</code> <span class="meta">` +
      `(${esc(v.dimensions.join(", "))}) ${esc(v.type)}` +
      (v.attributes.long_name ? ` · ${esc(v.attributes.long_name)}` : "") + "</span></summary>" +
      kvTable(Object.entries(v.attributes)) + "</details>").join("") + "</details>";
}

function renderCastList() {
  if (!pv.file) return;
  const q = $("cast-filter").value.trim().toLowerCase();
  const shown = pv.file.casts.filter((c) => !q ||
    [c.id, c.time, c.dataset].join(" ").toLowerCase().includes(q));
  $("cast-list").innerHTML = shown.map((c) =>
    `<div class="cast${c.index === pv.index ? " active" : ""}" data-index="${c.index}">` +
    `<span class="name">${esc(c.id)}</span><div class="meta">` +
    esc([c.time, position(c), c.size != null ? count(c.size, words().row) : null]
      .filter(Boolean).join(" · ")) + "</div></div>").join("") ||
    `<p class="empty">Nothing matches.</p>`;
}

async function showCast(index) {
  const token = pv.token;
  pv.index = index;
  for (const el of $("cast-list").querySelectorAll(".cast")) {
    el.classList.toggle("active", +el.dataset.index === index);
    if (+el.dataset.index === index) el.scrollIntoView({ block: "nearest" });
  }
  let cast;
  try {
    cast = await api(`${previewUrl(pv.name)}/casts/${index}`);
  } catch (e) {
    if (token === pv.token && pv.index === index) {
      $("cast-view").innerHTML = `<p class="msg bad">${esc(e.message)}</p>`;
    }
    return;
  }
  if (token !== pv.token || pv.index !== index) return;
  pv.cast = cast;
  renderCast();
}

function renderCast() {
  const c = pv.file.casts[pv.index], cast = pv.cast, w = words();
  const head = `<div class="cast-head"><strong>${esc(c.id)}</strong><div class="meta">` +
    esc([`${w.one} ${c.index + 1} of ${pv.file.casts.length}`, c.time, position(c), c.dataset,
         c.size != null ? count(c.size, w.row) : null].filter(Boolean).join(" · ")) +
    "</div></div>";
  const tables = cast.tables.map((t, i) => {
    const plot = i === 0 && pv.kind !== "taxa" && t.rows > 1 && t.columns.filter(plotted).length > 1;
    return `<div class="cast-main">${plot ? `<figure class="plot" id="plot"></figure>` : ""}` +
      `<div class="levels" data-table="${i}"></div></div>` +
      (t.empty.length ? `<p class="meta">No value in this ${w.one}: ${esc(t.empty.join(", "))}</p>`
                      : "");
  }).join("");
  $("cast-view").innerHTML = head + tables +
    `<details class="section fields"${pv.fieldsOpen ? " open" : ""}><summary>` +
    `${w.one[0].toUpperCase() + w.one.slice(1)} fields (${cast.fields.length})</summary>` +
    kvTable(cast.fields.map((f) => [f.name, f.value, f.long_name, f.units])) + "</details>";
  cast.tables.forEach((t, i) => renderLevels(document.querySelector(`.levels[data-table="${i}"]`), t));
  if ($("plot")) renderPlot();
}

function renderLevels(box, t) {
  box.innerHTML = `<table class="data"><thead><tr><th class="n">#</th>` + t.columns.map((c) =>
    `<th class="${c.flag ? "flag" : ""}" title="${esc(c.long_name || c.name)}">${esc(c.name)}` +
    (c.units ? `<small>${esc(c.units)}</small>` : "") + "</th>").join("") +
    "</tr></thead><tbody></tbody></table>";
  const body = box.querySelector("tbody");
  const more = document.createElement("button");
  more.className = "small more";
  let shown = 0;
  const add = () => {
    const end = Math.min(t.rows, shown + PAGE);
    let html = "";
    for (let r = shown; r < end; r++) {
      html += `<tr><td class="n">${r + 1}</td>` + t.columns.map((c) => {
        const v = c.values[r];
        return `<td class="${c.flag ? "flag" : c.numeric ? "" : "text"}">${v == null ? "" : esc(v)}</td>`;
      }).join("") + "</tr>";
    }
    body.insertAdjacentHTML("beforeend", html);
    shown = end;
    more.hidden = shown >= t.rows;
    more.textContent = `Show ${Math.min(PAGE, t.rows - shown)} more (${t.rows - shown} not shown)`;
  };
  more.onclick = add;
  box.append(more);
  add();
}

// A profile: one variable against depth, depth increasing downward. Any numeric column can
// be either axis; a level number stands in for depth when depth does not vary (a track).
const plotted = (c) => c.numeric && !c.flag;

function extent(values) {
  let lo = Infinity, hi = -Infinity;
  for (const v of values) { if (v < lo) lo = v; if (v > hi) hi = v; }
  return [lo, hi];
}

function niceScale(lo, hi, count) {
  if (lo === hi) { const pad = Math.abs(lo) * 0.05 || 1; lo -= pad; hi += pad; }
  const raw = (hi - lo) / count, mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw);
  const first = Math.floor(lo / step) * step, last = Math.ceil(hi / step) * step;
  const ticks = [];
  for (let t = first; t <= last + step / 2; t += step) ticks.push(+t.toPrecision(12));
  return { lo: first, hi: last, ticks };
}

function renderPlot() {
  const fig = $("plot"), t = pv.cast.tables[0];
  const level = { name: "level", values: Array.from({ length: t.rows }, (_, i) => i + 1),
                  down: true, units: null };
  const cols = [...t.columns.filter(plotted), level];
  const byName = Object.fromEntries(cols.map((c) => [c.name, c]));
  const varies = (c) => { const [a, b] = extent(c.values.filter((v) => v != null)); return a < b; };
  const y = byName[pv.y] ? pv.y : (cols.find((c) => c.down && c !== level && varies(c)) || level).name;
  const x = byName[pv.x] && pv.x !== y ? pv.x
    : (cols.find((c) => !c.down && c !== level && c.name !== y) || cols.find((c) => c.name !== y)).name;
  const option = (c, sel) => `<option value="${esc(c.name)}"${c.name === sel ? " selected" : ""}>` +
    `${esc(c.name)}${c.units ? ` (${esc(c.units)})` : ""}</option>`;
  const X = byName[x], Y = byName[y];
  const pts = [];
  for (let i = 0; i < t.rows; i++) {
    if (X.values[i] != null && Y.values[i] != null) pts.push([X.values[i], Y.values[i], i]);
  }
  const W = 340, H = 420, L = 56, R = 14, T = 26, B = 10, pw = W - L - R, ph = H - T - B;
  let svg = `<p class="meta">No ${words().row} has both values.</p>`;
  let sx, sy;
  if (pts.length) {
    const xs = niceScale(...extent(pts.map((p) => p[0])), 4);
    const ys = niceScale(...extent(pts.map((p) => p[1])), 6);
    sx = (v) => L + (v - xs.lo) / (xs.hi - xs.lo) * pw;
    sy = (v) => T + (Y.down ? v - ys.lo : ys.hi - v) / (ys.hi - ys.lo) * ph;
    const grid = xs.ticks.map((v) => `<line class="grid" x1="${sx(v)}" x2="${sx(v)}" y1="${T}" y2="${T + ph}"/>` +
      `<text class="tick" x="${sx(v)}" y="${T - 8}" text-anchor="middle">${v}</text>`).join("") +
      ys.ticks.map((v) => `<line class="grid" x1="${L}" x2="${L + pw}" y1="${sy(v)}" y2="${sy(v)}"/>` +
      `<text class="tick" x="${L - 6}" y="${sy(v) + 4}" text-anchor="end">${v}</text>`).join("");
    const line = pts.map((p, i) => `${i ? "L" : "M"}${sx(p[0]).toFixed(1)},${sy(p[1]).toFixed(1)}`).join("");
    const dots = pts.length <= 60 ? pts.map((p) =>
      `<circle class="dot" r="4" cx="${sx(p[0]).toFixed(1)}" cy="${sy(p[1]).toFixed(1)}"/>`).join("") : "";
    svg = `<svg width="${W}" height="${H}" role="img" aria-label="${esc(x)} against ${esc(y)}">` +
      grid + `<path class="series" d="${line}"/>${dots}` +
      `<g class="hover" visibility="hidden"><line class="cross" x1="${L}" x2="${L + pw}"/>` +
      `<circle class="dot" r="4"/></g>` +
      `<rect class="hit" x="${L}" y="${T}" width="${pw}" height="${ph}" fill="transparent"/></svg>` +
      `<div class="tip" hidden></div>`;
  }
  fig.innerHTML = `<figcaption><select id="plot-x" title="Plotted">` +
    cols.filter((c) => c.name !== y && c !== level).map((c) => option(c, x)).join("") +
    `</select><span class="meta">against</span><select id="plot-y" title="Downward axis">` +
    cols.filter((c) => c.name !== x).map((c) => option(c, y)).join("") + `</select></figcaption>` + svg;
  $("plot-x").onchange = (e) => { pv.x = e.target.value; renderPlot(); };
  $("plot-y").onchange = (e) => { pv.y = e.target.value; renderPlot(); };
  if (!pts.length) return;

  // The crosshair snaps to the nearest level on the downward axis.
  const hit = fig.querySelector(".hit"), hover = fig.querySelector(".hover"), tip = fig.querySelector(".tip");
  const fmt = (c, v) => `${v}${c.units ? ` ${c.units}` : ""}`;
  hit.addEventListener("pointermove", (e) => {
    const box = hit.getBoundingClientRect(), py = e.clientY - box.top + T, px = e.clientX - box.left + L;
    let best = null, bestD = Infinity;
    for (const p of pts) {
      const d = Math.abs(sy(p[1]) - py) * 1000 + Math.abs(sx(p[0]) - px);
      if (d < bestD) { bestD = d; best = p; }
    }
    const cx = sx(best[0]), cy = sy(best[1]);
    hover.setAttribute("visibility", "visible");
    hover.querySelector("line").setAttribute("y1", cy);
    hover.querySelector("line").setAttribute("y2", cy);
    hover.querySelector("circle").setAttribute("cx", cx);
    hover.querySelector("circle").setAttribute("cy", cy);
    const strong = document.createElement("strong");
    strong.textContent = fmt(X, best[0]);
    const rest = document.createElement("span");
    rest.textContent = `${y} ${fmt(Y, best[1])}` + (Y === level ? "" : ` · level ${best[2] + 1}`);
    tip.replaceChildren(strong, rest);
    tip.hidden = false;
    const svgBox = fig.querySelector("svg").getBoundingClientRect(), figBox = fig.getBoundingClientRect();
    const left = svgBox.left - figBox.left + cx + 12;
    tip.style.left = `${Math.min(left, svgBox.left - figBox.left + W - tip.offsetWidth)}px`;
    tip.style.top = `${svgBox.top - figBox.top + cy + 10}px`;
  });
  hit.addEventListener("pointerleave", () => { hover.setAttribute("visibility", "hidden"); tip.hidden = true; });
}

$("output-list").addEventListener("click", (e) => {
  const a = e.target.closest("a.preview");
  if (!a) return;
  e.preventDefault();
  openPreview(a.dataset.name, a.dataset.kind);
});
$("preview-close").onclick = closePreview;
$("cast-filter").addEventListener("input", renderCastList);
$("cast-list").addEventListener("click", (e) => {
  const el = e.target.closest(".cast");
  if (el) showCast(+el.dataset.index);
});
$("cast-view").addEventListener("toggle", (e) => {
  if (e.target.matches(".fields")) pv.fieldsOpen = e.target.open;
}, true);
document.addEventListener("keydown", (e) => {
  if (!pv.name) return;
  if (e.key === "Escape" && !(e.target === $("cast-filter") && e.target.value)) {
    closePreview();
    return;
  }
  const inList = e.target === document.body ||
    (e.target instanceof Element && e.target.closest("#cast-list"));
  if ((e.key === "ArrowDown" || e.key === "ArrowUp") && inList) {
    const items = [...$("cast-list").querySelectorAll(".cast")];
    const next = items[items.findIndex((el) => +el.dataset.index === pv.index) +
                       (e.key === "ArrowDown" ? 1 : -1)];
    if (next) { e.preventDefault(); showCast(+next.dataset.index); }
  }
});

// ------------------------------------------------------------------ commands
async function open(acc, keepSource = false) {
  if (ui.dirty && acc !== ui.acc && !window.confirm("Discard your unsaved edits to decisions.toml?")) {
    return;
  }
  closePreview();
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
