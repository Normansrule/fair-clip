// App backend: the dashboard on this computer does the work (FFmpeg in the media sandbox, the same
// fair-clip code as the browser build). Requests carry the session cookie and a CSRF token.
const csrf = () => document.querySelector('meta[name="fairclip-csrf"]')?.content || "";

async function call(path, body) {
  const r = await fetch(path, {
    method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
    body: JSON.stringify(body),
  });
  return unwrap(r);
}

async function unwrap(r) {
  if (r.status === 401 || r.status === 303) throw new Error("you've been signed out: reload the page and sign in");
  let data;
  try { data = await r.json(); } catch { throw new Error(`the app answered ${r.status}`); }
  if (!r.ok || data.ok === false) throw new Error(data.error || data.detail || `the app answered ${r.status}`);
  return data;
}

function upload(fd, onProgress) {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    x.open("POST", "/api/studio/analyze");
    x.setRequestHeader("X-CSRF-Token", csrf());
    x.upload.onprogress = (e) => e.lengthComputable && onProgress(`sending the video to the app… ${Math.round((e.loaded / e.total) * 100)}%`);
    x.upload.onload = () => onProgress("checking the video and finding the best moments…");
    x.onerror = () => reject(new Error("couldn't reach the app: is it still running?"));
    x.onload = () => {
      let data;
      try { data = JSON.parse(x.responseText); } catch { reject(new Error(`the app answered ${x.status}`)); return; }
      if (x.status >= 400 || data.ok === false) reject(new Error(data.error || data.detail || `the app answered ${x.status}`));
      else resolve(data);
    };
    x.send(fd);
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export async function createBackend(status) {
  status("connected to the app");
  let session = null;
  let job = null;
  return {
    kind: "app",
    async analyze({ file, captions, brand, rights, duration }, progress) {
      const fd = new FormData();
      fd.append("video", file, file.name);
      if (captions) fd.append("captions", new Blob([captions], { type: "text/plain" }), "captions.srt");
      fd.append("meta", JSON.stringify({ brand, rights, duration }));
      const r = await upload(fd, progress);
      session = r.session;
      return r;
    },
    async titleCheck(i, take, title) {
      return (await call("/api/studio/title-check", { session, i, take, title })).problems;
    },
    async make(clips, quality, onClip) {
      job = (await call("/api/studio/make", { session, clips, quality })).job;
      const seen = new Set();
      for (;;) {
        await sleep(700);
        const s = await unwrap(await fetch(`/api/studio/job/${job}`, { credentials: "same-origin" }));
        for (const [k, r] of s.done.entries()) {
          if (!seen.has(k)) {
            seen.add(k);
            onClip(k, 1, r.passed ? "ready" : "blocked", { ...r, url: r.passed ? `/api/studio/job/${job}/clip/${k}` : null });
          }
        }
        if (s.state === "running" && s.current) onClip(s.current.k, s.current.progress, "rendering");
        if (s.state === "error") throw new Error(s.error);
        if (s.state === "done") return s.files;
      }
    },
    async zip() {
      const r = await fetch(`/api/studio/job/${job}/zip`, { credentials: "same-origin" });
      if (!r.ok) throw new Error(`the app answered ${r.status}`);
      return r.blob();
    },
  };
}
