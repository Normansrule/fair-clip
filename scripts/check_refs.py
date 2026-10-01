#!/usr/bin/env python3
"""Bibliography hygiene (offline). Network link checking runs weekly in .github/workflows/links.yml.

    python scripts/check_refs.py           # validate (CI)
    python scripts/check_refs.py --write   # also regenerate docs/REFERENCES.md from docs/references.json

Rules: every entry has id/type/title/https URL/accessed date; every citation key used in the docs,
written as [key](REFERENCES.md#key), exists; every entry is cited somewhere.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REFS = ROOT / "docs" / "references.json"
OUT = ROOT / "docs" / "REFERENCES.md"
CITE = re.compile(r"\[([a-z0-9-]+)\]\((?:docs/)?REFERENCES\.md#([a-z0-9-]+)\)")


def render(refs: list[dict]) -> str:
    lines = ["# References", "", "Generated from `docs/references.json` by `scripts/check_refs.py --write`. Every entry was opened and",
             "checked against the claim it supports on the access date shown. Primary sources only (official documentation,",
             "regulators, statutes). Notes paraphrase what each source says; quotes are short and attributed.", ""]
    for r in sorted(refs, key=lambda x: x["id"]):
        who = ", ".join(a.get("literal", "") for a in r.get("author", []))
        acc = "-".join(f"{p:02d}" for p in r["accessed"]["date-parts"][0])
        issued = f" ({'-'.join(f'{p:02d}' for p in r['issued']['date-parts'][0])})" if r.get("issued") else ""
        lines += [f'<a id="{r["id"]}"></a>', f"**[{r['id']}]** {who}. *{r['title']}*{issued}. <{r['URL']}> (accessed {acc}).", "",
                  f"> {r.get('note', '')}", ""]
    return "\n".join(lines)


def main() -> int:
    refs = json.loads(REFS.read_text())
    errors = []
    ids = set()
    for r in refs:
        for k in ("id", "type", "title", "URL", "accessed"):
            if k not in r:
                errors.append(f"{r.get('id', '?')}: missing {k}")
        if not str(r.get("URL", "")).startswith("https://"):
            errors.append(f"{r.get('id')}: URL must be https")
        if r.get("id") in ids:
            errors.append(f"duplicate id {r.get('id')}")
        ids.add(r.get("id"))
    cited = set()
    for md in [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]:
        if md.name == "REFERENCES.md" or not md.exists():
            continue
        for key, anchor in CITE.findall(md.read_text()):
            if key != anchor:
                errors.append(f"{md.name}: citation [{key}] points at #{anchor}")
            if key not in ids:
                errors.append(f"{md.name}: unknown citation [{key}]")
            cited.add(key)
    for unused in sorted(ids - cited):
        errors.append(f"reference '{unused}' is never cited")
    if "--write" in sys.argv:
        OUT.write_text(render(refs))
    elif OUT.exists() and OUT.read_text() != render(refs):
        errors.append("docs/REFERENCES.md is stale: run scripts/check_refs.py --write")
    if errors:
        print("bibliography problems:\n  " + "\n  ".join(errors))
        return 1
    print(f"{len(refs)} references OK, all cited")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
