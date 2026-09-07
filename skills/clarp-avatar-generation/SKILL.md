---
name: clarp-avatar-generation
description: Prompt formulas, negative constraints, photography standards, and downsampling pipelines for generating Clarp agent and contact avatars.
---

# Clarp Avatar Generation

Standardized procedure for generating, refining, and downsampling persona avatars across all Clarp fleet tiers (Claude, Codex, Grok, Gemini, Janitors, and the Clarp host companion).

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

Use this baseline structure when generating portraits with OpenAI `gpt-image-2`:

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
