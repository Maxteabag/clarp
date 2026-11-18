#!/usr/bin/env python3
"""Wrong rules for a shim stopping its tool and everything in the same group."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import runner

MAIN = 'clarp-shim/src/main.rs'
RUNS = {'stop': ['test', '--test', 'shim', '--', 'a_direct_child_can_abort_before_its_group_is_terminated']}
PLANTS = [
    (MAIN, 'stop', 'the direct-child signal reaches the entire group',
     'let _ = kill(pid, early.signal);', 'let _ = killpg(pid, early.signal);'),
]


def built_size():
    """Use the workspace build the plant workflow already made, before modifying any code."""
    binary = Path(__file__).resolve().parents[2] / 'target/debug/clarp-shim'
    with tempfile.TemporaryDirectory() as temp:
        stripped = Path(temp) / 'clarp-shim'
        shutil.copyfile(binary, stripped)
        subprocess.run(['strip', '--strip-all', stripped], check=True)
        print('shim bytes: workspace debug', binary.stat().st_size,
              'stripped', stripped.stat().st_size,
              subprocess.check_output(['rustc', '-V'], text=True).strip(), flush=True)


if __name__ == '__main__':
    if os.environ.get('GITHUB_ACTIONS') and '--shard' in sys.argv:
        if sys.argv[sys.argv.index('--shard') + 1].startswith('0/'):
            built_size()
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
