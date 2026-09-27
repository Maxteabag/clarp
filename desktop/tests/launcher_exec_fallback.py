#!/usr/bin/env python3
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    if len(sys.argv) != 3:
        print("usage: launcher_exec_fallback.py LAUNCHER REAL_PROBE", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        launcher = root / "clarp-desktop"
        real = root / "clarp-desktop-real"
        shutil.copy2(sys.argv[1], launcher)
        shutil.copy2(sys.argv[2], real)
        launcher.chmod(0o755)
        real.chmod(0o755)
        runtime = root / "runtime"
        runtime.mkdir()
        env = dict(os.environ)
        env["XDG_RUNTIME_DIR"] = str(runtime)
        env["CLARP_INSTANCE_NAME"] = "launcher-fallback-test"
        result = subprocess.run(
            [str(launcher), "--sentinel", "with space"],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            print(result.stderr, file=sys.stderr)
            return result.returncode
        expected = "real-probe argc=3 argv0=clarp-desktop-real argv1=--sentinel argv2=with space"
        if result.stdout.strip() != expected:
            print(f"unexpected stdout: {result.stdout!r}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
