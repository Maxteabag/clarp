#!/usr/bin/env python3
"""Prove the report viewer never fetches a remote resource.

Qt's rich text engine honours <img src>, <table background>, CSS url(...) and
CSS @import. A report assembled from scraped pages would otherwise phone home
the instant it is opened, leaking that it was read and from where.

Two runs share one listener:

* control  - the same fixture HTML rendered by a bare Qt TextEdit. Qt must hit
             the listener, otherwise this probe cannot detect a leak at all and
             a silent result would be meaningless.
* client   - the real client opening the report through the product path.
             It must not hit the listener even once.
"""
import http.server
import os
import re
import socketserver
import subprocess
import sys
import tempfile
import threading

hits: list[str] = []


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server API
        hits.append(self.path)
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args):
        pass


def fixture_html(source: str, origin: str) -> str:
    """Read the report fixture out of main.cpp so both runs use one source."""
    body = source.split('const QString body = QStringLiteral(', 1)[1]
    body = body.split(').arg(tracker);', 1)[0]
    parts = re.findall(r'"((?:[^"\\]|\\.)*)"', body)
    text = "".join(parts).replace('\\"', '"').replace("\\\\", "\\")
    return text.replace("%1", origin)


def run_control(runner: str, html: str, work: str) -> subprocess.CompletedProcess:
    path = os.path.join(work, "tst_control.qml")
    with open(path, "w") as handle:
        handle.write(
            "import QtQuick\nimport QtTest\n"
            "TestCase {\n"
            "  name: 'Control'\n  visible: true\n  when: windowShown\n"
            "  width: 900\n  height: 700\n"
            "  TextEdit { id: t; width: 860; readOnly: true;\n"
            "    textFormat: TextEdit.RichText; wrapMode: Text.Wrap }\n"
            "  function test_render() {\n"
            "    t.text = controlHtml;\n"
            "    wait(2500);\n"
            "  }\n"
            "  property string controlHtml: %r\n"
            "}\n" % html
        )
    return subprocess.run(
        [runner, "-input", path],
        env=dict(os.environ, QT_QPA_PLATFORM="offscreen",
                 QT_QUICK_BACKEND="software", QT_FORCE_STDERR_LOGGING="1"),
        capture_output=True, text=True, timeout=90)


def main() -> int:
    binary, main_cpp, runner = sys.argv[1], sys.argv[2], sys.argv[3]
    with open(main_cpp) as handle:
        source = handle.read()

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as server:
        port = server.server_address[1]
        origin = f"http://127.0.0.1:{port}"
        threading.Thread(target=server.serve_forever, daemon=True).start()

        with tempfile.TemporaryDirectory() as work:
            control = run_control(runner, fixture_html(source, origin), work)
            control_hits = list(hits)
            hits.clear()

            config = os.path.join(work, "config")
            os.makedirs(config, exist_ok=True)
            client = subprocess.run(
                [binary],
                env=dict(os.environ,
                         QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software",
                         QT_IM_MODULE="compose", QT_FORCE_STDERR_LOGGING="1",
                         CLARP_BASE_URL="http://127.0.0.1:1",
                         CLARP_TOKEN="offline-fixture",
                         CLARP_INSTANCE_NAME=f"report-no-fetch-{port}",
                         XDG_CONFIG_HOME=config,
                         CLARP_SCREENSHOT_SCENARIO="report",
                         CLARP_SCREENSHOT_DELAY_MS="4200",
                         CLARP_SCREENSHOT_PATH=os.path.join(work, "capture.png"),
                         CLARP_REPORT_TRACKER_ORIGIN=origin),
                capture_output=True, text=True, timeout=120)
            client_hits = list(hits)
        server.shutdown()

    if control.returncode != 0:
        sys.stderr.write(control.stderr[-3000:])
        print("FAIL: the control render did not run", file=sys.stderr)
        return 1
    if not control_hits:
        print("FAIL: the control render fetched nothing, so this probe cannot "
              "detect a leak and its silence would prove nothing", file=sys.stderr)
        return 1
    print(f"control render fetched {sorted(set(control_hits))}")

    if client.returncode != 0:
        sys.stderr.write(client.stderr[-3000:])
        print(f"FAIL: the client exited {client.returncode}", file=sys.stderr)
        return 1
    if client_hits:
        print(f"FAIL: the report viewer fetched {sorted(set(client_hits))}",
              file=sys.stderr)
        return 1
    print("ok: the report viewer fetched nothing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
