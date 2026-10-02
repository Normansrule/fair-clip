# Visual style

Every visual in this repo (diagrams, charts, the dashboard, the overlays burned into clips) follows this page. The colours live in one file, [`assets/src/palette.json`](../assets/src/palette.json), and `scripts/render_assets.py` reads them from there, so a colour change happens in one place.

## Colours mean things, and the meaning never changes

| Role | Light | Dark | Used for | Never used for |
|---|---|---|---|---|
| Ink | `#0B1020` | `#E8ECF4` | text, primary lines | |
| Muted | `#5B6478` | `#9AA4B8` | secondary text, connectors | anything you must notice |
| **Flow / value-add (blue)** | `#2563EB` | `#6EA0FF` | data moving through the pipeline, value-add cards, the "clip" token | warnings |
| **Highlight (amber)** | `#9A5B00` text · `#FFB000` fill | `#FFB000` | highlight scores, the spoken word in captions | blocks |
| **Gate / hard stop (red)** | `#C81E1E` | `#F87171` | blocks, hard stops (■), untrusted input | decoration |
| **Pass / approved (green)** | `#15803D` | `#4ADE80` | passed checks, approved | |
| **Disclosure (yellow)** | `#FDE047` | `#FDE047` | affiliate and paid-promotion disclosures, *only* | anything else, so viewers learn "yellow = someone is paid" |
| **Sandbox (violet)** | `#7C3AED` | `#A78BFA` | the media isolation boundary | |
| **Google (teal)** | `#0F766E` | `#2DD4BF` | anything that talks to Google | |

**Colour-blind safety:** the main contrast pair is blue against amber, which stays distinct under the common forms of colour blindness. Red and green never carry meaning alone. They always come with a symbol: ✓ pass, ⚠ warning, ✕ block, ■ hard stop. Text meets Web Content Accessibility Guidelines (WCAG) AA contrast (4.5:1). We checked every pair with the WCAG formula. That is why amber *text* is the darker `#9A5B00` (5.1:1), and why filled badges and buttons switch to ink-coloured text in dark mode (the light dark-mode fills would give white text only ~2.6:1).

## Type

- **DejaVu Sans** (bundled in `src/fairclip/data/fonts/`, Bitstream Vera licence) for everything burned into video and for diagrams. Bundling it means a clip renders identically on every machine and inside the sandbox.
- The dashboard uses the system UI font stack, so it loads no remote fonts and the Content Security Policy (CSP) stays strict.
- Code, hashes and reason codes use a monospace font (`DejaVu Sans Mono`).

## Diagrams

- Boxes have rounded corners (radius 10 to 16 px). A **hard stop** box has a thick red border, a red ■, and the words "HARD STOP".
- Solid grey arrows show normal flow. Dashed red arrows show untrusted input. A dashed violet outline marks an isolation boundary. Dashed blue arrows show feedback loops that need human approval.
- Every diagram ships in a light and a dark variant (`*-light.svg`, `*-dark.svg`), switched in the README with `<picture>` and `prefers-color-scheme`.

## Animation

- One idea per animation, a 3 to 10 second loop with ease-in-out motion, holding on the key frame. The pipeline token pauses longest at the two hard stops.
- Every animated SVG contains `@media (prefers-reduced-motion: reduce)`, which turns the motion off and leaves a sensible still frame.
- GIFs stay under 5 MB and SVGs under 500 KB. `render_assets.py --check` fails CI if either limit is exceeded.

## On-video overlays (the clip itself)

| Element | Position (9:16) | Style |
|---|---|---|
| Hook (verbatim quote, first 2 s) | top band | ink on white, bold |
| Value-add card + label (CONTEXT / MY TAKE / ANALYSIS / EXPLAINER) | top band | white on blue |
| Disclosure band | fixed, just below the picture (y ≈ 1300 of 1920) | ink on yellow, bold. It is always in the same place, so viewers learn where to look |
| Captions | lower band (y ≈ 1500) | white with a dark outline; the spoken word in amber, scaled to 108 % |
| Lower third | bottom left, after the hook | white on ink |
| Attribution | bottom, whole clip | small white on ink |

## Figures and captions

Every figure gets alt text, a numbered caption that states the takeaway, and a credit line when it is based on outside data or work. Illustrative (made-up) data is always labelled as illustrative, both in the figure and in the dashboard.
