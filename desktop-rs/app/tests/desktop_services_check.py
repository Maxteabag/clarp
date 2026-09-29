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
    menu.Event(entries["Quit"], "clicked", dbus.String(""), dbus.UInt32(0))
    check(wait(lambda: app.poll() is not None, 10) and app.returncode == 0, "Quit ends the app cleanly")
finally:
    if app.poll() is None:
        app.kill()
print("PASS" if not failures else f"FAIL {len(failures)}")
sys.exit(1 if failures else 0)
