# assets/

The persona PNGs in this folder are **not** hand-drawn. They were rendered
from [**Open Mascot Studio**](https://github.com/0oooh/open-mascot-studio),
a Grok-bot-style vector mascot engine licensed under the **MIT License**
(see `LICENSE-open-mascot-studio.txt`, Copyright (c) 2026
[0oooh](https://github.com/0oooh)).

| file | what it is |
| --- | --- |
| `persona.png` | the default sprite: `curious` expression, `soft` blob, `#12161d`, oval eyes, no stage, baked with a soft glow so it reads on dark and light desktops |
| `persona-focused.png` | `focused` — leaning in, used while **connecting** |
| `persona-beaming.png` | `beaming` — used while the sandbox is **busy** |
| `persona-uneasy.png` | `uneasy` — used while **reconnecting** |
| `persona-surprised.png` | `surprised` — used on a hard **error** |
| `persona-sleepy.png` | `sleepy` — used once nothing has happened for a while |
| `persona-confident.png` | `confident` — used for a plain **ssh** session |
| `persona-faces.png` | labelled contact sheet of every state face, for picking a different look |
| `persona-options.png` | contact sheet of six expression/shape/eye/colour variants |
| `LICENSE-open-mascot-studio.txt` | upstream MIT license (required attribution) |

`buff_popup.py` loads these and derives the breathing frames from them at
runtime — no Pillow, no SVG renderer, no network. It builds only the face it
needs and warms the rest in the background, so a mood change is instant.

Which state shows which face is the single function `buff_popup.face_for()`;
the table above is that function's result, not a second copy of it.

## How these were rendered

The bake script lives outside the repo (it needs Chrome and a local http
server for ESM imports); the recipe is:

```bash
# 1. the mascot source
curl -sL -o oms.tar.gz \
  https://codeload.github.com/0oooh/open-mascot-studio/tar.gz/refs/heads/main
tar xzf oms.tar.gz            # -> open-mascot-studio-main
python -m http.server 8799 &  # ESM needs http:// (file:// would break imports)

# 2. one expression, transparent, at 2x for a clean downsample
chrome --headless=new --disable-gpu --hide-scrollbars --no-sandbox \
  --window-size=1120,1120 --default-background-color=00000000 \
  --virtual-time-budget=6000 --screenshot=raw.png \
  "http://127.0.0.1:8799/bake.html?expr=curious&px=1120"

# 3. composite that render over the shipped persona.png (see below)
```

`bake.html` imports `createDefinition` / `sampleExpression` from the package,
builds the scene, clears the stage fill (transparent background) and drops the
ground shadow so the sprite can float.

**Why the composite step matters.** The glow behind the sprite is a blurred,
tinted copy of the blob silhouette. Only the *expression* changes between
faces — the body is identical — so rather than guessing at a blur radius and
tint, each new face is rendered with a transparent background and then
composited over the already-shipped `persona.png`. The result has a
pixel-identical halo to the original, by construction. (It was checked: re-
baking `curious` this way and comparing it to `persona.png` gives an interior
difference of **0.3/255** with the eyes landing in **exactly** the same pixels.)

To swap a face, change `?expr=` / `?shape=` / `?color=` / `?eye=` and re-run
the commands above (see `persona-faces.png` and `persona-options.png`).