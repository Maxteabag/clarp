#!/usr/bin/env python3
"""Tray and notifications of the real window, on a private session bus.

Plays a StatusNotifierWatcher (the tray host side) and a notification
daemon, starts clarp-desktop against the fake Host, then drives the tray
over D-Bus like a panel would: mute through the menu, a reply in another
chat raises a notification, Quit exits. Run inside dbus-run-session (see
run-desktop-services.sh); never on the user's bus.
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
# Only the wrapper, which starts a private bus, sets this: owning these
# names on the user's bus would take over their panel and notifications.
if not os.environ.get("CLARP_PRIVATE_BUS"):
    sys.exit("refusing to run outside a private bus (use run-desktop-services.sh)")
bus = dbus.SessionBus()

items, notes = [], []


class Watcher(dbus.service.Object):
    @dbus.service.method("org.kde.StatusNotifierWatcher", in_signature="s", sender_keyword="sender")
    def RegisterStatusNotifierItem(self, service, sender=None):
        items.append(service if service.startswith(":") or "." in service else sender)
        self.StatusNotifierItemRegistered(service)

    @dbus.service.method("org.kde.StatusNotifierWatcher", in_signature="s")
    def RegisterStatusNotifierHost(self, service):
        pass

    @dbus.service.signal("org.kde.StatusNotifierWatcher", signature="s")
    def StatusNotifierItemRegistered(self, service):
        pass

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="ss", out_signature="v")
    def Get(self, interface, name):
        return self.GetAll(interface)[name]

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        return {"IsStatusNotifierHostRegistered": True, "ProtocolVersion": dbus.Int32(0),
                "RegisteredStatusNotifierItems": dbus.Array(items, signature="s")}


class Notifications(dbus.service.Object):
    @dbus.service.method("org.freedesktop.Notifications", in_signature="susssasa{sv}i", out_signature="u")
    def Notify(self, app, replaces, icon, summary, body, actions, hints, timeout):
        notes.append({"app": str(app), "summary": str(summary), "body": str(body), "timeout": int(timeout)})
        return dbus.UInt32(len(notes))

    @dbus.service.method("org.freedesktop.Notifications", out_signature="as")
    def GetCapabilities(self):
        return ["body"]


watcher_name = dbus.service.BusName("org.kde.StatusNotifierWatcher", bus)
Watcher(bus, "/StatusNotifierWatcher")
notify_name = dbus.service.BusName("org.freedesktop.Notifications", bus)
Notifications(bus, "/org/freedesktop/Notifications")

base, settings, binary = sys.argv[1], sys.argv[2], sys.argv[3]
app = subprocess.Popen([binary, "--no-new-agent"], stderr=subprocess.STDOUT, stdout=open(os.environ["CLARP_APP_LOG"], "w"))
failures = []


def check(ok, what):
    print(("ok   " if ok else "FAIL ") + what, flush=True)
    if not ok:
        failures.append(what)


def wait(condition, seconds=15):
    deadline = time.time() + seconds
    while time.time() < deadline:
        while GLib.MainContext.default().pending():
            GLib.MainContext.default().iteration(False)
        if condition():
            return True
        time.sleep(0.05)
    return False


def muted():
    try:
        return json.load(open(settings)).get("audio/muted")
    except (OSError, ValueError):
        return None


try:
    check(wait(lambda: items), "the tray registers with the host")
    service = items[0]
    item = bus.get_object(service, "/StatusNotifierItem")
    menu_path = item.Get("org.kde.StatusNotifierItem", "Menu", dbus_interface=dbus.PROPERTIES_IFACE)
    check(item.Get("org.kde.StatusNotifierItem", "Title", dbus_interface=dbus.PROPERTIES_IFACE) == "Clarp", "titled Clarp")
    menu = dbus.Interface(bus.get_object(service, menu_path), "com.canonical.dbusmenu")
    _, layout = menu.GetLayout(0, -1, [])
    entries = {str(child[1].get("label", "")): int(child[0]) for child in layout[2]}
    check(set(entries) >= {"Show Clarp", "Mute voice replies", "Quit"}, "menu: " + ", ".join(entries))
    menu.Event(entries["Mute voice replies"], "clicked", dbus.String(""), dbus.UInt32(0))
    check(wait(lambda: muted() is True), "muting from the tray mutes the window")
    request = urllib.request.Request(base + "/__control/event", method="POST", headers={"Content-Type": "application/json"},
                                     data=json.dumps({"type": "user-notification", "session": "mike", "persona": "Mike",
                                                      "preview": "A new reply"}).encode())
    urllib.request.urlopen(request).read()
    check(wait(lambda: notes), "a reply in a closed chat notifies")
    check(notes and notes[0]["summary"] == "Mike" and notes[0]["body"] == "A new reply" and notes[0]["timeout"] == 8000,
          "notification: " + json.dumps(notes[:1]))
    menu.Event(entries["Show Clarp"], "clicked", dbus.String(""), dbus.UInt32(0))

    def activity():
        log = [json.loads(line) for line in open(os.environ["CLARP_HOST_LOG"]) if line.strip()]
        return [r["body"] for r in log if r["path"] == "/application-activity"]
    check(wait(lambda: any(a.get("foreground") for a in activity())), "the shown window reports foreground activity")
    first = activity()[0]
    check(first["instance_id"] and first["sequence"] >= 1 and "input_age_ms" in first, "activity report: " + json.dumps(first))

    # A second launch opens its window in this process and exits at once.
    def gets():
        log = [json.loads(line) for line in open(os.environ["CLARP_HOST_LOG"]) if line.strip()]
        return [r["path"] for r in log if r["method"] == "GET"]
    runtime = os.environ["XDG_RUNTIME_DIR"]
    sockets = [name for name in os.listdir(runtime) if name.startswith("clarp-desktop-") and name.endswith(".sock")]
    check(len(sockets) == 1 and os.stat(os.path.join(runtime, sockets[0])).st_mode & 0o777 == 0o700,
          "the first window listens on a private instance socket: " + ", ".join(sockets))
    startup = gets()[0]
    before = gets().count(startup)
    started = time.time()
    second = subprocess.run([binary, "--no-new-agent"], capture_output=True, timeout=20)
    took = time.time() - started
    check(second.returncode == 0 and took < 2, f"a second launch hands over and exits ({took:.2f} s, exit {second.returncode})")
    check(wait(lambda: gets().count(startup) > before), f"the running app opens a second window, which loads {startup}")
    check(app.poll() is None, "the first window keeps running")
    def memory_lines():
        return [json.loads(line.split(" ", 1)[1]) for line in open(os.environ["CLARP_APP_LOG"]) if line.startswith("memory {")]
    check(wait(lambda: memory_lines(), 10), "the memory line is logged")
    line = memory_lines()[-1]
    check(line["items"] > 50 and 0 < line["textItems"] < line["items"] and line["VmRSS"] > 0 and line["conversations"] >= 0,
          "memory line counts the real window: " + json.dumps({k: line[k] for k in ("items", "textItems", "VmRSS", "agents", "stalls")}))
    stall_log = os.path.join(os.environ["XDG_STATE_HOME"], "clarp", "desktop-stalls.log")
    check(not os.path.exists(stall_log) or os.path.getsize(stall_log) < 1_000_000, "the watchdog runs by default, logging under the state dir")
    menu.Event(entries["Quit"], "clicked", dbus.String(""), dbus.UInt32(0))
    check(wait(lambda: app.poll() is not None, 10) and app.returncode == 0, "Quit ends the app cleanly")
    check(not any(name.endswith(".sock") for name in os.listdir(runtime)), "quitting removes the instance socket")
    alone = subprocess.Popen([binary, "--no-new-agent"], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    check(wait(lambda: any(n.endswith(".sock") for n in os.listdir(runtime)) or alone.poll() is not None, 15)
          and alone.poll() is None, "with nobody listening, the next launch runs and listens itself")
    alone.terminate()
    alone.wait(10)
finally:
    if app.poll() is None:
        app.kill()
print("PASS" if not failures else f"FAIL {len(failures)}")
sys.exit(1 if failures else 0)
