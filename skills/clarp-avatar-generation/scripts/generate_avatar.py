#!/usr/bin/env python3
"""Generate, crop, and downsample Clarp agent avatars using OpenAI gpt-image-2.

Standardized pipeline:
1. Native 1024x1024 generation with photorealistic/archetype prompt structure.
2. Center square crop.
3. Lanczos downsampling to 512x512 (detail/review) and 256x256 (PWA UI).
4. Optional static asset installation into clarp/static/avatars/<slug>.png.
"""
import argparse
import base64
import os
import pathlib
import re
import shutil
import sys
import time
from PIL import Image

def get_openai_key():
    if os.environ.get("OPENAI_API_KEY"):
        return os.environ["OPENAI_API_KEY"]
    bashrc = pathlib.Path.home() / ".bashrc"
    if bashrc.exists():
        m = re.search(r'export OPENAI_API_KEY=[\'"]([^\'"]+)[\'"]', bashrc.read_text())
        if m:
            os.environ["OPENAI_API_KEY"] = m.group(1)
            return m.group(1)
    return None

TIER_TEMPLATES = {
    "claude": (
        "A photorealistic candid 35mm photograph of {name}, a thoughtful, intelligent human developer. "
        "Captured at a dynamic three-quarter candid angle in a warm minimalist sunlit architectural studio with natural bokeh. "
        "Authentic human facial anatomy and realistic skin texture, wearing clean minimalist clothing with a subtle terracotta Claude asterisk starburst insignia. "
        "Natural cinematic lighting, authentic shallow depth of field, 8k photograph, no CGI plastic sheen, no text, no borders."
    ),
    "codex": (
        "A photorealistic candid 35mm photograph of {name}, a sleek aesthetic humanoid android. "
        "Captured at a dynamic three-quarter candid angle in a modern high-tech workspace with soft ambient bokeh. "
        "His/her face has realistic skin texture combined with distinct cybernetic android engineering: delicate micro-seams along the jaw and hairline, "
        "striking glowing synthetic optics with subtle slit or ring illumination, acoustic telemetry ear vanes, and an articulated composite armor collar. "
        "No green matrix digital rain, no animal ears, natural cinematic lighting, authentic depth of field, 8k photograph, no CGI plastic sheen, no text, no borders."
    ),
    "grok": (
        "Graphic novel comic book illustration portrait of {name}, an edgy cyberpunk rebel hacker with attitude. "
        "Messy hair with electric neon highlights, sharp cocky grin, dynamic ink shading, heavy graphic novel comic style, "
        "wearing a distressed leather motorcycle jacket with a metallic slash insignia, centered square headshot on dark cyberpunk city background with neon bokeh, no text, no borders."
    ),
    "gemini": (
        "A stunning stylized digital art portrait of {name}, an ethereal cosmic celestial sage. "
        "Captured at a candid three-quarter angle in a starry twilight nebula realm with soft luminous starlight bokeh. "
        "Deep cosmic indigo skin, glowing ethereal eyes, holding a brilliantly glowing four-pointed Gemini spark star that radiates crystalline violet and cyan light. "
        "Clean artistic mastery, majestic celestial lighting, centered square composition, no text, no borders."
    ),
    "janitor": (
        "Detailed 3D character render of {name}, a classic vintage industrial cleanup bot. "
        "Built from heavy riveted cast iron and brushed brass with weathered seafoam-turquoise enamel patina and light grease smudges. "
        "Features twin warm glowing amber circular vacuum-tube eyes, a small brass analog pressure gauge mounted on its temple, and a sturdy riveted neck collar. "
        "Friendly utilitarian demeanor, authentic weathered metal textures, soft warm workshop lighting bokeh, centered square headshot, no text, no borders."
    ),
    "clarp": (
        "A photorealistic candid 35mm photograph of {name}, a sleek obsidian and polished carbon-fiber futuristic host android. "
        "Captured at a dynamic three-quarter angle in a dark minimalist tech sanctuary with subtle atmospheric lighting bokeh. "
        "Embedded across its curved obsidian facial plate and sleek cranial chassis is an intricate, luminous emerald-green constellation node graph, "
        "where glowing green fiber lines connect shining cybernetic node spheres in the distinct shape of a celestial crescent 'C' curve, "
        "exactly like a glowing stellar node lattice. Calm, intelligent, loyal synthetic optics softly glowing with emerald light. "
        "High-end aerospace carbon craftsmanship, realistic reflections, cinematic lighting, 8k photograph, centered square composition, no text, no borders."
    ),
}

def generate_avatar(name: str, tier: str = "codex", custom_prompt: str = "",
                    model: str = "gpt-image-2", out_dir: pathlib.Path = None,
                    copy_to_static: bool = False, static_dir: pathlib.Path = None) -> bool:
    slug = re.sub(r'[^a-z0-9]+', '-', name.strip().lower()).strip('-')
    if not slug:
        print("Error: Invalid name", file=sys.stderr)
        return False

    api_key = get_openai_key()
    if not api_key:
        print("Error: OPENAI_API_KEY not found in environment or ~/.bashrc", file=sys.stderr)
        return False

    from openai import OpenAI
    client = OpenAI(api_key=api_key)

    if custom_prompt:
        prompt = custom_prompt
    elif tier.lower() in TIER_TEMPLATES:
        prompt = TIER_TEMPLATES[tier.lower()].format(name=name)
    else:
        prompt = TIER_TEMPLATES["codex"].format(name=name)

    print(f"[{name}] Generating avatar using {model} (tier: {tier})...")
    t0 = time.time()
    try:
        resp = client.images.generate(model=model, prompt=prompt, n=1)
        b64 = resp.data[0].b64_json
        raw_bytes = base64.b64decode(b64)
    except Exception as exc:
        print(f"[{name}] Generation failed: {exc}", file=sys.stderr)
        return False

    dur = time.time() - t0
    print(f"[{name}] Generated in {dur:.1f}s. Processing image...")

    out_dir = out_dir or pathlib.Path("/home/peter/.cache/clarp/avatar-staging")
    raw_dir = out_dir / "raw"
    dir_512 = out_dir / "512"
    dir_256 = out_dir / "256"

    for d in (raw_dir, dir_512, dir_256):
        d.mkdir(parents=True, exist_ok=True)

    raw_path = raw_dir / f"{slug}.png"
    raw_path.write_bytes(raw_bytes)

    # Center crop to exact 1:1 square
    im = Image.open(raw_path)
    w, h = im.size
    min_dim = min(w, h)
    left = (w - min_dim) // 2
    top = (h - min_dim) // 2
    cropped = im.crop((left, top, left + min_dim, top + min_dim))

    # Lanczos downsampling
    p512 = dir_512 / f"{slug}.png"
    im_512 = cropped.resize((512, 512), Image.Resampling.LANCZOS)
    im_512.save(p512, "PNG", optimize=True)

    p256 = dir_256 / f"{slug}.png"
    im_256 = cropped.resize((256, 256), Image.Resampling.LANCZOS)
    im_256.save(p256, "PNG", optimize=True)

    print(f"[{name}] Saved 512x512 -> {p512}")
    print(f"[{name}] Saved 256x256 -> {p256}")

    if copy_to_static:
        static_dir = static_dir or pathlib.Path("/home/peter/GIT/clarp/static/avatars")
        static_dir.mkdir(parents=True, exist_ok=True)
        dest = static_dir / f"{slug}.png"
        shutil.copy2(p512, dest)
        print(f"[{name}] Copied to static assets -> {dest}")

    return True

def main():
    parser = argparse.ArgumentParser(description="Generate and downsample Clarp agent avatars.")
    parser.add_argument("--name", required=True, help="Character or contact name")
    parser.add_argument("--tier", default="codex", choices=["claude", "codex", "grok", "gemini", "janitor", "clarp"],
                        help="Character archetype tier")
    parser.add_argument("--prompt", default="", help="Custom prompt override")
    parser.add_argument("--model", default="gpt-image-2", help="OpenAI image generation model")
    parser.add_argument("--out-dir", type=pathlib.Path, default=pathlib.Path("/home/peter/.cache/clarp/avatar-staging"),
                        help="Output staging directory")
    parser.add_argument("--save-static", action="store_true", help="Copy 512px output to clarp/static/avatars/")
    parser.add_argument("--static-dir", type=pathlib.Path, default=pathlib.Path("/home/peter/GIT/clarp/static/avatars"),
                        help="Clarp static avatars directory")

    args = parser.parse_args()
    success = generate_avatar(
        name=args.name,
        tier=args.tier,
        custom_prompt=args.prompt,
        model=args.model,
        out_dir=args.out_dir,
        copy_to_static=args.save_static,
        static_dir=args.static_dir,
    )
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()
