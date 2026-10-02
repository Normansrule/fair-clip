# Security policy

## Reporting a vulnerability

Please **don't** open a public issue for security problems. Use GitHub's private vulnerability reporting instead: this repository → **Security** → **Report a vulnerability**. Include what you found, how to reproduce it, and the impact you expect. You'll get an acknowledgement within 7 days, and a fix or a mitigation plan within 30 days for confirmed issues. Credit is given in the release notes unless you'd rather stay anonymous.

Especially welcome: sandbox escapes or SSRF through crafted media; prompt-injection paths that get links, claims or flags past the compliance gate; ways to publish without approval, or to change a clip or its metadata after approval; token leakage; dashboard authentication, CSRF or XSS bypasses.

## Supported versions

Only the latest release gets fixes during the 0.x series.

## What we already do

See [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md) for the threat model and mitigations. Releases are built by CI from version tags and published with SHA-256 checksums and a build-provenance attestation. Verify them with `sha256sum -c SHA256SUMS` and `gh attestation verify <file> -R <owner>/fair-clip`.
