"""Private gate: parent retains unreaped birth identity BEFORE permitting exec."""
import os
import select
import signal
import sys
import time
import math


def main():
    fd = int(sys.argv[1])
    intended_mask = {int(x) for x in sys.argv[2].split(',') if x}
    deadline = None if sys.argv[3] == "-" else float(sys.argv[3])
    argv = sys.argv[4:]
    if not argv or not argv[0].startswith('/'):
        raise SystemExit(126)
    if not select.select([fd], [], [], 5)[0] or os.read(fd, 1) != b'G':
        raise SystemExit(126)
    os.close(fd)
    # Restore the parent's ORIGINAL mask, not its temporary launch reservation mask.
    signal.pthread_sigmask(signal.SIG_SETMASK, intended_mask)
    if deadline is not None and (not math.isfinite(deadline) or time.monotonic() >= deadline):
        raise SystemExit(126)
    os.execvpe(argv[0], argv, os.environ)


if __name__ == '__main__':
    main()
