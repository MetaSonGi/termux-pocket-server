"""실행: python -m unittest -v"""
import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import pocket
import server


class FakeSite:
    """테스트용 로컬 웹서버. code 값을 바꿔 장애를 흉내냅니다."""

    def __init__(self):
        site = self
        self.code = 200

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(site.code)
                self.end_headers()
                self.wfile.write(b"x")

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class ConfigTest(unittest.TestCase):
    def write(self, data):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(data, f)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_defaults_without_file(self):
        cfg = pocket.load_config("/nonexistent/config.json")
        self.assertEqual(cfg["host"], "127.0.0.1")

    def test_rejects_unknown_key_and_bad_url(self):
        with self.assertRaises(ValueError):
            pocket.load_config(self.write({"prot": 1}))
        with self.assertRaises(ValueError):
            pocket.load_config(self.write({"monitors": [{"name": "x", "url": "file:///etc/passwd"}]}))

    def test_example_config_is_valid(self):
        here = os.path.dirname(os.path.abspath(__file__))
        cfg = pocket.load_config(os.path.join(here, "config.example.json"))
        self.assertTrue(cfg["monitors"])


class StatusTest(unittest.TestCase):
    def test_collect_status_has_keys(self):
        s = pocket.collect_status()
        for k in ("time", "hostname", "uptime", "memory", "disk", "battery"):
            self.assertIn(k, s)
        self.assertGreater(s["disk"]["total"], 0)

    def test_battery_parses_termux_json(self):
        fake = mock.Mock(return_value=mock.Mock(stdout='{"percentage": 15, "status": "DISCHARGING", "plugged": "UNPLUGGED", "temperature": 30.1}'))
        self.assertEqual(pocket.battery(runner=fake)["percentage"], 15)
        bad = mock.Mock(return_value=mock.Mock(stdout="not json"))
        self.assertIsNone(pocket.battery(runner=bad))

    def test_humanize(self):
        self.assertEqual(pocket.human_bytes(1536), "1.5KB")
        self.assertEqual(pocket.human_duration(90061), "1일 1시간 1분")
        self.assertEqual(pocket.human_duration(None), "-")


class MonitorTest(unittest.TestCase):
    def setUp(self):
        self.site = FakeSite()
        self.addCleanup(self.site.close)

    def test_check_url_ok_and_wrong_status(self):
        self.assertTrue(pocket.check_url(self.site.url)["ok"])
        self.site.code = 503
        r = pocket.check_url(self.site.url)
        self.assertFalse(r["ok"])
        self.assertEqual(r["status"], 503)

    def test_unreachable(self):
        r = pocket.check_url("http://127.0.0.1:9/", timeout=2)
        self.assertFalse(r["ok"])
        self.assertIsNone(r["status"])

    def test_down_after_threshold_then_recover_once(self):
        m = pocket.Monitor([{"name": "site", "url": self.site.url}], fail_threshold=2)
        self.assertEqual(m.run_once(), [])          # 첫 정상: 알림 없음
        self.site.code = 500
        self.assertEqual(m.run_once(), [])          # 1회 실패: 아직 알림 없음
        ev = m.run_once()
        self.assertEqual(len(ev), 1)
        self.assertIn("장애", ev[0])
        self.assertEqual(m.run_once(), [])          # 계속 장애: 중복 알림 없음
        self.site.code = 200
        ev = m.run_once()
        self.assertEqual(len(ev), 1)
        self.assertIn("복구", ev[0])

    def test_down_on_first_start_alerts(self):
        self.site.code = 500
        m = pocket.Monitor([{"name": "s", "url": self.site.url}], fail_threshold=1)
        self.assertIn("장애", m.run_once()[0])


class TelegramTest(unittest.TestCase):
    def test_disabled_without_env(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            t = pocket.Telegram()
            self.assertFalse(t.enabled)
            self.assertFalse(t.send("hi"))

    def test_send_builds_request_and_hides_token_on_error(self):
        captured = {}

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self): return b'{"ok": true}'

        def opener(req, timeout):
            captured["url"], captured["data"] = req.full_url, req.data
            return Resp()

        t = pocket.Telegram("123:ABC", "42", opener=opener)
        self.assertTrue(t.send("안녕"))
        self.assertIn("/bot123:ABC/sendMessage", captured["url"])
        self.assertIn(b"chat_id=42", captured["data"])

        def boom(req, timeout):
            raise urllib.error.URLError("fail")

        with mock.patch("builtins.print") as p:
            self.assertFalse(pocket.Telegram("SECRET", "1", opener=boom).send("x"))
            self.assertNotIn("SECRET", str(p.call_args_list))

    def test_battery_alert_once_until_recovered(self):
        low = {"percentage": 10, "status": "DISCHARGING"}
        msg, st = pocket.battery_alert(low, 20, False)
        self.assertIn("배터리", msg)
        self.assertEqual(pocket.battery_alert(low, 20, st), (None, True))
        self.assertEqual(pocket.battery_alert({"percentage": 10, "status": "CHARGING"}, 20, True), (None, False))


class ServerTest(unittest.TestCase):
    def start(self, cfg, token=""):
        with mock.patch.dict(os.environ, {"DASHBOARD_TOKEN": token}):
            app = server.App(cfg, notifier=pocket.Telegram("", ""))
        srv, app = server.make_server(cfg, app)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}", app

    def cfg(self, **kw):
        c = dict(pocket.DEFAULT_CONFIG, port=0)
        c.update(kw)
        return c

    def test_dashboard_and_api(self):
        site = FakeSite()
        self.addCleanup(site.close)
        base, app = self.start(self.cfg(monitors=[{"name": "<b>x</b>", "url": site.url}]))
        app.tick()
        page = urllib.request.urlopen(base + "/").read().decode()
        self.assertIn("정상", page)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", page)   # 이름은 HTML 이스케이프
        data = json.loads(urllib.request.urlopen(base + "/api/status").read())
        self.assertTrue(data["monitors"][0]["up"])
        self.assertEqual(urllib.request.urlopen(base + "/healthz").read(), b"ok")

    def test_token_required_when_set(self):
        base, _ = self.start(self.cfg(), token="s3cret")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(base + "/api/status")
        self.assertEqual(cm.exception.code, 401)
        self.assertEqual(urllib.request.urlopen(base + "/api/status?token=s3cret").status, 200)
        req = urllib.request.Request(base + "/api/status", headers={"Authorization": "Bearer s3cret"})
        self.assertEqual(urllib.request.urlopen(req).status, 200)

    def test_public_bind_without_token_refused(self):
        with mock.patch.dict(os.environ, {"DASHBOARD_TOKEN": ""}):
            with self.assertRaises(SystemExit):
                server.make_server(self.cfg(host="0.0.0.0"))


if __name__ == "__main__":
    unittest.main()
