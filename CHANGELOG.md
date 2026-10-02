# Changelog

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
