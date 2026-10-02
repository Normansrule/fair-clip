// fair-clip web demo: the affiliate-disclosure checker (mirror of gate.check_affiliate's timing rules).
// PLAIN must stay identical to fairclip.affiliate.PLAIN_DISCLOSURE (tests/test_affiliate.py checks it).
"use strict";
(() => {
  const PLAIN = String.raw`\b(?:(?:i|we) (?:earn|get|make|receive) (?:an? )?(?:small )?(?:commissions?|money|paid|a cut|a fee)|(?:i|we)(?:'m|'re| am| are) (?:paid|compensated)|paid (?:link|promotion|partnership)|sponsored by|affiliate commission)\b`;
  const NEGATION = String.raw`\b(?:not|no|never|neither|nor|without|none|isn't|wasn't|aren't|weren't|don't|doesn't|didn't|won't|can't|haven't|hasn't)\b|n't\b`;
  const DENIAL_AFTER = String.raw`^\W*(?:\w+\W+){0,2}?(?:nothing|nobody|no one|none|zero|not a (?:cent|penny|dime|thing))\b`;
  const BENIGN = String.raw`\b(?:at )?no (?:extra |additional )?(?:cost|charge|fee)(?: to you)?\b|\bno matter\b|\bnot only\b`;
  const WINDOW = 10, MIN_CARD = 2, WORDS_PER_S = 2.9;
  const $ = (id) => document.getElementById(id);
  const RULES = {
    AFFILIATE_START_DISCLOSURE_MISSING: ["b", "No disclosure said or shown in the first 10 s"],
    AFFILIATE_SINGLE_CHANNEL: ["w", "Only said or only shown: both together are clearer (FTC)"],
    AFFILIATE_SPOKEN_UNCONFIRMED: ["w", "Said, but not confirmed: tick it only if it's your disclosure for these links"],
    AFFILIATE_SPOKEN_TOO_LATE: ["w", "Said, but after the first 10 s"],
    AFFILIATE_WEAK_WORDING: ["b", "The sentence doesn't say you get paid"],
    AFFILIATE_REQUIRED_WORDING_MISSING: ["b", "The program's required wording isn't in your sentence"],
  };

  // Words with times, like a transcript: [start, end, word]
  function timed(text, at) {
    return text.split(/\s+/).filter(Boolean).map((w, i) => [at + i / WORDS_PER_S, at + (i + 0.8) / WORDS_PER_S, w]);
  }
  // Denials (affiliate._negated): a negation earlier in the same sentence (benign phrases like
  // "at no extra cost to you" removed first), or a denial right after ("I'm paid nothing").
  const norm = (t) => t.replace(/[\u2019\u2018\u02bc\uff07]/g, "'");
  function negated(text, start, end) {
    const stops = ".!?;\n";
    let a = 0;
    for (const ch of stops) a = Math.max(a, text.lastIndexOf(ch, start - 1) + 1);
    let b = text.length;
    for (const ch of stops) { const i = text.indexOf(ch, end); if (i >= 0) b = Math.min(b, i); }
    const before = text.slice(a, start).replace(new RegExp(BENIGN, "gi"), " ");
    const after = text.slice(end, b);
    return new RegExp(NEGATION, "i").test(before) || new RegExp(DENIAL_AFTER, "i").test(after);
  }
  // Same algorithm as affiliate.detect_spoken: join words, map each match to the word it starts in.
  function detect(words) {
    let text = "";
    const starts = [];
    for (const [s, , w] of words) { if (text) text += " "; starts.push([text.length, s]); text += norm(w); }
    const out = [];
    for (const m of text.matchAll(new RegExp(PLAIN, "gi"))) {
      if (negated(text, m.index, m.index + m[0].length)) continue; // "not sponsored by", "I'm paid nothing"
      let t = 0;
      for (const [off, s] of starts) if (off <= m.index) t = s;
      out.push([Math.round(t * 100) / 100, m[0]]);
    }
    return out;
  }

  function run() {
    const at = +$("dc-at").value, cardAt = +$("dc-card-at").value;
    $("o-dc-at").value = at.toFixed(1) + " s";
    $("o-dc-card-at").value = cardAt.toFixed(1) + " s";
    const confirm = $("dc-confirm").checked;
    const heard = detect(timed($("dc-said").value.slice(0, 2000), at));
    const early = heard.filter((h) => h[0] <= WINDOW);
    const shown = $("dc-card").checked && cardAt <= WINDOW && MIN_CARD > 0;
    $("dc-card-at").disabled = !$("dc-card").checked;
    const spoken = early.length > 0 && confirm;
    const codes = [];
    if (early.length && !confirm) codes.push("AFFILIATE_SPOKEN_UNCONFIRMED");
    if (!early.length && heard.length && !shown) codes.push("AFFILIATE_SPOKEN_TOO_LATE");
    if (!spoken && !shown) codes.push("AFFILIATE_START_DISCLOSURE_MISSING");
    else if (!(spoken && shown)) codes.push("AFFILIATE_SINGLE_CHANNEL");
    const sentence = $("dc-text").value;
    if (!detect(timed(sentence, 0)).length) codes.push("AFFILIATE_WEAK_WORDING");
    const req = $("dc-req").value.trim();
    if (req && !sentence.toLowerCase().includes(req.toLowerCase())) codes.push("AFFILIATE_REQUIRED_WORDING_MISSING");

    $("dc-heard").textContent = heard.length
      ? "Heard: " + heard.map(([t, p]) => `“${p}” at ${t.toFixed(1)} s`).join(" · ")
      : "No disclosure phrase is said.";
    const blocked = codes.some((c) => RULES[c][0] === "b");
    const v = $("dc-verdict");
    v.className = "verdict " + (blocked ? "block" : "pass");
    v.textContent = blocked ? "✕ Blocked" : codes.length ? "✓ Passes, with warnings a person must acknowledge" : "✓ Passes: said and shown in the first 10 s";
    const ul = $("dc-codes");
    ul.replaceChildren();
    for (const c of codes) {
      const li = document.createElement("li");
      const ic = document.createElement("span");
      ic.className = RULES[c][0];
      ic.textContent = RULES[c][0] === "b" ? "✕" : "⚠";
      const body = document.createElement("span");
      const code = document.createElement("code");
      code.textContent = c;
      body.append(code, " " + RULES[c][1]);
      li.append(ic, body);
      ul.append(li);
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("#dc input, #dc textarea").forEach((i) => i.addEventListener("input", run));
    run();
  });
})();
