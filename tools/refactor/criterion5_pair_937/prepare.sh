#!/usr/bin/env bash
# SOURCE ONLY. Called only by the exact reviewed dispatch workflow after handoff.
set -euo pipefail
work=$(pwd -P)
export C937_WORKSPACE="$work"
python tools/refactor/criterion5_pair_937/stage_sources.py --workspace "$work"
# One shared Python/Node/dependency environment, based on reviewed B's lock.
export UV_PROJECT_ENVIRONMENT="$work/.venv"
uv python install 3.12.14
uv sync --project "$work/measurement-arms/B" --python 3.12.14 --frozen --group dev
npm --prefix "$work/measurement-arms/B" ci --ignore-scripts --no-audit --no-fund
ln -s "$work/measurement-arms/B/node_modules" "$work/measurement-arms/A/node_modules"
npm --prefix "$work/measurement-arms/A" run build
npm --prefix "$work/measurement-arms/B" run build
# Ubuntu's image normally provides ffmpeg; refusal avoids unbudgeted installers.
command -v ffmpeg
command -v ffprobe
