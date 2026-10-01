# Contributing

Thanks for helping. A few ground rules keep this project trustworthy:

1. **Run `make check`** before opening a pull request (lint, tests, asset staleness, bibliography).
2. **New gate rules** go in `src/clipwarden/gate/reasons.py` **and** get a trigger in `tests/test_gate.py::TRIGGERS`. The test fails if a reason can't be reached. Then run `make assets` so the flowchart updates.
3. **New factual claims** in the docs need a primary source in `docs/references.json`, cited as a Markdown link whose text is the key and whose target is `REFERENCES.md#<key>` (from `docs/`). Run `python scripts/check_refs.py --write`. Never cite something you haven't opened.
4. **New equations** follow the pattern in `docs/EQUATIONS.md` and get a worked example in `tests/test_equations.py`.
5. **Visuals** are generated, never hand-edited: change `scripts/render_assets.py` or `scripts/render_media.py` and follow `docs/STYLE.md`.
6. **Security-relevant changes** (sandbox, auth, vault, gate, publisher) need a test that would have failed before the change.
7. **No features that fabricate** reviews, testimonials, results, prices, scarcity or endorsements, and none that process content without a rights record. Pull requests adding them will be closed.

By contributing you agree that your contribution is licensed under Apache-2.0.
