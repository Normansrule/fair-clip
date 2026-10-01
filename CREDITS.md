# Credits and third-party material

| Item | Source | License | Used for |
|---|---|---|---|
| DejaVu Sans, DejaVu Sans Bold | DejaVu fonts project (bundled from Debian's `fonts-dejavu-core`) | Bitstream Vera Fonts license + public-domain changes; see `src/clipwarden/data/fonts/LICENSE-DejaVu.txt` | captions/cards burned into clips, figures |
| Sample talk *Where does the weight go?* (`examples/demo/`) | made for this repo by `scripts/make_sample.py` (Pillow drawings, espeak-ng voice) | CC0-1.0 (`examples/demo/LICENSE-SAMPLE.txt`) | demo, tests, README visuals |
| FFmpeg / FFprobe | ffmpeg.org (installed in the sandbox image, not redistributed in this repo) | LGPL/GPL (per build) | media processing |

Visual-style inspiration (ideas only, no code or assets copied): Distill's article template, Manim's one-idea-per-animation approach, Excalidraw's diagram readability, d2l.ai's interleaving of prose, equations and code.

Libraries are listed with exact versions and hashes in `requirements*.lock`. Their licenses ship with each installed package.
