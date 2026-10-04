// Renders the example clips from data/studio-example.json (made by scripts/export_studio_example.py).
// Text goes in with textContent only.
(async () => {
  const box = document.getElementById("example-clips");
  if (!box) return;
  let data;
  try { data = await (await fetch("data/studio-example.json")).json(); } catch { box.textContent = "Couldn't load the example."; return; }
  const el = (tag, props = {}, ...kids) => { const e = Object.assign(document.createElement(tag), props); e.append(...kids); return e; };
  const copyBtn = (label, text) => {
    const b = el("button", { type: "button", textContent: label });
    b.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(text); } catch { return; }
      b.textContent = "✓ Copied"; b.classList.add("copied");
      setTimeout(() => { b.textContent = label; b.classList.remove("copied"); }, 1500);
    });
    return b;
  };
  box.replaceChildren(...data.clips.map((c) => el("article", { className: "clipcard" },
    el("video", { src: c.video, poster: c.poster, controls: true, preload: "none", playsInline: true }),
    el("h3", { textContent: c.title }),
    el("p", { className: "take", textContent: `Owner's take: ${c.take}` }),
    el("pre", { textContent: c.description }),
    el("div", { className: "row" }, copyBtn("Copy title", c.title), copyBtn("Copy description", c.description), copyBtn("Copy tags", c.tags.join(", "))),
    el("ul", {}, ...c.checklist.map((t) => el("li", { textContent: t }))),
  )));
})();
