"""Transport tests: server.py's bridge commands with the browser mocked out.

    python -m unittest discover -s tests -v

No browser, no network, no Playwright needed: the cdp worker (`_cdp_run`) or the
Playwright objects underneath it are replaced by fakes.
"""

import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(_ROOT / "mcp"), str(_ROOT)]   # public layout: mcp/server.py; private: ./server.py
import server  # noqa: E402


class CdpCase(unittest.TestCase):
    """Base: TRANSPORT=cdp, the worker function replaced by self.respond, fresh pool."""

    def setUp(self):
        self.calls = []
        patches = [
            mock.patch.object(server, "TRANSPORT", "cdp"),
            mock.patch.object(server, "_cdp_run", self.run_cmd),
            mock.patch.object(server, "_CDP_POOL", None),
            mock.patch.object(server, "POLL_INTERVAL_SEC", 0),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_cmd(self, action, args):
        self.calls.append((action, args, threading.get_ident()))
        return self.respond(action, args or {})

    def respond(self, action, args):
        return {"success": True}


class TestPickTransport(unittest.TestCase):
    def pick(self, env):
        clean = {k: v for k, v in os.environ.items() if k not in ("OE_TRANSPORT", "OE_BRIDGE_URL")}
        clean.update(env)
        with mock.patch.dict(os.environ, clean, clear=True):
            return server._pick_transport()

    def test_default_is_cdp(self):
        self.assertEqual(self.pick({}), "cdp")

    def test_explicit_bridge_url_keeps_legacy_http(self):
        self.assertEqual(self.pick({"OE_BRIDGE_URL": "http://127.0.0.1:10086/command"}), "http")

    def test_forced_transport_wins(self):
        self.assertEqual(self.pick({"OE_TRANSPORT": "http"}), "http")
        self.assertEqual(self.pick({"OE_TRANSPORT": "CDP", "OE_BRIDGE_URL": "http://x/command"}), "cdp")

    def test_unknown_value_falls_back_to_default(self):
        with mock.patch("sys.stderr"):
            self.assertEqual(self.pick({"OE_TRANSPORT": "edge"}), "cdp")


class TestCdpCmd(CdpCase):
    def test_passes_action_and_args(self):
        self.assertEqual(server._bridge_cmd("list_tabs", {"a": 1}), {"success": True})
        self.assertEqual(self.calls[0][:2], ("list_tabs", {"a": 1}))

    def test_all_calls_run_on_one_thread(self):
        # MCP tools enter via asyncio.to_thread from varying threads; Playwright sync needs one.
        threads = [threading.Thread(target=server._bridge_cmd, args=("list_tabs", {})) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        idents = {c[2] for c in self.calls}
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(len(idents), 1)
        self.assertNotIn(threading.get_ident(), idents)

    def test_foreign_error_becomes_runtime_error(self):
        class PlaywrightError(Exception):
            pass

        def boom(action, args):
            raise PlaywrightError("Target page, context or browser has been closed")
        self.respond = boom
        with self.assertRaisesRegex(RuntimeError, "cdp: evaluate: Target page"):
            server._bridge_cmd("evaluate", {"code": "1"})

    def test_timeout(self):
        self.respond = lambda action, args: time.sleep(0.5) or {}
        with self.assertRaisesRegex(RuntimeError, "did not return within"):
            server._cdp_cmd("evaluate", {"code": "1"}, timeout=0.05)

    def test_http_transport_still_goes_to_kimi(self):
        with mock.patch.object(server, "TRANSPORT", "http"), \
             mock.patch.object(server, "_kimi_cmd", return_value={"via": "http"}) as k:
            self.assertEqual(server._bridge_cmd("list_tabs", {}), {"via": "http"})
            k.assert_called_once()
        self.assertEqual(self.calls, [])


class TestEnsureOeTab(CdpCase):
    def test_existing_tab_no_navigate_and_no_daemon(self):
        with mock.patch.object(server, "_ensure_kimi_daemon") as d, mock.patch.object(server, "_ensure_browser") as b:
            self.respond = lambda action, args: {"success": True, "url": server.BASE_URL + "/"}
            server._ensure_oe_tab()
            d.assert_not_called()
            b.assert_not_called()
        self.assertEqual([c[0] for c in self.calls], ["list_tabs", "find_tab"])
        self.assertEqual(self.calls[1][1], {"url": server.BASE_URL})

    def test_no_tab_opens_one(self):
        def respond(action, args):
            if action == "list_tabs":
                return {"success": True, "tabs": []}
            if action == "find_tab":
                return {"success": False}
            return {"success": True, "url": args["url"], "status": 200}
        self.respond = respond
        server._ensure_oe_tab()
        self.assertEqual([c[0] for c in self.calls], ["list_tabs", "find_tab", "navigate"])
        nav = self.calls[2][1]
        self.assertEqual(nav["url"], server.BASE_URL + "/")
        self.assertTrue(nav["newTab"])


# ---------- _cdp_run against fake Playwright objects ----------

class FakePage:
    def __init__(self, url="about:blank"):
        self.url, self.closed, self.evaluated = url, False, []

    def is_closed(self):
        return self.closed

    def goto(self, url, wait_until=None, timeout=None):
        self.url = url
        return mock.Mock(status=200)

    def evaluate(self, code):
        self.evaluated.append(code)
        return "started"


class FakeContext:
    def __init__(self, pages):
        self.pages = pages

    def new_page(self):
        p = FakePage()
        self.pages.append(p)
        return p


class TestCdpRun(unittest.TestCase):
    def setUp(self):
        self.ctx = FakeContext([FakePage("https://example.com/"), FakePage(server.BASE_URL + "/ask/1")])
        patches = [
            mock.patch.object(server, "_CDP", {"pw": None, "browser": None, "page": None}),
            mock.patch.object(server, "_cdp_context", lambda: self.ctx),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_find_tab_adopts_an_existing_oe_tab(self):
        r = server._cdp_run("find_tab", {"url": server.BASE_URL})
        self.assertTrue(r["success"])
        self.assertIs(server._CDP["page"], self.ctx.pages[1])

    def test_find_tab_miss(self):
        self.ctx.pages.pop()
        self.assertEqual(server._cdp_run("find_tab", {"url": server.BASE_URL}), {"success": False})

    def test_navigate_new_tab_then_evaluate_runs_there(self):
        server._cdp_run("navigate", {"url": server.BASE_URL + "/", "newTab": True})
        page = server._CDP["page"]
        self.assertEqual(len(self.ctx.pages), 3)
        self.assertEqual(page.url, server.BASE_URL + "/")
        self.assertEqual(server._cdp_run("evaluate", {"code": "1+1"}), {"value": "started"})
        self.assertEqual(page.evaluated, ["1+1"])

    def test_closed_tab_is_dropped_not_reused(self):
        server._cdp_run("find_tab", {"url": server.BASE_URL})
        self.ctx.pages[1].closed = True
        with self.assertRaisesRegex(RuntimeError, "tab was closed"):
            server._cdp_run("evaluate", {"code": "1"})
        self.assertIsNone(server._CDP["page"])


class TestCdpLaunch(unittest.TestCase):
    def test_already_up_does_not_spawn(self):
        with mock.patch.object(server, "_cdp_version", return_value={"Browser": "x"}), \
             mock.patch.object(server.subprocess, "Popen") as popen:
            self.assertFalse(server._cdp_launch())
            popen.assert_not_called()

    def test_spawns_dedicated_profile_on_the_port(self):
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d, "browser")
            exe.write_text("")
            prof = Path(d, "profile")
            seq = iter([None, None, {"Browser": "x"}])
            with mock.patch.object(server, "_cdp_version", lambda: next(seq)), \
                 mock.patch.object(server, "BROWSER_EXE", str(exe)), \
                 mock.patch.object(server, "CDP_PROFILE", prof), \
                 mock.patch.object(server, "CDP_LAUNCH_LOG", Path(d, "launch.log")), \
                 mock.patch.object(server, "CDP_EXTRA_ARGS", ["--no-sandbox"]), \
                 mock.patch.object(server.time, "sleep"), \
                 mock.patch.object(server.subprocess, "Popen") as popen:
                popen.return_value.poll.return_value = None
                self.assertTrue(server._cdp_launch("https://x/"))
            argv = popen.call_args.args[0]
            self.assertEqual(argv[0], str(exe))
            self.assertIn(f"--user-data-dir={prof}", argv)
            self.assertIn(f"--remote-debugging-port={server.CDP_PORT}", argv)
            self.assertEqual(argv[-2:], ["--no-sandbox", "https://x/"])
            self.assertTrue(prof.is_dir())

    def test_early_exit_reports_the_browser_output(self):
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d, "browser")
            exe.write_text("")
            log = Path(d, "launch.log")

            def fake_popen(argv, stdout=None, **kw):
                stdout.write(b"FATAL: No usable sandbox!\n")
                proc = mock.Mock(returncode=1)
                proc.poll.return_value = 1
                return proc
            with mock.patch.object(server, "_cdp_version", return_value=None), \
                 mock.patch.object(server, "BROWSER_EXE", str(exe)), \
                 mock.patch.object(server, "CDP_PROFILE", Path(d, "profile")), \
                 mock.patch.object(server, "CDP_LAUNCH_LOG", log), \
                 mock.patch.object(server.time, "sleep"), \
                 mock.patch.object(server.subprocess, "Popen", fake_popen):
                with self.assertRaisesRegex(RuntimeError, "(?s)exited with code 1.*No usable sandbox"):
                    server._cdp_launch()

    def test_no_browser_is_a_clear_error(self):
        with mock.patch.object(server, "_cdp_version", return_value=None), \
             mock.patch.object(server, "BROWSER_EXE", None):
            with self.assertRaisesRegex(RuntimeError, "OE_BROWSER_EXE"):
                server._cdp_launch()


# ---------- the fire / poll / slice protocol over the transport ----------

class FakeTab:
    """Plays the page side of _relay_request's fire / poll / slice / delete protocol."""

    def __init__(self, status, body, gone=False):
        self.status, self.body, self.gone = status, body, gone
        self.fired = None
        self.slices = []
        self.deleted = False

    def respond(self, action, args):
        if action == "list_tabs":
            return {"success": True, "tabs": []}
        if action == "find_tab":
            return {"success": True, "url": server.BASE_URL + "/"}
        code = args["code"]
        if "fetch(" in code:
            self.fired = code
            return {"value": "started"}
        if "gone: true" in code:
            if self.gone:
                return {"value": json.dumps({"gone": True})}
            return {"value": json.dumps({"done": True, "status": self.status, "len": len(self.body), "error": None})}
        m = re.search(r"\.body\.slice\((\d+), (\d+)\)", code)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            self.slices.append((a, b))
            return {"value": self.body[a:b]}
        if code.startswith("delete window."):
            self.deleted = True
            return {"value": "ok"}
        raise AssertionError(f"unexpected code: {code[:80]}")


class TestRelayRequest(CdpCase):
    def test_round_trip_in_chunks(self):
        body = json.dumps({"id": "abc", "text": "x" * (server.CHUNK * 2 + 123)})
        tab = FakeTab(200, body)
        self.respond = tab.respond
        status, got = server._relay_request("POST", "/api/article", {"q": "hello"})
        self.assertEqual(status, 200)
        self.assertEqual(got, body)
        self.assertEqual(len(tab.slices), 3)
        self.assertTrue(tab.deleted)
        self.assertIn('"/api/article"', tab.fired)
        self.assertIn("credentials: 'include'", tab.fired)

    def test_gone_slot_raises(self):
        tab = FakeTab(200, "{}", gone=True)
        self.respond = tab.respond
        with self.assertRaisesRegex(RuntimeError, "reloaded"):
            server._relay_request("GET", "/api/article/x/output")


if __name__ == "__main__":
    unittest.main()
