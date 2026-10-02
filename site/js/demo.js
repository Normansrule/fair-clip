// fair-clip interactive demo. No dependencies, no network beyond this site's own JSON.
"use strict";

const FEATURES = ["energy", "relevance", "rate", "scene", "silence"];
const SIGN = { energy: 1, relevance: 1, rate: 1, scene: 1, silence: -1 };
const DEFAULT = { energy: 0.25, relevance: 0.35, rate: 0.15, scene: 0.10, silence: 0.15 };
const W_MIN = 0.02, W_MAX = 0.60, SOURCE_LEN = 125.8;
const $ = (id) => document.getElementById(id);

// Same projection as scoring.normalise(): clip into [W_MIN, W_MAX], rescale the free weights so Σw = 1.
function normalise(w) {
  let v = FEATURES.map((k) => Math.max(0, w[k] || 0));
  let s = v.reduce((a, b) => a + b, 0);
  v = s === 0 ? v.map(() => 1 / v.length) : v.map((x) => x / s);
  for (let it = 0; it < 50; it++) {
    v = v.map((x) => Math.min(W_MAX, Math.max(W_MIN, x)));
    const excess = v.reduce((a, b) => a + b, 0) - 1;
    if (Math.abs(excess) < 1e-12) break;
    const free = v.map((x) => x > W_MIN + 1e-12 && x < W_MAX - 1e-12);
    const fs = v.reduce((a, x, i) => a + (free[i] ? x : 0), 0);
    if (fs > 0) v = v.map((x, i) => (free[i] ? x - (excess * x) / fs : x));
    else { const t = v.reduce((a, b) => a + b, 0); v = v.map((x) => x / t); }
  }
  return Object.fromEntries(FEATURES.map((k, i) => [k, v[i]]));
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text; // textContent only: never innerHTML with data
  return e;
}

// ------------------------------------------------------------------------------- 1. ranking
let CANDS = [];
function readWeights() {
  const raw = {};
  for (const k of FEATURES) raw[k] = Number($("w-" + k).value) / 100;
  return normalise(raw);
}
function rank() {
  const w = readWeights();
  for (const k of FEATURES) $("o-" + k).textContent = w[k].toFixed(2);
  const scored = CANDS.map((c) => ({ c, s: FEATURES.reduce((a, k) => a + SIGN[k] * w[k] * c.z[k], 0) }))
    .sort((a, b) => b.s - a.s);
  const lo = Math.min(0, ...scored.map((x) => x.s)), hi = Math.max(...scored.map((x) => x.s), 0.001);
  const box = $("cands");
  box.replaceChildren();
  const tl = $("timeline");
  tl.replaceChildren();
  scored.forEach(({ c, s }, i) => {
    const row = el("div", "cand" + (i === 0 ? " top" : ""));
    row.append(el("div", "rank", "#" + (i + 1)));
    const right = el("div");
    right.append(el("div", "meta", `${c.start.toFixed(1)}–${c.end.toFixed(1)} s · S = ${s >= 0 ? "+" : ""}${s.toFixed(3)} · was #${c.rank} at default weights`));
    right.append(el("div", "txt", c.text.slice(0, 140) + "…"));
    const bar = el("div", "bar");
    bar.style.width = Math.max(2, ((s - lo) / (hi - lo)) * 100) + "%";
    right.append(bar);
    row.append(right);
    box.append(row);
    const span = el("span", i === 0 ? "top" : "");
    span.style.left = (c.start / SOURCE_LEN) * 100 + "%";
    span.style.width = ((c.end - c.start) / SOURCE_LEN) * 100 + "%";
    span.title = `#${i + 1}: ${c.start.toFixed(0)}–${c.end.toFixed(0)} s`;
    tl.append(span);
  });
}

// ------------------------------------------------------------------------------- 3. gate simulator
const FIX = {
  RIGHTS_MISSING: "No rights record", VALUE_ADD_MISSING: "No original value-add", VALUE_ADD_INSUFFICIENT: "Value-add covers too little of the clip (min 25 %)",
  SYNTHETIC_DISCLOSURE_MISSING: "Altered/synthetic flag not set", AFFILIATE_START_DISCLOSURE_MISSING: "No disclosure near the start",
  AFFILIATE_SINGLE_CHANNEL: "Disclosure only on screen (FTC: audio + video is clearer)", AFFILIATE_CARD_MISSING: "No card near a recommendation",
  AFFILIATE_DESCRIPTION_ORDER: "Disclosure not above the first link", AFFILIATE_WEAK_WORDING: "Wording doesn't say you get paid",
  AFFILIATE_FABRICATED_CLAIM: "Scarcity/testimonial/result claim", LINK_NOT_ALLOWLISTED: "Link not on the allowlist", LINK_CLOAKED: "Shortened / disguised link",
  METADATA_CLICKBAIT: "Clickbait title", METADATA_UNGROUNDED: "Title claims something not said in the clip", FINANCE_DISCLAIMER_MISSING: "Disclaimer missing",
  FINANCE_PUMP_PATTERN: "Hype / pump-style claim", FINANCE_PAID_UNDISCLOSED: "Paid securities promotion without payer and amount (§17(b))",
  PROMPT_INJECTION_SUSPECTED: "Instruction-like text in the transcript", DAILY_CAP_REACHED: "Daily publishing cap reached (3)",
};
const WARN = new Set(["AFFILIATE_SINGLE_CHANNEL", "PROMPT_INJECTION_SUSPECTED"]);

function simulate() {
  const f = {};
  document.querySelectorAll("#sim input[type=checkbox]").forEach((i) => (f[i.dataset.f] = i.checked));
  const cov = Number($("cov").value), today = Number($("today").value);
  $("o-cov").textContent = cov + "%";
  $("o-today").textContent = String(today);
  const out = [];
  if (!f.rights) out.push("RIGHTS_MISSING");
  if (!f.valueadd) out.push("VALUE_ADD_MISSING");
  else if (cov < 25) out.push("VALUE_ADD_INSUFFICIENT");
  if (!f.synthetic) out.push("SYNTHETIC_DISCLOSURE_MISSING");
  if (f.affiliate) {
    if (!f.aff_start && !f.aff_spoken) out.push("AFFILIATE_START_DISCLOSURE_MISSING");
    else if (!(f.aff_start && f.aff_spoken)) out.push("AFFILIATE_SINGLE_CHANNEL");
    if (!f.aff_card) out.push("AFFILIATE_CARD_MISSING");
    if (!f.aff_order) out.push("AFFILIATE_DESCRIPTION_ORDER");
    if (!f.aff_wording) out.push("AFFILIATE_WEAK_WORDING");
    if (f.aff_scarcity) out.push("AFFILIATE_FABRICATED_CLAIM");
  }
  if (f.shortlink) out.push("LINK_NOT_ALLOWLISTED");
  if (f.clickbait) out.push("METADATA_CLICKBAIT", "METADATA_UNGROUNDED");
  if (f.markets) {
    if (!f.mk_disclaimer) out.push("FINANCE_DISCLAIMER_MISSING");
    if (f.mk_pump) out.push("FINANCE_PUMP_PATTERN");
    if (f.mk_paid) out.push("FINANCE_PAID_UNDISCLOSED");
  }
  if (f.injection) out.push("PROMPT_INJECTION_SUSPECTED");
  if (today >= 3) out.push("DAILY_CAP_REACHED");
  const blocks = out.filter((c) => !WARN.has(c));
  const v = $("verdict");
  v.className = "verdict " + (blocks.length ? "block" : "pass");
  v.textContent = blocks.length ? `✕ Blocked: ${blocks.length} reason(s). It can't reach approval.` :
    out.length ? `⚠ Passes with ${out.length} warning(s). The reviewer must acknowledge them.` : "✓ Passes. Next stop: a human.";
  const ul = $("codes");
  ul.replaceChildren();
  for (const c of out) {
    const li = el("li");
    li.append(el("span", WARN.has(c) ? "w" : "b", WARN.has(c) ? "⚠" : "✕"));
    const t = el("span");
    t.append(el("code", "", c), document.createTextNode(" " + FIX[c]));
    li.append(t);
    ul.append(li);
  }
}

// ------------------------------------------------------------------------------- 4. real reports
function showReport(g, cid) {
  document.querySelectorAll("#tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.cid === cid)));
  const r = g[cid];
  const v = $("rv");
  const blocks = r.results.filter((x) => x.severity === "block");
  const warns = r.results.filter((x) => x.severity === "warn");
  v.className = "verdict " + (r.passed ? "pass" : "block");
  v.textContent = (r.passed ? "✓ Passed" : "✕ Blocked") + ` · ${blocks.length} block · ${warns.length} warn · "${r.title}"`;
  const ul = $("rcodes");
  ul.replaceChildren();
  for (const x of r.results) {
    const li = el("li");
    const sym = { pass: ["p", "✓"], warn: ["w", "⚠"], block: ["b", "✕"] }[x.severity];
    li.append(el("span", sym[0], sym[1]));
    const t = el("span");
    t.append(el("strong", "", x.check + " "));
    if (x.code !== "OK") t.append(el("code", "", x.code + " "));
    t.append(document.createTextNode(x.message));
    li.append(t);
    ul.append(li);
  }
}

async function main() {
  const [demo, gates] = await Promise.all([fetch("data/demo.json").then((r) => r.json()), fetch("data/gates.json").then((r) => r.json())]);
  CANDS = demo.candidates;
  for (const k of FEATURES) $("w-" + k).addEventListener("input", rank);
  $("reset").addEventListener("click", () => { for (const k of FEATURES) $("w-" + k).value = Math.round(DEFAULT[k] * 100); rank(); });
  rank();
  document.querySelectorAll("#sim input").forEach((i) => i.addEventListener("input", simulate));
  simulate();
  const tabs = $("tabs");
  const labels = ["A · top moment + commentary", "B · no value-add", "C · affiliate example"];
  Object.keys(gates).forEach((cid, i) => {
    const b = el("button", "", labels[i] || cid);
    b.dataset.cid = cid;
    b.setAttribute("role", "tab");
    b.addEventListener("click", () => showReport(gates, cid));
    tabs.append(b);
  });
  showReport(gates, Object.keys(gates)[1]);
}
main().catch((e) => { const n = $("cands"); if (n) n.textContent = "Could not load demo data: " + e; });
