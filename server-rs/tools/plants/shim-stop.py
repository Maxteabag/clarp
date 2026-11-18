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
RUNS = {'plain': ['test', '--test', 'shim', '--', 'plain_stop_owns_the_group_until_the_end_is_written'],
        'served': ['test', '--test', 'host', '--test', 'shim', '--', 'opencode_and_deepseek_run_resume_abort_and_report_their_native_sessions'],
        'stop': ['test', '--test', 'shim', '--', 'a_direct_child_can_abort_before_its_group_is_terminated']}
PLANTS = [
    (MAIN, 'stop', 'the direct-child signal reaches the entire group',
     'let _ = kill(pid, early.signal);', 'let _ = killpg(pid, early.signal);'),
    (MAIN, 'plain', 'a stop cannot reach descendants after leader exit',
     'if !matches!(*stage, StopStage::Ready) {', 'if self.leader_exited.load(Ordering::SeqCst) || !matches!(*stage, StopStage::Ready) {'),
    (MAIN, 'plain', 'plain Stop does nothing', '*stage = StopStage::Asked;', 'if self.before_stop.is_none() { return; } *stage = StopStage::Asked;'),
    (MAIN, 'plain', 'each request starts a fresh escalation',
     'if !matches!(*stage, StopStage::Ready) {', 'if matches!(*stage, StopStage::Finished | StopStage::Signalled) {'),
    (MAIN, 'plain', 'the group is killed as soon as stop is asked',
     'Self::signal(self.pid, &self.to_spool, Signal::SIGTERM);', 'Self::signal(self.pid, &self.to_spool, Signal::SIGKILL);'),
    (MAIN, 'plain', 'TERM is only sent to the leader',
     'Self::signal(self.pid, &self.to_spool, Signal::SIGTERM);', 'let _ = kill(Pid::from_raw(self.pid as i32), Signal::SIGTERM);'),
    (MAIN, 'plain', 'escalation is forgotten when the leader exits',
     'Self::signal(self.pid, &self.to_spool, Signal::SIGKILL);', 'if !self.leader_exited.load(Ordering::SeqCst) { Self::signal(self.pid, &self.to_spool, Signal::SIGKILL); }'),
    (MAIN, 'plain', 'descendants get no grace',
     'Duration::from_millis(grace_ms).saturating_sub(term_at.elapsed())', 'Duration::ZERO.saturating_sub(term_at.elapsed())'),
    (MAIN, 'plain', 'descendants keep running past the deadline',
     'Duration::from_millis(grace_ms).saturating_sub(term_at.elapsed())', 'Duration::from_millis(grace_ms * 5).saturating_sub(term_at.elapsed())'),
    (MAIN, 'plain', 'a leader is reaped before its descendants have gone',
     'WaitPidFlag::WEXITED | WaitPidFlag::WNOWAIT', 'WaitPidFlag::WEXITED'),
    (MAIN, 'plain', 'the original exit code is discarded', 'code: observed.code(),', 'code: None,'),
    (MAIN, 'plain', 'the original terminating signal is discarded', 'signal: observed.signal(),', 'signal: None,'),
    (MAIN, 'plain', 'the end is written without waiting for the pipes to close',
     'let _ = pump.join();', 'drop(pump);'),
    (MAIN, 'stop', 'the wait for direct abort is omitted', 'stage, early.wait,', 'stage, Duration::ZERO,'),
    (MAIN, 'stop', 'the abort wait continues after leader exit', '|_| !self.leader_exited.load(Ordering::SeqCst)', '|_| true'),
    ('clarp-shim/src/opencode.rs', 'served', 'a shim-directed abort is mistaken for a completed turn',
     'let _ = kill(Pid::this(), Signal::SIGTERM);', 'std::process::exit(0);'),
    ('clarp-shim/src/opencode.rs', 'served', 'the loopback JSON request loses its body',
     '.with_body(serde_json::to_vec(body)?)', '.with_body(Vec::new())'),

]


def built_size():
    """Use the workspace build the plant workflow already made, before modifying any code."""
    binary = Path(__file__).resolve().parents[2] / 'target/debug/clarp-shim'
    workspace = binary.parents[2]
    edges = subprocess.check_output(
        ['cargo', 'tree', '--locked', '-p', 'clarp-shim', '--edges', 'normal,build', '--prefix', 'none'],
        text=True, cwd=workspace)
    packages = {line.split()[0] for line in edges.splitlines() if line.strip()}
    excluded = packages & {'ureq', 'rustls', 'openssl', 'native-tls', 'signal-hook',
                           'flate2', 'brotli', 'zstd', 'clarp-speech'}
    if excluded or 'minreq' not in packages:
        raise RuntimeError(f'unexpected standalone shim dependencies: {sorted(packages)}')
    print('shim dependencies:', ', '.join(sorted(packages)), flush=True)
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
