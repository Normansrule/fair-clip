# Changelog

## 0.1.0 (milestone 1: MVP)

- Ten-stage pipeline with dashboard status and logs: rights intake (hard stop), transcription (sidecar or faster-whisper), highlight scoring (Eq. 1), editing (9:16 reframe, jump cuts, hook, word captions, lower third), required value-add layer, grounded metadata, compliance gate (48 reasons: 41 block, 7 warn), human approval, dry-run/live publishing, analytics import and bounded weight proposals.
- Security: network-less FFmpeg sandbox (per-job Docker or an isolated worker container), single `youtube.upload` scope with PKCE, keychain/AES-GCM token vault, log redaction, hash-chained append-only audit log, kill switch, approval bound to file and request hashes, localhost dashboard with CSRF/CSP/Host checks.
- Affiliate and markets gate rules (disclosure placement, allowlisted links only, no fabricated claims, §17(b) payment disclosure, sourced and fresh numbers, pump-pattern block). Their UIs land in milestones 3–4.
- Docs: POLICY, SECURITY_MODEL, EQUATIONS, STYLE, SETUP_YOUTUBE; verified primary-source bibliography; generated visuals; GitHub Pages site with an interactive demo.
