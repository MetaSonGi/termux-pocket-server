"""termux-pocket-server 핵심 기능: 상태 수집, HTTP 모니터, 텔레그램 알림.

표준 라이브러리만 사용합니다. 안드로이드 Termux, 일반 리눅스, macOS에서 동작하며
Termux 전용 기능(배터리 등)은 없으면 건너뜁니다.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

CONFIG_FILE = os.environ.get("POCKET_CONFIG", "config.json")

DEFAULT_CONFIG = {
    "host": "127.0.0.1",          # 다른 기기에서 보려면 "0.0.0.0" + DASHBOARD_TOKEN 설정
    "port": 8765,
    "check_interval": 60,         # 모니터 주기(초)
    "fail_threshold": 2,          # 연속 실패 몇 번이면 DOWN 알림
    "monitors": [],               # [{"name": "...", "url": "https://...", "expect_status": 200, "timeout": 10}]
    "battery_low": 20,            # 배터리 이 % 이하 + 충전 중 아님 → 알림
}


def load_config(path: str = CONFIG_FILE) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
        unknown = set(user) - set(DEFAULT_CONFIG)
        if unknown:
            raise ValueError(f"알 수 없는 설정 키: {', '.join(sorted(unknown))}")
        cfg.update(user)
    for m in cfg["monitors"]:
        validate_monitor(m)
    return cfg


def validate_monitor(m: dict) -> None:
    if not m.get("name"):
        raise ValueError("모니터에 name이 필요합니다")
    url = m.get("url", "")
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        raise ValueError(f"{m['name']}: url은 http(s)여야 합니다")


# ---------------------------------------------------------------- 상태 수집

def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def uptime_seconds() -> float | None:
    raw = _read("/proc/uptime")
    return float(raw.split()[0]) if raw else None


def load_average() -> list[float] | None:
    try:
        return [round(x, 2) for x in os.getloadavg()]
    except (OSError, AttributeError):
        return None


def memory() -> dict | None:
    raw = _read("/proc/meminfo")
    if not raw:
        return None
    info = {}
    for line in raw.splitlines():
        k, _, v = line.partition(":")
        parts = v.split()
        if parts and parts[0].isdigit():
            info[k] = int(parts[0]) * 1024
    total = info.get("MemTotal")
    avail = info.get("MemAvailable", info.get("MemFree"))
    if not total or avail is None:
        return None
    return {"total": total, "available": avail, "used_pct": round((total - avail) / total * 100, 1)}


def disk(path: str = os.path.expanduser("~")) -> dict:
    u = shutil.disk_usage(path)
    return {"total": u.total, "free": u.free, "used_pct": round(u.used / u.total * 100, 1)}


def battery(runner=subprocess.run) -> dict | None:
    """Termux:API의 termux-battery-status. 없거나 응답이 없으면 None."""
    if not shutil.which("termux-battery-status") and runner is subprocess.run:
        return None
    try:
        out = runner(["termux-battery-status"], capture_output=True, text=True, timeout=8)
        data = json.loads(out.stdout)
        return {"percentage": data.get("percentage"), "status": data.get("status"),
                "plugged": data.get("plugged"), "temperature": data.get("temperature")}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def collect_status() -> dict:
    return {
        "time": int(time.time()),
        "hostname": platform.node(),
        "system": f"{platform.system()} {platform.release()}",
        "python": platform.python_version(),
        "uptime": uptime_seconds(),
        "load": load_average(),
        "memory": memory(),
        "disk": disk(),
        "battery": battery(),
    }


def human_bytes(n: float | None) -> str:
    if n is None:
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return "-"


def human_duration(sec: float | None) -> str:
    if sec is None:
        return "-"
    sec = int(sec)
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m, _ = divmod(sec, 60)
    return (f"{d}일 " if d else "") + f"{h}시간 {m}분"


# ---------------------------------------------------------------- HTTP 모니터

def check_url(url: str, timeout: float = 10, expect_status: int = 200, opener=urllib.request.urlopen) -> dict:
    start = time.monotonic()
    req = urllib.request.Request(url, headers={"User-Agent": "termux-pocket-server/1.0"})
    try:
        with opener(req, timeout=timeout) as resp:
            code = resp.status
    except urllib.error.HTTPError as e:
        code = e.code
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"ok": False, "status": None, "ms": None, "error": str(getattr(e, "reason", e))[:200]}
    ms = round((time.monotonic() - start) * 1000)
    ok = code == expect_status
    return {"ok": ok, "status": code, "ms": ms, "error": None if ok else f"HTTP {code} (기대 {expect_status})"}


@dataclass
class MonitorState:
    name: str
    url: str
    up: bool | None = None          # None = 아직 판정 전
    fails: int = 0
    last: dict = field(default_factory=dict)
    changed_at: int | None = None


class Monitor:
    """연속 실패 횟수로 DOWN을 판정해 일시적 오류에 알림이 쏟아지지 않게 합니다."""

    def __init__(self, monitors: list[dict], fail_threshold: int = 2, checker=check_url, clock=time.time):
        self.cfg = {m["name"]: m for m in monitors}
        self.state = {m["name"]: MonitorState(m["name"], m["url"]) for m in monitors}
        self.threshold = max(1, int(fail_threshold))
        self.checker = checker
        self.clock = clock

    def run_once(self) -> list[str]:
        """한 번 점검하고, 상태가 바뀐 모니터의 알림 문구 목록을 돌려줍니다."""
        events = []
        for name, st in self.state.items():
            m = self.cfg[name]
            r = self.checker(m["url"], timeout=m.get("timeout", 10), expect_status=m.get("expect_status", 200))
            st.last = r
            if r["ok"]:
                st.fails = 0
                if st.up is False:
                    events.append(f"✅ 복구: {name} ({r['status']}, {r['ms']}ms)")
                if st.up is not True:
                    st.changed_at = int(self.clock())
                st.up = True
            else:
                st.fails += 1
                if st.fails >= self.threshold and st.up is not False:
                    events.append(f"🚨 장애: {name} — {r['error']} (연속 {st.fails}회)")
                    st.up = False
                    st.changed_at = int(self.clock())
        return events

    def snapshot(self) -> list[dict]:
        return [{"name": s.name, "url": s.url, "up": s.up, "fails": s.fails, "last": s.last,
                 "changed_at": s.changed_at} for s in self.state.values()]


# ---------------------------------------------------------------- 텔레그램

class Telegram:
    """TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 환경변수를 씁니다. 설정 파일에 토큰을 두지 마세요."""

    def __init__(self, token: str | None = None, chat_id: str | None = None, opener=urllib.request.urlopen):
        self.token = token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id if chat_id is not None else os.environ.get("TELEGRAM_CHAT_ID", "")
        self.opener = opener

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str) -> bool:
        if not self.enabled:
            print("[알림 미설정] " + text)
            return False
        data = urllib.parse.urlencode({"chat_id": self.chat_id, "text": text[:4000]}).encode()
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            with self.opener(urllib.request.Request(url, data=data), timeout=15) as resp:
                return json.loads(resp.read().decode()).get("ok", False)
        except (urllib.error.URLError, OSError, ValueError) as e:
            # 토큰이 들어간 URL은 출력하지 않음
            print(f"[알림 실패] {type(e).__name__}")
            return False


def battery_alert(bat: dict | None, low: int, already_alerted: bool) -> tuple[str | None, bool]:
    """배터리 부족 알림 문구와 다음 알림 상태. 충전을 시작하거나 회복되면 다시 알릴 수 있게 초기화."""
    if not bat or bat.get("percentage") is None:
        return None, already_alerted
    pct, status = bat["percentage"], (bat.get("status") or "").upper()
    charging = status in ("CHARGING", "FULL")
    if pct <= low and not charging:
        if not already_alerted:
            return f"🔋 배터리 부족: {pct}% (충전 필요)", True
        return None, True
    return None, False
