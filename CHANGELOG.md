# Changelog

## 0.4.0 (milestone 4: the Studio)

**Added**
- **Studio: finished clips in three steps**, in the browser or in the app:
  1. **Your video:** add a video and its captions, say where it's from, and pick a channel name, style and accent colour.
  2. **Your take:** fair-clip finds the best moments. For each one, write a sentence of your own and, if you like, a title. A text card or a template (key number, definition, steps) carries your take on screen.
  3. **Download:** get one zip with the clips, a `.txt` per clip, `ALL-TITLES-AND-DESCRIPTIONS.txt`, `upload-sheet.csv` and a README. Each clip also has **Copy title / description / tags** buttons and a YouTube Studio checklist.
- **Web Studio** on GitHub Pages (`/studio/`). fair-clip's own Python runs in the page (Pyodide) and FFmpeg runs as WebAssembly (ffmpeg.wasm), so the video never leaves your computer.
  - Engines are pinned, hash-checked (`scripts/vendor_web.py`) and served from the site under a strict Content Security Policy.
- **App Studio** at `/studio/` on the dashboard (and **Open Studio** on the overview page).
  - It works without captions when speech-to-text is installed.
  - Clips are judged by your own gate settings, screen terms, kill switch, history and affiliate registry. Every Studio run is in the audit log.
- `fair-clip studio VIDEO --captions FILE --project studio.json`: the same flow on the command line, in two runs. The first writes the moments and a question for each into the project file. You add your takes, and the second run renders, gates and packages.
- **A pro look:**
  - Captions: three presets (`bold`, `clean`, `minimal`) with Inter fonts (OFL), word-by-word pop captions grouped like a caption editor would group them (never across a sentence or a pause).
  - Brand and layout: a brand accent colour (rejected if it could be mistaken for the yellow disclosure cards), a hook card that never drops words, context cards and a progress bar.
  - Edit and sound: punch-in on alternate jump cuts, a light grade, and broadcast audio (high-pass, compressor, −14 LUFS loudness, fades). Encodes come in `high` and `fast` presets.
- **Captions import:** SRT and WebVTT, including YouTube auto-captions with per-word timings and rolling duplicates. Caption text is treated as untrusted.
- **The example**, `examples/studio/`:
  - **Resonance**, a 2-minute 1080p explainer made for this project and dedicated to CC0: animated diagrams (pycairo) and a Kokoro-82M AI narrator. `scripts/make_example.py` rebuilds it.
  - The owner inputs used for the example clips (`project.json`).
  - `scripts/export_studio_example.py` produces the example clips, the site cards and the release asset `fair-clip-example-clips.zip`.
- **Metadata your viewers will see:**
  - Your take leads the description, followed by "In this clip:".
  - Hashtags come from your topics, the ones the clip talks about first, plus #Shorts for vertical clips.
  - Tags are your topics plus names said in the clip.
  - Your own title is checked live as you type, against the same rule the gate uses.
- A headless-Chromium end-to-end test of both Studio backends (CI job `studio-e2e`; locally `FAIRCLIP_E2E=1`).

**Changed**
- Title grounding allows simple inflections ("sways" ↔ "swayed"; bases of 4+ letters only). The channel name no longer counts as "said in the clip".
- Tags can't contain `<`, `>` or commas (YouTube rejects them).
- The Pages workflow builds with read-only permissions and deploys in a separate job.
- Fonts and the site's data files were hidden by a broad `.gitignore` rule; they're tracked now (a test guards it).
- Image tags are now `0.4.0`: rebuild the sandbox with `make sandbox`.

## 0.3.0 (milestone 3: the affiliate module; renamed to fair-clip)

**Renamed.** The project is now **fair-clip**:
- Python package `fairclip`, command `fair-clip`.
- Repo `Normansrule/fair-clip`, config `fairclip.toml`, environment variables `FAIRCLIP_*`.
- Images `fairclip-app` / `fairclip-media`, app data in `~/.local/share/fair-clip`.

The sample talk was regenerated with the new credit line (same timings, new SHA-256).

**Added**
- **Affiliate registry** (dashboard **Affiliate** page, or `fair-clip affiliate show | import | review-terms`):
  - **Programs you joined:** terms URL, date last re-read, link prefixes, optional required wording.
  - **Products you evaluated:** date, plus how you evaluated them, in your own words.
  - **Exact links:** shorteners and redirect parameters are refused.

  It is validated as a whole, written atomically and audited.
  - A registered link is allowlisted only for a clip whose affiliate plan lists it.
  - Approval and upload re-check the registry.
- **Per-clip affiliate editor** on the review page: pick registered links, write the disclosure, set the start card and recommendation times (cards are drawn automatically), then save to re-render and re-gate.
- **Spoken-disclosure detection timed against the transcript** (`fair-clip affiliate heard CLIP_ID`, and on the review page):
  - Nothing counts until you confirm it, so a guest's words or a denial can't stand in for your disclosure.
  - A confirmed disclosure must really be said there (±1.5 s), inside the first 10 s.
- 8 new gate reasons (60 in total: 47 block, 13 warn): `AFFILIATE_PROGRAM_UNKNOWN`, `AFFILIATE_LINK_UNREGISTERED`, `AFFILIATE_LINK_OUTSIDE_PLAN`, `AFFILIATE_REQUIRED_WORDING_MISSING` (block); `AFFILIATE_TERMS_STALE`, `AFFILIATE_SPOKEN_NOT_HEARD`, `AFFILIATE_SPOKEN_UNCONFIRMED`, `AFFILIATE_SPOKEN_TOO_LATE` (warn).
- [docs/AFFILIATE.md](docs/AFFILIATE.md). The demo registers a fictional program, an evaluated product and its link.

**Changed**
- Disclosure wording detection is stricter and denial-aware: "I earn a commission" counts. These no longer count: "I earn a living", "out of commission", "this video is not sponsored by anyone", "we don't earn a commission".
- Links: a URL in any query parameter (any case, percent-encoded) and non-ASCII hosts are refused.
- Approving is a compare-and-set, so a clip edited mid-decision can't be approved with stale hashes.

**Upgrading from clipwarden** (only if you installed 0.2.0 under the old name):
```bash
mv ~/.local/share/clipwarden ~/.local/share/fair-clip && mv ~/.local/share/fair-clip/clipwarden.toml ~/.local/share/fair-clip/fairclip.toml
mv ~/.local/share/fair-clip/clipwarden.sqlite3 ~/.local/share/fair-clip/fairclip.sqlite3 2>/dev/null
rm -f ~/.local/bin/clipwarden ~/.local/share/applications/clipwarden.desktop
```
Then rerun the installer. Sign in to YouTube again (`fair-clip auth login`), because the keychain entry has a new name.
- Image tags are now `0.3.0`: rebuild the sandbox with `make sandbox`.

## 0.2.0 (milestone 2: templates, live analytics, the app)

**Added**
- Five animated value-add templates (`key_number`, `definition`, `steps`, `compare`, `bar`), drawn by the existing escaped ASS renderer above the source frame. Every word and number on them is gated (links, sourced figures, screening, grounding). Guide: [docs/TEMPLATES.md](docs/TEMPLATES.md). The demo now uses `compare` (clip A) and `steps` (clip C).
- Read-only YouTube Analytics pull (`fair-clip analytics connect | pull | disconnect`, or **Pull now** on the dashboard). It uses a separate `yt-analytics.readonly` token and fetches totals plus the retention curve per published clip.
- Weight proposals from live data: `fair-clip weights propose --from-analytics` and **Propose from live analytics** on the Settings page (≥ 10 clips with ≥ 50 views each; still bounded to ±0.05 per weight and still needs your approval). Proposals now show a per-weight before/after table.
- The app: `scripts/install.sh` (checksum- and provenance-verified, no sudo, apps-menu launcher that shows in the Windows Start menu on WSL2), `fair-clip app` (opens the dashboard in your browser), a single-use first-run welcome link to choose the password, and a web-app manifest so Chrome and Edge can install the dashboard as its own window.
- Web demo site: template gallery and a "Get the app" section; works without sideways scrolling at phone width.

**Fixed**
- `httpx` was imported at runtime but only locked as a dev dependency: clean installs and the Docker image would crash on start. It is now a runtime dependency, and a test checks the runtime lock covers every runtime dependency.
- Fonts and the screen-terms list now ship inside the package (`fairclip/data/`), so an installed copy can't silently run the profanity screen with an empty list; a missing list is now an error. Put your own `screen_terms.txt` in the data folder to override it.
- Security review fixes (first hardening pass): links in any TLD and spoken/obfuscated links ("evil dot com"); hidden and right-to-left characters rejected; paid promotion must be disclosed inside the video; CC BY attribution per §3(a), ShareAlike blocked; rights re-checked for the publish day; uploads from a hash-verified private copy; audit chain safe under concurrent writers; approved clips frozen; metadata edits re-gated; server-side logout. The gate grew from 48 to 52 reasons (43 block, 9 warn).

**Changed**
- Container image tags are now `0.2.0`: rebuild the sandbox with `make sandbox`.

## 0.1.0 (milestone 1: MVP)

- Ten-stage pipeline with dashboard status and logs: rights intake (hard stop), transcription (sidecar or faster-whisper), highlight scoring (Eq. 1), editing (9:16 reframe, jump cuts, hook, word captions, lower third), required value-add layer, grounded metadata, compliance gate (48 reasons: 41 block, 7 warn), human approval, dry-run/live publishing, analytics import and bounded weight proposals.
- Security: network-less FFmpeg sandbox (per-job Docker or an isolated worker container), single `youtube.upload` scope with PKCE, keychain/AES-GCM token vault, log redaction, hash-chained append-only audit log, kill switch, approval bound to file and request hashes, localhost dashboard with CSRF/CSP/Host checks.
- Affiliate and markets gate rules (disclosure placement, allowlisted links only, no fabricated claims, §17(b) payment disclosure, sourced and fresh numbers, pump-pattern block). Their UIs land in milestones 3–4.
- Docs: POLICY, SECURITY_MODEL, EQUATIONS, STYLE, SETUP_YOUTUBE; verified primary-source bibliography; generated visuals; GitHub Pages site with an interactive demo.
