// fair-clip Studio: three steps, one page. The same UI drives two backends:
//   web: Python (Pyodide) + FFmpeg (WebAssembly) in this tab        -> backend-web.js
//   app: the fair-clip app on this computer (sandboxed FFmpeg)       -> backend-app.js
// No innerHTML with data anywhere: every string from a file, the user or the backend goes in via
// textContent / value, so a caption line can never become markup.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const kind = document.querySelector('meta[name="fairclip-backend"]')?.content === "app" ? "app" : "web";
document.body.dataset.backend = kind;

const state = { file: null, captions: "", duration: 0, objectURL: null, moments: [], pending: null, files: null };
let backend = null;
const engine = $("#engine");
const ready = (async () => {
  try {
    const mod = kind === "app" ? await import("./backend-app.js") : await import("./backend-web.js");
    backend = await mod.createBackend((m) => { engine.textContent = m; });
    engine.textContent = kind === "app" ? "● app" : "● ready (runs in this tab)";
    engine.classList.add("ready");
  } catch (e) {
    engine.textContent = "couldn't start";
    showError("#s1-error", `The Studio engine didn't load: ${e.message}\nUse a recent Chrome, Edge or Firefox on a computer (phones don't have the memory).`);
    throw e;
  }
})();

// ------------------------------------------------------------------ helpers
function showError(sel, msg) { const el = $(sel); el.textContent = msg; el.hidden = !msg; if (msg) el.scrollIntoView({ block: "center", behavior: "smooth" }); }
function step(n) {
  for (const k of [1, 2, 3]) $(`#s${k}`).hidden = k !== n;
  $$(".steps li").forEach((li) => { const k = +li.dataset.step; li.classList.toggle("on", k === n); li.classList.toggle("done", k < n); });
  window.scrollTo({ top: 0, behavior: "smooth" });
}
const fmt = (t) => `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, "0")}`;
async function copy(text, btn) {
  try { await navigator.clipboard.writeText(text); } catch {
    const ta = Object.assign(document.createElement("textarea"), { value: text });
    document.body.append(ta); ta.select(); document.execCommand("copy"); ta.remove();
  }
  const old = btn.textContent; btn.textContent = "✓ Copied"; btn.classList.add("copied");
  setTimeout(() => { btn.textContent = old; btn.classList.remove("copied"); }, 1600);
}
function download(blob, name) {
  const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: name });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 60_000);
}

// ------------------------------------------------------------------ step 1: inputs
function setVideo(file) {
  state.file = file;
  state.duration = 0;
  if (state.objectURL) URL.revokeObjectURL(state.objectURL);
  state.objectURL = URL.createObjectURL(file);
  const v = $("#preview");
  v.src = state.objectURL; v.hidden = false;
  v.onloadedmetadata = () => { state.duration = v.duration; $("#video-name").textContent = `${file.name} · ${fmt(v.duration)} · ${v.videoWidth}×${v.videoHeight}`; };
  v.onerror = () => { $("#video-name").textContent = `${file.name} (this browser can't preview it; it may still work)`; };
  $("#video-name").textContent = file.name;
  $("#drop-video").classList.add("ok");
  if (!$("#r-title").value) $("#r-title").value = file.name.replace(/\.[^.]+$/, "").slice(0, 200);
}
function videoMetadata() {
  // Duration comes from the browser's own decoder; formats it can't preview just skip that check.
  const v = $("#preview");
  if (v.readyState >= 1 && v.duration) { state.duration = v.duration; return Promise.resolve(); }
  return new Promise((resolve) => {
    const done = () => { if (v.duration && Number.isFinite(v.duration)) state.duration = v.duration; resolve(); };
    v.addEventListener("loadedmetadata", done, { once: true });
    v.addEventListener("error", () => resolve(), { once: true });
    setTimeout(resolve, 5000);
  });
}
async function setCaptions(file) {
  if (file.size > 10 * 1024 * 1024) { showError("#s1-error", "That captions file is over 10 MB."); return; }
  state.captions = await file.text();
  $("#captions-name").textContent = file.name;
  $("#drop-captions").classList.add("ok");
}
$("#video").addEventListener("change", (e) => e.target.files[0] && setVideo(e.target.files[0]));
$("#captions").addEventListener("change", (e) => e.target.files[0] && setCaptions(e.target.files[0]));
for (const id of ["#drop-video", "#drop-captions"]) {
  const el = $(id);
  el.addEventListener("dragover", (e) => { e.preventDefault(); el.classList.add("over"); });
  el.addEventListener("dragleave", () => el.classList.remove("over"));
  el.addEventListener("drop", (e) => {
    e.preventDefault(); el.classList.remove("over");
    const f = e.dataTransfer.files[0];
    if (f) (id === "#drop-video" ? setVideo(f) : setCaptions(f));
  });
}
function rightsUI() {
  const basis = $('input[name="basis"]:checked').value;
  $$(".rights-more [data-for]").forEach((el) => { el.hidden = !el.dataset.for.split(" ").includes(basis); });
}
$$('input[name="basis"]').forEach((r) => r.addEventListener("change", rightsUI));
rightsUI();

function readRights() {
  let basis = $('input[name="basis"]:checked').value;
  const r = { basis, title: $("#r-title").value.trim() || "Untitled video", holder: $("#r-holder").value.trim(),
    synthetic: $("#r-synthetic").checked, evidence: $("#r-evidence").value.trim() };
  if (basis === "creative_commons") {
    const lic = $("#r-cc").value;
    if (lic === "PD") { r.basis = "public_domain"; } else { r.cc_license = lic; }
    r.attribution = $("#r-attr").value.trim() || `"${r.title}" by ${r.holder}`;
  }
  if (!r.holder) r.holder = basis === "own_content" ? ($("#b-channel").value.trim() || "me") : "";
  return r;
}
function readBrand() {
  return {
    channel: $("#b-channel").value.trim(), style: $("#b-style").value, accent: $("#b-accent").value.toUpperCase(),
    aspect: $("#b-aspect").value, reframe: $("#b-reframe").value, clips: Math.max(1, Math.min(8, +$("#b-clips").value || 3)),
    topics: $("#b-topics").value.split(",").map((t) => t.trim()).filter(Boolean).slice(0, 30),
  };
}
function fillFrom(project) {
  const b = project.brand || {}, r = project.rights || {};
  $("#b-channel").value = b.channel || ""; $("#b-style").value = b.style || "bold"; $("#b-accent").value = (b.accent || "#3DDC97").toLowerCase();
  $("#b-aspect").value = b.aspect || "9:16"; $("#b-reframe").value = b.reframe || "fit"; $("#b-clips").value = b.clips || 3;
  $("#b-topics").value = (b.topics || []).join(", ");
  const basis = r.basis === "public_domain" ? "creative_commons" : r.basis || "own_content";
  $(`input[name="basis"][value="${basis}"]`).checked = true;
  $("#r-title").value = r.title || ""; $("#r-holder").value = r.holder || ""; $("#r-attr").value = r.attribution || "";
  $("#r-cc").value = r.basis === "public_domain" ? "PD" : r.cc_license || "CC0-1.0"; $("#r-evidence").value = r.evidence || "";
  $("#r-synthetic").checked = !!r.synthetic;
  rightsUI();
}

fetch("example/project.json").then((r) => { if (!r.ok) $(".try").hidden = true; }, () => { $(".try").hidden = true; });
$("#use-example").addEventListener("click", async (e) => {
  const btn = e.currentTarget; btn.disabled = true; btn.textContent = "Loading the example…";
  try {
    const get = async (n) => { const r = await fetch(`example/${n}`); if (!r.ok) throw new Error(`example/${n}: ${r.status}`); return r; };
    const [v, c, p] = await Promise.all([get("source.mp4"), get("captions.srt"), get("project.json")]);
    const project = await p.json();
    fillFrom(project);
    state.pending = project.clips || null;
    setVideo(new File([await v.blob()], "resonance-example.mp4", { type: "video/mp4" }));
    state.captions = await c.text();
    $("#captions-name").textContent = "captions.srt (example)"; $("#drop-captions").classList.add("ok");
    btn.textContent = "✓ Example loaded: press “Find the best moments”";
  } catch (err) {
    btn.textContent = "▶ Try it with the example video";
    showError("#s1-error", `Couldn't load the example: ${err.message}`);
  } finally { btn.disabled = false; }
});

$("#find").addEventListener("click", async () => {
  showError("#s1-error", "");
  const brand = readBrand(), rights = readRights();
  const missing = [];
  if (!state.file) missing.push("choose a video");
  if (kind === "web" && !state.captions) missing.push("add its captions file (.srt or .vtt)");
  if (!brand.channel) missing.push("type your channel name");
  if (rights.basis !== "own_content" && !rights.holder) missing.push("say who the rights holder is");
  if (["written_permission", "license"].includes(rights.basis) && rights.evidence.length < 3) missing.push("say where the permission or license is kept");
  if (missing.length) { showError("#s1-error", "Almost there: " + missing.join(", ") + "."); return; }
  const btn = $("#find"); btn.disabled = true;
  const status = (m) => { $("#s1-status").textContent = m; };
  try {
    status("starting…");
    await ready;
    if (!state.duration) await videoMetadata();
    const res = await backend.analyze({ file: state.file, captions: state.captions, brand, rights, duration: state.duration || null }, status);
    state.moments = res.candidates;
    renderMoments(res);
    status("");
    step(2);
  } catch (e) {
    status("");
    showError("#s1-error", e.message);
  } finally { btn.disabled = false; }
});

// ------------------------------------------------------------------ step 2: takes
function tplFields(card) {
  const t = $(".m-template", card).value;
  $(".m-headline", card).hidden = !["key_number", "definition"].includes(t);
  $(".m-unit", card).hidden = t !== "key_number";
  $(".m-items", card).hidden = t !== "steps";
}
function readTake(card) {
  const template = $(".m-template", card).value;
  const take = { text: $(".m-take", card).value.trim(), template, kind: "context_card" };
  if (template === "key_number") take.data = { headline: $(".m-head", card).value.trim(), unit: $(".m-unitv", card).value.trim() };
  if (template === "definition") take.data = { headline: $(".m-head", card).value.trim() };
  if (template === "steps") take.data = { items: $(".m-itemsv", card).value.split("\n").map((s) => s.trim()).filter(Boolean).slice(0, 5) };
  return take;
}
function renderMoments(res) {
  const box = $("#moments"); box.replaceChildren();
  const pending = state.pending || [];
  for (const m of res.candidates) {
    const card = $("#tpl-moment").content.firstElementChild.cloneNode(true);
    card.dataset.i = m.i;
    const v = $(".m-video", card);
    v.src = `${state.objectURL}#t=${m.start},${m.end}`;
    v.addEventListener("timeupdate", () => { if (v.currentTime >= m.end) v.pause(); });
    $(".m-time", card).textContent = `${fmt(m.start)}–${fmt(m.end)} · ${Math.round(m.end - m.start)} s of source`;
    $(".m-h", card).textContent = `Moment ${m.i + 1}`;
    $(".m-hook", card).textContent = m.hook ? `“${m.hook}”` : "";
    $(".m-text", card).textContent = m.text;
    $(".m-q", card).textContent = m.suggestion.prompt;
    const sel = $(".m-template", card);
    sel.value = m.suggestion.template;
    if (m.suggestion.data?.headline) $(".m-head", card).value = m.suggestion.data.headline;
    const ex = pending.find((p) => p.moment && Math.abs(p.moment.start - m.start) < 0.05 && Math.abs(p.moment.end - m.end) < 0.05);
    if (ex) {
      $(".m-take", card).value = ex.take || ""; $(".m-title", card).value = ex.title || "";
      if (ex.template) sel.value = ex.template;
      if (ex.data?.headline) $(".m-head", card).value = ex.data.headline;
      if (ex.data?.unit) $(".m-unitv", card).value = ex.data.unit;
      if (ex.data?.items) $(".m-itemsv", card).value = ex.data.items.join("\n");
      $(".m-include", card).checked = ex.include !== false;
    }
    sel.addEventListener("change", () => tplFields(card));
    tplFields(card);
    $(".m-include", card).addEventListener("change", (e) => card.classList.toggle("off", !e.target.checked));
    card.classList.toggle("off", !$(".m-include", card).checked);
    let timer = null;
    const check = () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const title = $(".m-title", card).value.trim();
        const warn = $(".m-warn", card);
        if (!title) { warn.hidden = true; return; }
        try {
          const probs = await backend.titleCheck(m.i, readTake(card), title);
          warn.textContent = probs.length ? `Title: ${probs.join("; ")}. Titles may only use words from the clip or your take.` : "";
          warn.hidden = !probs.length;
        } catch (e) { warn.textContent = e.message; warn.hidden = false; }
      }, 350);
    };
    $(".m-title", card).addEventListener("input", check);
    $(".m-take", card).addEventListener("input", check);
    box.append(card);
    if ($(".m-title", card).value) check();
  }
}
$("#back1").addEventListener("click", () => step(1));

// ------------------------------------------------------------------ step 3: make + download
$("#make").addEventListener("click", async () => {
  showError("#s2-error", "");
  const cards = $$(".moment").filter((c) => $(".m-include", c).checked);
  if (!cards.length) { showError("#s2-error", "Tick “Make this clip” on at least one moment."); return; }
  const empty = cards.filter((c) => !$(".m-take", c).value.trim()).map((c) => +c.dataset.i + 1);
  if (empty.length) { showError("#s2-error", `Write your take for moment ${empty.join(", ")} (or untick it). It's the part that makes the clip yours.`); return; }
  const clips = cards.map((c) => ({ i: +c.dataset.i, take: readTake(c), title: $(".m-title", c).value.trim() }));
  step(3);
  $("#actions").hidden = true; $("#howto").hidden = true;
  $("#s3-title").textContent = "Making your clips…";
  const results = $("#results"); results.replaceChildren();
  const slots = clips.map((c) => {
    const el = $("#tpl-result").content.firstElementChild.cloneNode(true);
    $(".r-state", el).textContent = `Clip from moment ${c.i + 1}: waiting`;
    $(".r-video", el).hidden = true; $(".r-dl", el).hidden = true; $$(".copyrow", el).forEach((r) => { r.hidden = true; });
    results.append(el);
    return el;
  });
  const total = clips.length;
  const bar = $("#bar");
  const onClip = (k, p, what, r) => {
    bar.style.width = `${Math.round(((k + (p ?? 0.5)) / total) * 100)}%`;
    const el = slots[k];
    if (what === "rendering") {
      const pct = p == null ? "…" : ` ${Math.round(p * 100)}%`;
      $("#s3-status").textContent = `Clip ${k + 1} of ${total}: rendering${pct}` + (kind === "web" ? " (in this tab: keep it open)" : "");
      $(".r-state", el).textContent = `Clip from moment ${clips[k].i + 1}: rendering${pct}`;
      return;
    }
    fillResult(el, r);
  };
  try {
    await ready;
    state.files = await backend.make(clips, $("#quality").value, onClip);
    const ok = $$(".result").filter((el) => !el.classList.contains("blocked")).length;
    bar.style.width = "100%";
    $("#s3-title").textContent = ok ? `${ok} clip${ok > 1 ? "s" : ""} ready` : "No clip passed the checks";
    $("#s3-status").textContent = ok < total ? `${total - ok} blocked by the compliance checks: the reasons are listed with the clip. Change your take or title and make them again.` : "";
    $("#actions").hidden = false; $("#zip").hidden = !ok; $("#copy-all").hidden = !ok; $("#howto").hidden = !ok;
  } catch (e) {
    $("#s3-title").textContent = "Something went wrong";
    $("#s3-status").textContent = e.message;
    $("#actions").hidden = false; $("#zip").hidden = true; $("#copy-all").hidden = true;
  }
});
function fillResult(el, r) {
  el.classList.toggle("blocked", !r.passed);
  const st = $(".r-state", el);
  st.className = `r-state ${r.passed ? "ok" : "no"}`;
  st.textContent = r.passed ? `✓ ${r.filename} · ${Math.round(r.duration)} s · passed all checks` : "✕ Blocked: not exported";
  $(".r-title", el).textContent = r.title;
  $(".r-desc", el).textContent = r.description;
  $$(".copyrow", el).forEach((row) => { row.hidden = !r.passed; });
  $(".r-ct", el).onclick = (e) => copy(r.title, e.currentTarget);
  $(".r-cd", el).onclick = (e) => copy(r.description, e.currentTarget);
  const tags = (r.tags || []).join(", ");
  $(".r-tags", el).textContent = tags ? `Tags: ${tags}` : "";
  $(".r-ctg", el).hidden = !tags;
  $(".r-ctg", el).onclick = (e) => copy(tags, e.currentTarget);
  const ul = $(".r-check", el); ul.replaceChildren();
  if (r.passed) for (const c of r.checklist) ul.append(Object.assign(document.createElement("li"), { textContent: c }));
  const bl = $(".r-blocks", el); bl.replaceChildren();
  for (const b of r.blocks) bl.append(Object.assign(document.createElement("li"), { textContent: `${b.message}${b.fix ? ` (fix: ${b.fix})` : ""}` }));
  if (r.passed && r.url) {
    const v = $(".r-video", el); v.src = r.url; v.hidden = false;
    v.onloadedmetadata = () => v.classList.toggle("wide", v.videoWidth > v.videoHeight);
    const a = $(".r-dl", el); a.href = r.url; a.download = r.filename; a.hidden = false;
  }
}
$("#zip").addEventListener("click", async (e) => {
  const btn = e.currentTarget; btn.disabled = true; const old = btn.textContent; btn.textContent = "Packing…";
  try { download(await backend.zip(state.files), "fair-clip-clips.zip"); } catch (err) { $("#s3-status").textContent = err.message; }
  finally { btn.disabled = false; btn.textContent = old; }
});
$("#copy-all").addEventListener("click", (e) => copy(state.files?.["ALL-TITLES-AND-DESCRIPTIONS.txt"] || "", e.currentTarget));
$("#again").addEventListener("click", () => step(2));
