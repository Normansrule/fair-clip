// Browser backend: Python (Pyodide) runs fair-clip's own decision code, FFmpeg (ffmpeg.wasm) does
// the media work. Both are served from this site (see scripts/vendor_web.py); nothing is uploaded.
import { FFmpeg } from "./vendor/ffmpeg/index.js";
import { makeZip } from "./zip.js";

const here = (p) => new URL(p, import.meta.url).href;
const CHUNK = 8 * 1024 * 1024;
const MAX_BYTES = 4 * 1024 ** 3;

export async function createBackend(status) {
  status("loading Python…");
  const { loadPyodide } = await import(here("./vendor/pyodide/pyodide.mjs"));
  const py = await loadPyodide({ indexURL: here("./vendor/pyodide/") });
  await py.loadPackage(["numpy", "pydantic"], { messageCallback: () => {} });
  const code = await (await fetch(here("./fairclip.zip"))).arrayBuffer();
  py.unpackArchive(code, "zip", { extractDir: "/lib/fairclip-src" });
  py.runPython("import sys; sys.path.insert(0, '/lib/fairclip-src')");
  const sw = py.pyimport("fairclip.studio_web");

  status("loading FFmpeg…");
  const ff = new FFmpeg();
  let logTail = [];
  ff.on("log", ({ message }) => { logTail.push(message); if (logTail.length > 40) logTail.shift(); });
  await ff.load({ coreURL: here("./vendor/ffmpeg-core/ffmpeg-core.js"), wasmURL: here("./vendor/ffmpeg-core/ffmpeg-core.wasm") });
  await ff.createDir("/fonts");
  await ff.createDir("/work");
  const fonts = await (await fetch(here("./fonts/fonts.json"))).json();
  for (const f of fonts) {
    await ff.writeFile(`/fonts/${f}`, new Uint8Array(await (await fetch(here(`./fonts/${f}`))).arrayBuffer()));
  }

  let video = null;   // the File
  let mounted = false;
  let made = [];       // [{i, filename, data, result}]
  const j = (s) => { const r = JSON.parse(s); if (!r.ok) throw new Error(r.error); return r; };

  async function run(args, onTime) {
    logTail = [];
    const handler = ({ time }) => onTime && onTime(time / 1e6);
    ff.on("progress", handler);
    try {
      const rc = await ff.exec(args);
      if (rc !== 0) throw new Error("FFmpeg failed:\n" + logTail.slice(-8).join("\n"));
    } finally {
      ff.off("progress", handler);
    }
  }

  async function mountVideo(file) {
    if (mounted) { await ff.unmount("/in"); mounted = false; }
    try { await ff.deleteDir("/in"); } catch { /* not there yet */ }
    await ff.createDir("/in");
    // WORKERFS reads the file in place, so even a long video isn't copied into memory.
    mounted = await ff.mount("WORKERFS", { files: [file] }, "/in");
    if (!mounted) throw new Error("this browser can't read the file in place; try Chrome, Edge or Firefox");
  }

  async function still(path, t, width) {
    await run(["-ss", t.toFixed(2), "-i", path, "-frames:v", "1", "-vf", `scale=${width}:-2`, "-q:v", "5", "-y", "/work/still.jpg"]);
    const img = await ff.readFile("/work/still.jpg");
    await ff.deleteFile("/work/still.jpg");
    return URL.createObjectURL(new Blob([img], { type: "image/jpeg" }));
  }

  async function sha256(file, progress) {
    sw.hash_start();
    for (let o = 0; o < file.size; o += CHUNK) {
      const buf = new Uint8Array(await file.slice(o, o + CHUNK).arrayBuffer());
      sw.hash_update(buf);
      progress(`checking the file… ${Math.min(100, Math.round(((o + CHUNK) / file.size) * 100))}%`);
    }
    return sw.hash_done();
  }

  return {
    kind: "web",
    async analyze({ file, captions, brand, rights, duration }, progress) {
      if (file.size > MAX_BYTES) throw new Error("that video is over 4 GB: trim it first, or use the app");
      video = file;
      made = [];
      await sha256(file, progress);
      progress("reading the audio…");
      await mountVideo(file);
      await run(["-i", `/in/${file.name}`, "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-y", "/work/a.raw"]);
      const pcm = await ff.readFile("/work/a.raw");
      await ff.deleteFile("/work/a.raw");
      progress("finding the best moments…");
      const res = j(sw.analyze(JSON.stringify({ brand, rights, duration }), captions || "", pcm));
      // Stills made by FFmpeg itself, so moments show a picture even where the browser can't play the file.
      for (const m of res.candidates) {
        progress(`making previews… ${m.i + 1}/${res.candidates.length}`);
        try { m.poster = await still(`/in/${file.name}`, (m.start + m.end) / 2, 480); } catch { /* preview only */ }
      }
      return res;
    },
    async titleCheck(i, take, title) {
      return j(sw.title_check(i, JSON.stringify(take), title)).problems;
    },
    async make(clips, quality, onClip) {
      made = [];
      for (const [k, c] of clips.entries()) {
        const p = j(sw.plan(c.i, JSON.stringify(c.take), video.name, quality));
        await ff.writeFile("/work/overlay.ass", new TextEncoder().encode(p.ass));
        const args = p.args.map((a) => a.replaceAll("@in/overlay.ass", "/work/overlay.ass").replaceAll("@in/", "/in/")
          .replaceAll("@out/", "/work/").replaceAll("@fonts/", "/fonts/"));
        onClip(k, 0, "rendering");
        await run(args, (t) => onClip(k, Math.min(0.99, t / p.duration), "rendering"));
        const data = await ff.readFile("/work/clip.mp4");
        let poster = null;
        try { poster = await still("/work/clip.mp4", Math.min(3.5, p.duration / 3), 360); } catch { /* preview only */ }
        await ff.deleteFile("/work/clip.mp4");
        const digest = await crypto.subtle.digest("SHA-256", data);
        const hex = [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
        const r = j(sw.finish(c.i, c.title || "", hex)).clip;
        const url = r.passed ? URL.createObjectURL(new Blob([data], { type: "video/mp4" })) : null;
        made.push({ i: c.i, filename: r.filename, data: r.passed ? data : null, result: r });
        onClip(k, 1, r.passed ? "ready" : "blocked", { ...r, url, poster });
      }
      const files = j(sw.package(JSON.stringify(made.map((m) => m.i)))).files;
      return files;
    },
    async zip(files) {
      const enc = new TextEncoder();
      const items = made.filter((m) => m.data).map((m) => ({ name: m.filename, data: m.data }));
      for (const [name, text] of Object.entries(files)) items.push({ name, data: enc.encode(text) });
      return makeZip(items);
    },
  };
}
