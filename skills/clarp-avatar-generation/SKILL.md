---
name: clarp-avatar-generation
description: Prompt formulas, negative constraints, photography standards, and downsampling pipelines for generating Clarp agent and contact avatars.
---

# Clarp Avatar Generation

Standardized procedure for generating, refining, and downsampling persona avatars across all Clarp fleet tiers (Claude, Codex, Grok, Gemini, DeepSeek, Janitors, and the Clarp host companion).

## 1. Archetype Aesthetic Formulas

Every Clarp tier adheres to a distinct visual archetype with strict styling rules:

### Claude Tier (Humans)
- **Style**: Authentic candid 35mm photograph, natural studio portraiture.
- **Environment**: Warm minimalist architectural design studio with cedar, linen, and soft daylight bokeh.
- **Attire & Branding**: Clean minimalist cream, charcoal, or beige apparel subtly featuring the iconic warm terracotta Claude asterisk starburst insignia.
- **Anatomy**: Realistic human bone structure, natural skin texture, freckles, lifelike eyes.

### Codex Tier (Androids)
- **Style**: High-fidelity candid 35mm photograph, cinematic depth of field.
- **Framing**: Dynamic three-quarter angle in contextual workspaces (sunlit glass terraces, recording studios, electronics benches, modern architectural atriums).
- **Android Craftsmanship**: Authentic human facial contours combined with distinctive cybernetic detailing:
  - Delicate hairline and jawline seams with faint internal glow.
  - Synthetic optics with subtle slit pupils or concentric optical ring illumination.
  - Integrated acoustic telemetry vanes or ear audio modules.
  - Articulated composite neck and shoulder armor collars.
- **Negative Constraints**:
  - NO green matrix digital rain or falling code.
  - NO anime or furry animal ears (ears are acoustic telemetry vanes or human ears).
  - NO plastic CGI sheen or airbrushed video game look.

### Grok Tier (Cyberpunk Rebels)
- **Style**: Bold graphic novel comic book illustration with dynamic ink linework and crosshatching.
- **Attire & Demeanor**: Edgy cyberpunk rebel hackers in distressed black leather motorcycle jackets, aviator shades, messy hair with neon highlights, and the metallic slash emblem on the collar or lapel.
- **Environment**: Moody dark cyberpunk city background with soft neon bokeh.

### Gemini Tier (Cosmic Sages & Minimalist Creatures)
- **Style**: Stylized digital art portrait or clean vector illustration.
- **Archetype**: Celestial indigo genie / cosmic sage holding a brilliantly glowing four-pointed Gemini spark star emitting crystalline violet and cyan luminescence against nebula starlight.

### DeepSeek Tier (Deep-Sea Explorers)
- **Style**: Cinematic underwater photograph, photorealistic, cold electric-blue palette against black water.
- **Archetype**: Submersible pilots, marine biologists, salvage divers and abyssal researchers in matte charcoal technical dive suits with a small silver whale insignia on the collar.
- **Lighting**: Bioluminescent blue light from below on wet skin, drifting plankton particles, the huge dark silhouette of a whale passing far behind.
- **Negative Constraints**: NO goggles or masks covering the eyes, NO CGI plastic sheen, NO scuba regulator in the mouth.
- **Backend**: the `deepseek` card (OpenCode binary, DeepSeek models only); tier id `deepseek`.

### Janitor Tier (Maintenance Droids)
- **Style**: Detailed 3D character render of vintage utilitarian industrial robots.
- **Materials**: Weathered riveted iron, brushed brass, oxidized seafoam-turquoise enamel patina, light grease smudges.
- **Features**: Dual glowing amber vacuum-tube eyes, analog brass pressure gauge on temple, heavy neck collar with tool mount. Silent utilitarian presence (no speech voice).

### Clarp Companion Robot
- **Inspiration**: Directly derived from the Clarp app icon (`static/icon.png`).
- **Archetype**: Sleek obsidian and polished carbon-fiber futuristic android.
- **Key Feature**: An intricate emerald-green constellation node graph embedded across its curved facial plate, where glowing fiber lines connect glowing node spheres in the distinct shape of a celestial crescent "C" curve.

---

## 2. Universal Prompt Template

Use this baseline structure when generating portraits with OpenAI `gpt-image-2.5-sunburst` (the quality variant; `gpt-image-2.5-flare` is the fast one):

```text
A photorealistic candid 35mm photograph of {Name}, a {Archetype Description}.
Captured at a dynamic three-quarter candid angle in a {Contextual Environment Bokeh}.
His/Her face has {Realistic Facial Anatomy & Skin Texture}, accentuated by distinctive {Tier Details}:
{Specific Cybernetic / Material Seams, Optics, or Insignia}.
Natural cinematic lighting, authentic shallow depth of field, 8k photograph, no CGI plastic sheen, no text, no borders.
```

---

## 3. Image Processing & Downsampling Pipeline

Avatars must be rendered crisp across high-DPI displays, mobile screens, and small avatar badges:

1. **Generation**: Native 1024×1024 square image from `gpt-image-2`.
2. **Square Crop**: Center-crop to exact 1:1 aspect ratio:
   ```python
   min_dim = min(w, h)
   left = (w - min_dim) // 2
   top = (h - min_dim) // 2
   cropped = im.crop((left, top, left + min_dim, top + min_dim))
   ```
3. **Lanczos Downsampling**:
   - `512×512`: High-DPI retina inspection and archive (`DIR_512`). Saved with `Image.Resampling.LANCZOS` and `optimize=True`.
   - `256×256`: Clarp PWA and mobile client delivery (`DIR_256`).
4. **Static Assets**:
   - For built-in roster personas, copy 512px output to `/home/peter/GIT/clarp/static/avatars/<slug>.png`.

---

## 4. Helper Script

A CLI script is provided in `scripts/generate_avatar.py`:

```bash
# Generate an avatar for an existing tier
python3 ~/dotfiles/skills/clarp-avatar-generation/scripts/generate_avatar.py \
  --name "Axel" \
  --tier codex \
  --save-static

# Generate a custom archetype avatar
python3 ~/dotfiles/skills/clarp-avatar-generation/scripts/generate_avatar.py \
  --name "Claude Default" \
  --tier claude \
  --prompt "A photorealistic candid 35mm photograph of..." \
  --save-static
```

---

## 5. Live Review Server

Live avatar review runs on port 7685 via `clarp-avatar-review.service`. The HTML dashboard at `/home/peter/.cache/clarp/avatar-staging/clarp_contacts_redesign_report.html` displays all cards grouped by tier with interactive regeneration buttons and hot cache-busting previews.

---

## 6. Thumbnails a list shows

Only the Rust Host has these, from Host contract 160 (feature `avatar_thumbnails` in
`GET /server-info`); the Python Host has none. There, any picture address an agent or persona
row gives (`avatar_url`, `model_avatar_url`: `/avatars/<agent>?v=`, `/persona-avatars/<id>?v=`,
`/static/avatars/...?v=`) takes `&px=64`, `128` or `256` for a square JPEG of that size; any
other `px`, or a Host without the feature, gives the full picture. No row has a separate field
for it. Publish the full-size portrait as before: the Host makes the thumbnails itself, once per
picture and size. An address whose `v` is the picture's current version may be cached for a
year; a new picture has a new `v`.

---

## 7. Setting a running agent's picture (Rust Host)

An agent's picture comes from its own gallery. Do it for yourself or one of your own helpers,
from your own turn (or a job it started); another agent's picture is the owner's to change:

1. `clarp-media-publish FILE --session <agent-session> --caption "..."` returns
   `clarp-media://asset/<asset_id>`.
2. `clarp-admin portrait add --session <agent-session> --asset <asset_id>` keeps it as an
   alternate (max 3 kept) and prints the portraits with its `portrait_id`.
3. `clarp-admin portrait select --session <agent-session> --portrait <portrait_id>` makes it
   the picture worn.

Both need a Rust Host with the feature `tools_owner_free` (contract 193). They do what
`POST /agent-portraits` does; do not call that route with `[server].auth_token` from
config.toml, which is the owner's token.
4. Verify: `GET /agents/snapshot` shows `avatar_url` `/avatars/<agent_id>?v=...`, and fetching it returns the file you published.

If the `openai` module is missing, run the generator with `uv run -q --with openai python ...`.
The key is in `rbw get env-OPENAI_API_KEY`.
