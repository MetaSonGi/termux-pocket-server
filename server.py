"""상태 대시보드 HTTP 서버 + 백그라운드 모니터.

python server.py            # config.json 기준 실행
기본은 127.0.0.1에만 열립니다. 같은 와이파이의 다른 기기에서 보려면 config.json의 host를 "0.0.0.0"으로
바꾸고 DASHBOARD_TOKEN 환경변수를 설정하세요(토큰 없이 외부 공개는 거부합니다).
"""
from __future__ import annotations

import hmac
import html
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pocket

PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="30">
<title>Pocket Server</title><style>
body{{font:15px/1.5 system-ui,sans-serif;margin:0;padding:16px;background:#111;color:#eee}}
h1{{font-size:20px;margin:0 0 12px}}.g{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px}}
.c{{background:#1c1c1c;border:1px solid #333;border-radius:10px;padding:10px}}.c b{{display:block;font-size:18px}}
table{{width:100%;border-collapse:collapse;margin-top:14px}}td,th{{padding:6px;border-bottom:1px solid #333;text-align:left;font-size:14px}}
.up{{color:#5ad17d}}.down{{color:#ff6b6b}}.na{{color:#999}}small{{color:#999}}</style></head><body>
<h1>📱 {host}</h1><div class="g">{cards}</div>
<table><tr><th>모니터</th><th>상태</th><th>응답</th><th>오류</th></tr>{rows}</table>
<p><small>30초마다 새로고침 · {time} · JSON: /api/status</small></p></body></html>"""


def render_page(status: dict, monitors: list[dict]) -> str:
    e = html.escape
    mem, dsk, bat = status.get("memory"), status.get("disk"), status.get("battery")
    cards = [
        ("가동 시간", pocket.human_duration(status.get("uptime"))),
        ("부하(1/5/15분)", " / ".join(map(str, status["load"])) if status.get("load") else "-"),
        ("메모리", f"{mem['used_pct']}% · 여유 {pocket.human_bytes(mem['available'])}" if mem else "-"),
        ("저장공간", f"{dsk['used_pct']}% · 여유 {pocket.human_bytes(dsk['free'])}" if dsk else "-"),
        ("배터리", f"{bat['percentage']}% · {bat.get('status') or ''}" if bat and bat.get("percentage") is not None else "Termux:API 없음"),
    ]
    card_html = "".join(f'<div class="c"><small>{e(k)}</small><b>{e(str(v))}</b></div>' for k, v in cards)
    rows = []
    for m in monitors:
        cls, label = ("up", "정상") if m["up"] else (("down", "장애") if m["up"] is False else ("na", "확인 중"))
        last = m.get("last") or {}
        rows.append(f'<tr><td>{e(m["name"])}</td><td class="{cls}">{label}</td>'
                    f'<td>{e(str(last.get("ms") or "-"))}ms</td><td>{e(last.get("error") or "")}</td></tr>')
    if not rows:
        rows.append('<tr><td colspan="4"><small>config.json에 monitors를 추가하세요</small></td></tr>')
    return PAGE.format(host=e(status.get("hostname") or ""), cards=card_html, rows="".join(rows),
                       time=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(status["time"])))


class App:
    def __init__(self, cfg: dict, notifier=None, monitor=None):
        self.cfg = cfg
        self.notifier = notifier or pocket.Telegram()
        self.monitor = monitor or pocket.Monitor(cfg["monitors"], cfg["fail_threshold"])
        self.token = os.environ.get("DASHBOARD_TOKEN", "")
        self.stop = threading.Event()
        self._battery_alerted = False

    def tick(self) -> list[str]:
        events = self.monitor.run_once()
        msg, self._battery_alerted = pocket.battery_alert(pocket.battery(), self.cfg["battery_low"], self._battery_alerted)
        if msg:
            events.append(msg)
        for ev in events:
            self.notifier.send(ev)
        return events

    def loop(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # 모니터 오류로 서버가 죽지 않게
                print(f"[모니터 오류] {exc!r}")
            self.stop.wait(self.cfg["check_interval"])

    def authorized(self, handler: BaseHTTPRequestHandler) -> bool:
        if not self.token:
            return True
        q = parse_qs(urlparse(handler.path).query).get("token", [""])[0]
        h = handler.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        return any(hmac.compare_digest(x, self.token) for x in (q, h) if x)

    def handler(self):
        app = self

        class H(BaseHTTPRequestHandler):
            def _send(self, code, body: bytes, ctype):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if not app.authorized(self):
                    return self._send(401, "인증 필요 (?token=...)".encode(), "text/plain; charset=utf-8")
                path = urlparse(self.path).path
                if path == "/":
                    body = render_page(pocket.collect_status(), app.monitor.snapshot()).encode()
                    return self._send(200, body, "text/html; charset=utf-8")
                if path == "/api/status":
                    data = {"status": pocket.collect_status(), "monitors": app.monitor.snapshot()}
                    return self._send(200, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                if path == "/healthz":
                    return self._send(200, b"ok", "text/plain")
                self._send(404, b"not found", "text/plain")

            def log_message(self, fmt, *args):  # 토큰이 든 쿼리를 로그에 남기지 않음
                print(f"{self.address_string()} {self.command} {urlparse(self.path).path} {args[1] if len(args) > 1 else ''}")

        return H


def make_server(cfg: dict, app: App | None = None) -> tuple[ThreadingHTTPServer, App]:
    app = app or App(cfg)
    if cfg["host"] not in ("127.0.0.1", "localhost", "::1") and not app.token:
        raise SystemExit("외부 접속(host=%s)에는 DASHBOARD_TOKEN 환경변수가 필요합니다." % cfg["host"])
    return ThreadingHTTPServer((cfg["host"], cfg["port"]), app.handler()), app


def main():
    cfg = pocket.load_config()
    srv, app = make_server(cfg)
    threading.Thread(target=app.loop, daemon=True).start()
    print(f"대시보드: http://{cfg['host']}:{cfg['port']}/  (알림 {'켜짐' if app.notifier.enabled else '꺼짐'})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop.set()
        srv.server_close()


if __name__ == "__main__":
    main()
