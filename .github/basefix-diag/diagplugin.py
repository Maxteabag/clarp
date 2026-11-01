"""Failure-metadata hook (base-repair DIAGNOSTIC; untracked; never committed). Own test process only.
Fires only on a failed/errored phase of a TARGET node. Records:
 O6 threads: name/daemon/alive + <=12 frames each as (path<=160 chars, line, function<=80)
    fds: /proc/self/fd link targets (<=256, each <=160 chars)
 O1/O2/O4 for anon_inode:inotify fds: fdinfo 'inotify' watch lines (wd/ino/sdev/mask) + FIONREAD byte count
 O7 counts/flags only: transcript_import_cache._resetting (bool) if loaded; tts_queue row COUNT if loaded.
Never: locals, args, source text, env, tokens, file/queue/transcript contents, other processes.
<=20 dumps x 64 KiB to /var/tmp/diag-obs. Retention: delete by 2026-11-08 or at the base PR merge."""
import json, os, sys, threading, traceback, fcntl, termios, struct, time
OUT = "/var/tmp/diag-obs"; CAP = 64 * 1024; MAX = 20; _n = [0]
TARGETS = ("test_inotify_dispatcher.py::", "test_transcript_streamer.py::test_streamer_enqueues",
           "test_transcript_streamer.py::test_streamer_fires_per", "test_transcript_streamer.py::test_a_held_import",
           "test_transcript_streamer.py::test_streamer_broadcasts", "test_streamer_hook_coordination.py::",
           "test_server_home_terminal_runs_shell_in_home")

def _fds():
    out = []
    for fd in sorted(os.listdir("/proc/self/fd"), key=int)[:256]:
        try: target = os.readlink(f"/proc/self/fd/{fd}")
        except OSError: continue
        rec = {"fd": int(fd), "target": target[:160]}
        if target == "anon_inode:inotify":
            try:
                with open(f"/proc/self/fdinfo/{fd}") as fh:
                    rec["watches"] = [l.strip()[:200] for l in fh if l.startswith("inotify")][:16]
                rec["fionread"] = struct.unpack("i", fcntl.ioctl(int(fd), termios.FIONREAD, struct.pack("i", 0)))[0]
            except OSError as e:
                rec["error"] = type(e).__name__
        out.append(rec)
    return out

def _threads():
    frames = sys._current_frames(); out = []
    for t in threading.enumerate()[:64]:
        f = frames.get(t.ident)
        st = [[fs.filename[-160:], fs.lineno, fs.name[:80]] for fs in traceback.extract_stack(f)][-12:] if f else []
        out.append({"name": t.name[:80], "daemon": t.daemon, "alive": t.is_alive(), "stack": st})
    return out

def _o7():
    r = {}
    tic = sys.modules.get("lib.transcript_import_cache")
    if tic is not None: r["importer_resetting"] = bool(getattr(tic, "_resetting", None))
    tq = sys.modules.get("lib.tts_queue")
    if tq is not None:
        try: r["tts_queue_rows"] = len(tq.recent(limit=50))
        except Exception as e: r["tts_queue_rows_error"] = type(e).__name__
    return r

def pytest_runtest_makereport(item, call):
    if call.excinfo is None or _n[0] >= MAX or not any(t in item.nodeid for t in TARGETS):
        return
    _n[0] += 1
    rec = {"nodeid": item.nodeid[:300], "when": call.when, "at": time.time(), "exc_type": call.excinfo.typename,
           "threads": _threads(), "fds": _fds(), "o7": _o7()}
    os.makedirs(OUT, exist_ok=True)
    with open(f"{OUT}/{_n[0]:02d}-{call.when}.json", "w") as fh:
        fh.write(json.dumps(rec)[:CAP])
