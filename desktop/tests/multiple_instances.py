#!/usr/bin/env python3
"""Run two real windows against an offline fixture and an isolated session bus."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def main():
    with tempfile.TemporaryDirectory(prefix='clarp-multiple-instances-') as temp:
        root = Path(temp)
        env = {k: v for k, v in os.environ.items() if not k.startswith('CLARP_')}
        env.update(QT_QPA_PLATFORM='offscreen', QT_QUICK_BACKEND='software',
                   QT_IM_MODULE='compose', QT_FORCE_STDERR_LOGGING='1',
                   XDG_CONFIG_HOME=str(root / 'config'), XDG_CACHE_HOME=str(root / 'cache'),
                   XDG_DATA_HOME=str(root / 'data'),
                   CLARP_BASE_URL='http://127.0.0.1:1', CLARP_TOKEN='offline-fixture',
                   CLARP_INSTANCE_NAME='clarp-concurrent-test',
                   CLARP_SCREENSHOT_SCENARIO='markdown')
        processes = []
        logs = []
        try:
            for index, delay in enumerate(('12000', '4000')):
                log = (root / f'{index}.log').open('w+')
                logs.append(log)
                child_env = dict(env, CLARP_SCREENSHOT_PATH=str(root / f'{index}.png'),
                                 CLARP_SCREENSHOT_DELAY_MS=delay)
                processes.append(subprocess.Popen([sys.argv[1]], env=child_env,
                                                   stdout=log, stderr=log))
            deadline = time.monotonic() + 8
            expected = {f'org.mpris.MediaPlayer2.Clarp.instance{p.pid}' for p in processes}
            while True:
                assert all(p.poll() is None for p in processes), 'An instance exited during startup'
                services = subprocess.check_output(['busctl', '--user', '--no-legend', 'list'], text=True)
                if expected.issubset({line.split()[0] for line in services.splitlines()}):
                    break
                assert time.monotonic() < deadline, 'Both instances must expose independent MPRIS services'
                time.sleep(0.1)
            assert processes[1].wait(timeout=12) == 0, 'Second window failed'
            assert processes[0].poll() is None, 'Closing second instance closed the first'
            assert processes[0].wait(timeout=18) == 0, 'First window failed'
            for index in range(2):
                assert (root / f'{index}.png').read_bytes().startswith(b'\x89PNG\r\n\x1a\n'), 'Missing window capture'
            print('PASS: two concurrent windows, independent MPRIS services, independent exit, both rendered')
        except BaseException:
            for log in logs:
                log.flush()
                log.seek(0)
                print(log.read(), file=sys.stderr)
            raise
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.wait()
            for log in logs:
                log.close()


if __name__ == '__main__':
    main()
