# Changelog

## 0.2.0 (milestone 2: templates, live analytics, the app)

**Added**
- Five animated value-add templates (`key_number`, `definition`, `steps`, `compare`, `bar`), drawn by the existing escaped ASS renderer above the source frame. Every word and number on them is gated (links, sourced figures, screening, grounding). Guide: [docs/TEMPLATES.md](docs/TEMPLATES.md). The demo now uses `compare` (clip A) and `steps` (clip C).
- Read-only YouTube Analytics pull (`clipwarden analytics connect | pull | disconnect`, or **Pull now** on the dashboard). It uses a separate `yt-analytics.readonly` token and fetches totals plus the retention curve per published clip.
- Weight proposals from live data: `clipwarden weights propose --from-analytics` and **Propose from live analytics** on the Settings page (≥ 10 clips with ≥ 50 views each; still bounded to ±0.05 per weight and still needs your approval). Proposals now show a per-weight before/after table.
- The app: `scripts/install.sh` (checksum- and provenance-verified, no sudo, apps-menu launcher that shows in the Windows Start menu on WSL2), `clipwarden app` (opens the dashboard in your browser), a single-use first-run welcome link to choose the password, and a web-app manifest so Chrome and Edge can install the dashboard as its own window.
- Web demo site: template gallery and a "Get the app" section; works without sideways scrolling at phone width.

**Fixed**
- `httpx` was imported at runtime but only locked as a dev dependency: clean installs and the Docker image would crash on start. It is now a runtime dependency, and a test checks the runtime lock covers every runtime dependency.
- Fonts and the screen-terms list now ship inside the package (`clipwarden/data/`), so an installed copy can't silently run the profanity screen with an empty list; a missing list is now an error. Put your own `screen_terms.txt` in the data folder to override it.
- Security review fixes (first hardening pass): links in any TLD and spoken/obfuscated links ("evil dot com"); hidden and right-to-left characters rejected; paid promotion must be disclosed inside the video; CC BY attribution per §3(a), ShareAlike blocked; rights re-checked for the publish day; uploads from a hash-verified private copy; audit chain safe under concurrent writers; approved clips frozen; metadata edits re-gated; server-side logout. The gate grew from 48 to 52 reasons (43 block, 9 warn).

**Changed**
- Container image tags are now `0.2.0`: rebuild the sandbox with `make sandbox`.

## 0.1.0 (milestone 1: MVP)

- Ten-stage pipeline with dashboard status and logs: rights intake (hard stop), transcription (sidecar or faster-whisper), highlight scoring (Eq. 1), editing (9:16 reframe, jump cuts, hook, word captions, lower third), required value-add layer, grounded metadata, compliance gate (48 reasons: 41 block, 7 warn), human approval, dry-run/live publishing, analytics import and bounded weight proposals.
- Security: network-less FFmpeg sandbox (per-job Docker or an isolated worker container), single `youtube.upload` scope with PKCE, keychain/AES-GCM token vault, log redaction, hash-chained append-only audit log, kill switch, approval bound to file and request hashes, localhost dashboard with CSRF/CSP/Host checks.
- Affiliate and markets gate rules (disclosure placement, allowlisted links only, no fabricated claims, §17(b) payment disclosure, sourced and fresh numbers, pump-pattern block). Their UIs land in milestones 3–4.
- Docs: POLICY, SECURITY_MODEL, EQUATIONS, STYLE, SETUP_YOUTUBE; verified primary-source bibliography; generated visuals; GitHub Pages site with an interactive demo.
