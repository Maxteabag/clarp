#!/usr/bin/env python3
"""Controller tutorial lines and cache inputs from the reference functions."""
import contextlib
import importlib.util
import io
from urllib.parse import quote
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratch as tool_scratch

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'server-rs/crates/clarp-kernel/tests/fixtures/controller-narration-vectors.json'


def generate(directory):
    spec = importlib.util.spec_from_file_location('replay_host', ROOT / 'tests/replay/host.py')
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    (directory / 'scenario-config.toml').write_text('')
    replay.isolate(directory)
    replay.loopback_only()
    sys.path.insert(0, str(ROOT / 'server'))
    from lib import controller_narration as reference
    from lib.routes.helpers import _raw_query_value
    from lib.config import Config

    queries = ['', 'x=missing', 'text', 'text=', 'text=&text=later', 'text=one&text=two',
               't%65xt=encoded', 'Text=other&text=yes', 'text=Press+once', 'text=a%2Bb',
               'text=a%26b%3Dc', 'text=%ff', 'text=%xx', 'text=a=b', 'text=+',
               'text=%20Press%20%20once%20', 'text=%09%0A%00', 'text=' + 'x'*241]
    for code in [0, 9, 10, 13, 28, 32, 127, 160, 173, 0x301, 0x2003, 0x2028,
                 0x200b, 0xe000, 0xffff, 0x1f308, 0xfffd]:
        queries.append('text=' + quote('a' + chr(code) + 'b'))
    for size in [239, 240, 241]:
        queries.append('text=' + quote(chr(0x1f308)*size))
    queries.append('text=' + 'x'*240 + '%09')
    rows = []
    for query in queries:
        text = reference.normalize_text(_raw_query_value('/controller-narration?' + query, 'text'))
        for voice, model in [('fixture-a', 'model-a'), ('fixture-b', 'model-a'),
                             ('fixture-a', 'model-b'), ('', 'model-a')]:
            cfg = Config(cartesia_voices={reference.NARRATION_PERSONA: voice})
            mapped = reference.voice_id(cfg)
            digest = reference.cache_key(text=text, voice=mapped, model=model) if text and mapped else None
            rows.append([query, voice, model, text or None, mapped, digest])
    return rows


if __name__ == '__main__':
    if '--isolated' in sys.argv:
        with tool_scratch.directory() as temp, contextlib.redirect_stderr(io.StringIO()):
            print(json.dumps(generate(Path(temp)), ensure_ascii=True))
    else:
        result = subprocess.check_output([sys.executable, '-I', __file__, '--isolated'],
                                         env=tool_scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}), text=True)
        cases = json.loads(result.splitlines()[-1])
        OUT.write_text('[\n' + ',\n'.join(json.dumps(c) for c in cases) + '\n]\n')
