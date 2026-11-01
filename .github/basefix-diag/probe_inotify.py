"""S0 read-path probe (DIAGNOSTIC, untracked). Prints ONE JSON line of metadata only: Python/kernel versions,
poll readiness, the FIONREAD byte count, the length of inotify_simple's _readall(), and the number of parsed
events, for one append to a private temp file. Never prints paths' contents, env or file data. Repeats 20x."""
import json, os, platform, select, struct, fcntl, termios, tempfile, time, sys
import inotify_simple
out = {"python": sys.version.split()[0], "kernel": platform.release(), "inotify_simple": getattr(inotify_simple, "__version__", "?"), "trials": []}
with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR", "/tmp")) as d:
    f = os.path.join(d, "t.jsonl"); open(f, "w").close()
    ino = inotify_simple.INotify()
    ino.add_watch(f, inotify_simple.flags.MODIFY | inotify_simple.flags.CLOSE_WRITE)
    for _ in range(20):
        with open(f, "a") as h:
            h.write("x\n")
        time.sleep(0.05)
        p = select.poll(); p.register(ino.fileno(), select.POLLIN)
        ready = bool(p.poll(500))
        n = struct.unpack("i", fcntl.ioctl(ino.fileno(), termios.FIONREAD, struct.pack("i", 0)))[0]
        raw = ino._readall()
        evs = list(inotify_simple.parse_events(raw)) if raw else []
        out["trials"].append({"ready": ready, "fionread": n, "readall_len": len(raw), "events": len(evs)})
    ino.close()
t = out["trials"]
out["summary"] = {"ready": sum(x["ready"] for x in t), "fionread_pos": sum(x["fionread"] > 0 for x in t),
                  "readall_pos": sum(x["readall_len"] > 0 for x in t), "with_events": sum(x["events"] > 0 for x in t), "n": len(t)}
print(json.dumps(out))
