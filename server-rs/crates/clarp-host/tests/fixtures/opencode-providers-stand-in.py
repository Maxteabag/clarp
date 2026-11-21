#!/usr/bin/env python3
"""A stand-in for OpenCode's sign-ins: `--version`, and `serve` on loopback with its own
password. It keeps what it is asked in $FAKE_ROOT/serve-requests BEFORE it answers, and
writes OpenCode's store itself when a sign-in completes, as OpenCode does. Every key and
code here is made up."""
import base64, json, os, pathlib, signal, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

root = pathlib.Path(os.environ["FAKE_ROOT"])
with open(root / "started", "a") as out:
    out.write(" ".join(sys.argv[1:]) + "\n")
if sys.argv[1:] == ["--version"]:
    print("9.9.9-stand-in")
    sys.exit(0)
assert sys.argv[1:] == ["serve", "--hostname", "127.0.0.1", "--port", "0"], sys.argv
PASSWORD = os.environ["OPENCODE_SERVER_PASSWORD"]
EXPECTED = "Basic " + base64.b64encode(f"opencode:{PASSWORD}".encode()).decode()
store = pathlib.Path(os.environ["XDG_DATA_HOME"]) / "opencode" / "auth.json"


def ended(*_):
    with open(root / "serve-ended", "a") as out:
        out.write(f"{os.getpid()}\n")
    sys.exit(0)


signal.signal(signal.SIGTERM, ended)


def signed_in(provider):
    data = json.loads(store.read_text()) if store.exists() else {}
    data[provider] = {"type": "oauth", "refresh": "made-up", "access": "made-up", "expires": 1}
    store.write_text(json.dumps(data))


METHODS = {
    "openai": [{"type": "oauth", "label": "ChatGPT Pro/Plus (browser)"},
               {"type": "oauth", "label": "ChatGPT Pro/Plus (headless)",
                "prompts": [{"type": "text", "key": "org"}, "loose"]},
               {"type": "api", "label": "Manually enter API Key"}],
    "github-copilot": [{"type": "oauth", "label": "Login with GitHub Copilot"}],
    "broken": [{"type": "oauth", "label": "Broken device"}],
    "silent": [{"type": "oauth", "label": "Silent device"}],
    "azure": [{"type": "oauth", "label": "Login with Azure CLI"}],
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def answer(self, status, value):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_any(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        path, _, query = self.path.partition("?")
        seen = {"method": self.command, "path": path, "query": query,
                "authorized": self.headers.get("Authorization") == EXPECTED,
                "body": json.loads(raw) if raw else None}
        with open(root / "serve-requests", "a") as out:
            out.write(json.dumps(seen) + "\n")
        if not seen["authorized"]:
            return self.answer(401, {"error": "who are you"})
        body = seen["body"] or {}
        if path == "/provider/auth":
            if (root / "hold-probe").exists():
                # Answered when the test lets it, so that listings are asked meanwhile.
                while not (root / "release-probe").exists():
                    time.sleep(0.05)
            return self.answer(200, METHODS)
        provider = path.split("/")[2] if path.startswith("/provider/") else ""
        if path.endswith("/oauth/authorize"):
            if provider == "silent":
                return self.answer(200, None)
            if provider == "broken":
                return self.answer(500, {"error": "made-up failure", "key": "sk-abcdefgh12345678"})
            if provider == "github-copilot":
                return self.answer(200, {"url": "https://example.invalid/device", "method": "auto",
                                         "instructions": "Enter code: WXYZ-1234"})
            return self.answer(200, {"url": "https://example.invalid/code", "method": "code",
                                     "instructions": "Open the page, then type ABCD-EFGH"})
        if path.endswith("/oauth/callback"):
            if body.get("code") is None:
                # A sign-in in the provider's own page: done when the test approves it.
                while not (root / "approve").exists():
                    time.sleep(0.05)
                signed_in(provider)
                return self.answer(200, True)
            if body["code"] == "slow-code":
                # Answered when the test lets it, so that it can read the sign-in meanwhile.
                while not (root / "release-code").exists():
                    time.sleep(0.05)
                signed_in(provider)
                return self.answer(200, True)
            if body["code"] == "good-code":
                signed_in(provider)
                return self.answer(200, True)
            return self.answer(200, False)
        return self.answer(404, {"error": "no such route"})

    do_GET = do_POST = handle_any


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
print(f"opencode server listening on http://127.0.0.1:{server.server_address[1]}", flush=True)
server.serve_forever()
