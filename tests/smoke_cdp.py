"""Real-browser smoke test for the cdp transport — no OpenEvidence, no login, no quota.

    python tests/smoke_cdp.py          (Linux without a display: xvfb-run -a python ...)

Starts a throwaway profile of your installed Edge / Chrome / Chromium on a free port,
points the server at a tiny local HTTP origin standing in for openevidence.com, and
runs the server's real request path through it: launch -> find_tab miss -> new tab ->
fire-and-forget fetch -> poll -> body pulled back in CHUNK slices -> slot deleted.
Then a second request (tab reuse), and the browser is closed again.
"""

import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


TMP = Path(tempfile.mkdtemp(prefix="oe-smoke-"))
os.environ.update(
    OE_TRANSPORT="cdp",
    OE_CDP_PORT=str(free_port()),
    OE_CDP_PROFILE=str(TMP / "profile"),
    OE_DATA_DIR=str(TMP),
)
ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "mcp"), str(ROOT)]   # public layout: mcp/server.py; private: ./server.py
import server  # noqa: E402

PAD = "x" * (server.CHUNK * 2 + 777)   # forces three slices


class Origin(BaseHTTPRequestHandler):
    def _send(self, code, ctype, body: bytes):
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/"):
            self._send(200, "application/json", json.dumps({"method": "GET", "path": self.path}).encode())
        else:
            self._send(200, "text/html", b"<!doctype html><title>fake OE</title><p>ok</p>")

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        got = json.loads(self.rfile.read(n) or b"null")
        out = {"echo": got, "ctype": self.headers.get("content-type"), "pad": PAD}
        self._send(200, "application/json", json.dumps(out).encode())

    def log_message(self, *a):
        pass


def main() -> int:
    httpd = ThreadingHTTPServer(("127.0.0.1", free_port()), Origin)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    server.BASE_URL = f"http://127.0.0.1:{httpd.server_address[1]}"
    print(f"platform={sys.platform} browser={server.BROWSER_EXE}")
    print(f"cdp={server.CDP_ENDPOINT} profile={server.CDP_PROFILE}")
    try:
        t = time.monotonic()
        status, body = server._relay_request("POST", "/api/article", {"question": "ü 中文 ok"})
        data = json.loads(body)
        assert status == 200, status
        assert data["echo"] == {"question": "ü 中文 ok"}, data["echo"]
        assert data["ctype"] == "application/json", data["ctype"]
        assert data["pad"] == PAD, "chunked body did not reassemble intact"
        print(f"POST ok: {len(body)} bytes in {time.monotonic() - t:.1f}s (cold start included)")

        status, body = server._relay_request("GET", "/api/article/abc/output")
        assert status == 200 and json.loads(body)["path"] == "/api/article/abc/output", body
        tabs = server._bridge_cmd("list_tabs", {})["tabs"]
        n_origin = sum(1 for x in tabs if x["url"].startswith(server.BASE_URL))
        assert n_origin == 1, f"expected the tab to be reused, found {n_origin} origin tabs"
        print("GET ok, tab reused")
        print("SMOKE PASS")
        return 0
    finally:
        try:
            server._CDP_POOL.submit(
                lambda: server._CDP["browser"].new_browser_cdp_session().send("Browser.close")
            ).result(15)
        except Exception as e:
            print(f"(browser close: {e})")
        httpd.shutdown()
        time.sleep(1)
        shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
