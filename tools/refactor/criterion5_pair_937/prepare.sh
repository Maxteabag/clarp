#!/usr/bin/env bash
# SOURCE ONLY. Called only by the exact reviewed dispatch workflow after handoff.
set -euo pipefail
work=$(pwd -P)
export C937_WORKSPACE="$work"
python - <<'PY'
import json,os,pathlib,re,subprocess
root=pathlib.Path(os.environ['C937_WORKSPACE']);plan=json.loads((root/'tools/refactor/criterion5_pair_937/plan.json').read_text())
driver=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
if os.environ.get('GITHUB_REF_NAME')!='ci/criterion5-937-'+driver[:12]:raise SystemExit('immutable CI branch must encode exact reviewed driver commit')
for arm in ('A','B'):
 if not re.fullmatch('[0-9a-f]{40}',plan[arm]):raise SystemExit('exact40char pins required')
remote=subprocess.check_output(['git','ls-remote','https://github.com/Maxteabag/clarp.git','refs/heads/main'],text=True).split()[0]
if remote!=plan['B']:raise SystemExit('reviewed B is no longer main: captain must rebind exact pin/review; no autonomous retarget')
(root/'measurement-source.json').write_text(json.dumps({'driver_sha':driver,'A':plan['A'],'B':plan['B'],'main_at_setup':remote,'run_id':os.environ.get('GITHUB_RUN_ID'),'run_attempt':os.environ.get('GITHUB_RUN_ATTEMPT')},indent=2)+'\n')
(root/'measurement-arms').mkdir()
for arm in ('A','B'):
 subprocess.run(['git','fetch','--no-tags','--depth=1','https://github.com/Maxteabag/clarp.git',plan[arm]],check=True,timeout=120)
 dest=root/'measurement-arms'/arm;dest.mkdir()
 with (root/('measurement-'+arm+'.tar')).open('wb') as f:subprocess.run(['git','archive',plan[arm]],stdout=f,check=True)
 subprocess.run(['tar','-xf',str(root/('measurement-'+arm+'.tar')),'-C',str(dest)],check=True)
PY
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
