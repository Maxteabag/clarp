"""Private session-bus notification daemon kept alive across two app processes."""
import json
import os
from pathlib import Path
import subprocess
import sys

from gi.repository import Gio, GLib

INTERFACE = "org.freedesktop.Notifications"
PATH = "/org/freedesktop/Notifications"
XML = """<node><interface name="org.freedesktop.Notifications">
<method name="Notify">
<arg type="s" direction="in"/><arg type="u" direction="in"/><arg type="s" direction="in"/>
<arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="as" direction="in"/>
<arg type="a{sv}" direction="in"/><arg type="i" direction="in"/><arg type="u" direction="out"/>
</method><method name="CloseNotification"><arg type="u" direction="in"/></method>
<signal name="ActionInvoked"><arg type="u"/><arg type="s"/></signal>
</interface></node>"""

bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
log = Path(os.environ["CLARP_RESTART_LOG"])
info = Gio.DBusNodeInfo.new_for_xml(XML)
visible = {}
serial = 100
old_button = None
current_button = None
button_id = None


def record(**fields):
    with log.open("a") as stream:
        print(json.dumps(fields), file=stream)


def method(connection, sender, path, interface, member, args, invocation):
    global serial, old_button, current_button, button_id
    if member == "CloseNotification":
        ident, = args.unpack()
        record(close=visible.pop(ident, None), ident=ident)
        invocation.return_value(GLib.Variant("()", ()))
        return
    _, replaces, _, _, body, actions, _, _ = args.unpack()
    serial += 1
    ident = replaces or serial
    visible[ident] = body
    record(body=body, replaces=replaces, ident=ident)
    invocation.return_value(GLib.Variant("(u)", (ident,)))
    mark = next((key for key in actions[::2] if key.endswith(":mark-read")), None)
    if body == "before restart":
        old_button = mark
    if body == "after restart":
        current_button, button_id = mark, ident


bus.register_object(PATH, info.interfaces[0], method, None, None)
bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
              "RequestName", GLib.Variant("(su)", (INTERFACE, 0)), None, 0, -1, None)
loop = GLib.MainLoop()
result = 1
phase = "first"


def launch():
    return subprocess.Popen(sys.argv[1:], env={**os.environ, "CLARP_CHECK_PASS": phase})


child = launch()


def progress():
    global child, phase, serial, result
    for command, key in (("stale", old_button), ("current", current_button)):
        trigger = log.with_suffix("." + command)
        if trigger.exists() and button_id is not None:
            trigger.unlink()
            bus.emit_signal(None, PATH, INTERFACE, "ActionInvoked", GLib.Variant("(us)", (button_id, key)))
            record(pressed=command)
    code = child.poll()
    if code is None:
        return True
    if code == 0 and phase == "first" and old_button:
        phase = "second"
        # The first popup expired while the app was down. Its ID now belongs
        # to a foreign popup, and the daemon will reuse it again for the next Notify.
        ident = next(i for i, body in visible.items() if body == "before restart")
        visible[ident] = "another application"
        serial = ident - 1
        child = launch()
        return True
    result = code if phase == "second" else 1
    loop.quit()
    return False


GLib.timeout_add(50, progress)
try:
    loop.run()
finally:
    if child.poll() is None:
        child.terminate()
        child.wait()
sys.exit(result)
