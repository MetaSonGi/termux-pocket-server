#!/data/data/com.termux/files/usr/bin/env bash
# 사용법: bash pocketctl.sh start | stop | restart | status | logs
# (웹 업로드로 받은 경우 실행 권한이 없으므로 항상 bash로 실행)
set -euo pipefail
cd "$(dirname "$0")"
PID_FILE=".pocket.pid"
LOG_FILE="pocket.log"
PY="$(command -v python3 || command -v python)"

running() { [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; }

start() {
  if running; then echo "이미 실행 중 (PID $(cat "$PID_FILE"))"; return 0; fi
  [ -f config.json ] || { cp config.example.json config.json; echo "config.json을 만들었습니다. 모니터 주소를 수정하세요."; }
  command -v termux-wake-lock >/dev/null && termux-wake-lock || true   # 화면이 꺼져도 CPU 유지
  nohup "$PY" server.py >>"$LOG_FILE" 2>&1 &
  echo $! >"$PID_FILE"
  sleep 1
  if running; then echo "시작됨 (PID $(cat "$PID_FILE")) · 로그: $LOG_FILE"; else echo "시작 실패 — $LOG_FILE 확인"; tail -n 20 "$LOG_FILE"; exit 1; fi
}

stop() {
  if running; then
    kill "$(cat "$PID_FILE")"
    for _ in 1 2 3 4 5; do running || break; sleep 1; done
    running && kill -9 "$(cat "$PID_FILE")" || true
    echo "중지됨"
  else
    echo "실행 중이 아님"
  fi
  rm -f "$PID_FILE"
  command -v termux-wake-unlock >/dev/null && termux-wake-unlock || true
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  status) if running; then echo "실행 중 (PID $(cat "$PID_FILE"))"; else echo "중지됨"; exit 1; fi ;;
  logs) tail -n 50 -f "$LOG_FILE" ;;
  *) echo "사용법: $0 {start|stop|restart|status|logs}"; exit 2 ;;
esac
