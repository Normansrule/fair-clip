# Value-add templates

Every clip needs original value on screen (see [POLICY.md](POLICY.md) and [EQUATIONS.md §5](EQUATIONS.md#5-value-add-coverage-and-originality)). The plain **card** is a label plus your sentence. The five templates below add structure and motion. They **never replace your words**: each one still needs `text`, your own sentence (at least 8 words across the clip's value-add, at most 140 characters with a template), and that sentence closes every panel.

<img src="../assets/templates/gallery.png" alt="All five templates over the sample talk: key number, definition, steps, compare, bar." width="100%">

| Template | Use it for | Data | Animation |
|---|---|---|---|
| `key_number` | one figure worth remembering | `headline` (≤ 14 characters, must contain a digit, e.g. `"853 m"`), optional `unit` label | counts up, then settles on your exact text |
| `definition` | a term the clip uses | `headline` = the term (≤ 24 characters) | underline draws in |
| `steps` | a short process | `items`: 2–5 steps (≤ 60 characters each); needs ≥ 1.2 s per step | steps appear one by one |
| `compare` | before/after, A vs B | `left_title`, `right_title` (≤ 20 characters), `left`, `right` (1–4 items, ≤ 28 characters each) | right column follows the left |
| `bar` | relative sizes | `bars`: 2–5 `{label, value}`, optional `unit` | bars grow to their values |

## How to write one

In your plan file, add `template` and `data` to a value-add element. Times are on the **clip's** timeline (after jump cuts).

```json
{
  "kind": "analysis",
  "template": "compare",
  "start": 12.0,
  "end": 21.0,
  "text": "Same wind, different deck: depth and stiffness changed after 1940, not the strength of the steel.",
  "data": {
    "left_title": "1940 deck",  "left":  ["thin", "flexible", "fluttered"],
    "right_title": "Modern deck", "right": ["deep", "stiff", "wind-tunnel tested"]
  }
}
```

Copy-ready examples of all five live in [`src/clipwarden/media/templates.py`](../src/clipwarden/media/templates.py) (`EXAMPLES`). Preview them over the sample talk with:

```bash
python scripts/render_templates.py            # writes assets/templates/*.png and *.gif
python scripts/render_templates.py --only bar
```

## What the gate checks on templates

Templates get no shortcuts. `ValueAddElement.visible_text()` gathers **every word and number a viewer can read** (your sentence, headline, steps, column items, bar labels and values) and hands it to the same checks as any card:

- **Links:** "evil dot com" inside a step is still a link, and it must be on your allowlist.
- **Markets clips:** a figure like `"37% growth"` in a key number needs a cited, fresh data source, like any number in your text.
- **Screening:** the blocked and review word lists apply.
- **Grounding:** template words count as "said in the clip" for title grounding, because they are on screen.
- **Hidden characters:** right-to-left overrides and zero-width characters are rejected when the plan is loaded.

Coverage (≥ 25 % of the clip) and originality (< 60 % copied phrases) are measured on `text` only. Data labels are short by design, so they can't pad coverage.

## Layout and accessibility

- **9:16:** panels sit in the top band (y ≈ 96–644 of 1920), above the source frame, so the original is never covered. Captions stay in their own band near the bottom, and disclosures keep their reserved yellow band.
- **16:9:** a compact panel on the right third.
- Colors follow [STYLE.md](STYLE.md): value-add blue on near-black at about 90 % opacity, white text. Disclosure yellow is never used by templates.
- Motion is short (≤ 0.6 s per element) and stops. The final state holds for the rest of the element's time.

## Limits

- Text isn't measured in pixels. Each line is shrunk to fit its width (down to a 14 px floor), and the field limits above guarantee it fits at that floor. A test renders every template with every field at its maximum length and checks that nothing leaves the panel. Preview with the script above anyway: the smallest sizes are legible, but not comfortable.
- Bars are drawn to scale against the largest value. They are not a charting library: no axes or negative values.
