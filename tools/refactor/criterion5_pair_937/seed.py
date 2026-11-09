"""Run one existing synthetic replay text-turn to create the common state DB."""
import argparse
import json
from pathlib import Path
import sys

p = argparse.ArgumentParser()
p.add_argument('--harness', required=True)
p.add_argument('--source', required=True)
p.add_argument('--output', required=True)
a = p.parse_args()
sys.path.insert(0, str(Path(a.harness) / 'tests/replay'))
from capture import capture
from scenarios import BY_ID

scenario = dict(BY_ID['text-turn'])
scenario['config'] = '[tts]\nprovider = "none"\n[whisper]\nenabled = false\n[network]\nadvertise_lan = false\n'
result = capture(Path(a.source), scenario, Path(a.output), python=sys.executable)
Path(a.output + '.seed.json').write_text(json.dumps(result, default=str))
if result['error']:
    raise SystemExit('synthetic replay seed invalid: ' + result['error'])
