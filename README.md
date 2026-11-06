# termux-pocket-server
안 쓰는 안드로이드폰을 개인 서버로 활용하는 Python·Termux 키트. 상태 대시보드, HTTP 모니터, 텔레그램 알림, 재시작 스크립트

## 기능
| 기능 | 내용 |
|---|---|
| 상태 대시보드 | 가동 시간·부하·메모리·저장공간·배터리(Termux:API)를 웹 페이지와 `/api/status` JSON으로 표시 |
| HTTP 모니터 | 등록한 주소를 주기적으로 점검. **연속 실패 횟수**로 장애를 판정해 일시 오류에는 알리지 않음 |
| 텔레그램 알림 | 장애·복구·배터리 부족을 상태가 바뀔 때 한 번만 알림 (중복 알림 없음) |
| 실행 스크립트 | `pocketctl.sh start/stop/restart/status/logs`, 화면이 꺼져도 동작하도록 wake-lock, 부팅 시 자동 시작 |

표준 라이브러리만 사용합니다. 설치할 패키지가 없습니다.

## 설치 (Termux)
```sh
pkg install python git termux-api      # termux-api는 배터리 정보용(선택). Play/F-Droid의 Termux:API 앱도 필요
git clone <이 저장소 주소> ~/termux-pocket-server
cd ~/termux-pocket-server
bash pocketctl.sh start                   # 처음 실행 시 config.json을 만듭니다
```
폰 브라우저에서 `http://127.0.0.1:8765/` 를 엽니다.

### 설정 (`config.json`)
```json
{ "port": 8765, "check_interval": 60, "fail_threshold": 2, "battery_low": 20,
  "monitors": [{ "name": "내 블로그", "url": "https://example.com/", "expect_status": 200, "timeout": 10 }] }
```
설정을 바꾼 뒤 `bash pocketctl.sh restart`. 모르는 키나 http(s)가 아닌 주소는 시작할 때 오류로 알려줍니다.

### 텔레그램 알림
토큰은 설정 파일이 아닌 **환경변수**로 넣습니다(`~/.bashrc` 등).
```sh
export TELEGRAM_BOT_TOKEN="BotFather에서 받은 토큰"
export TELEGRAM_CHAT_ID="내 채팅 ID"
```
설정하지 않으면 알림 대신 로그(`pocket.log`)에만 기록합니다.

### 다른 기기에서 대시보드 보기
기본은 폰 안(127.0.0.1)에서만 열립니다. 같은 와이파이의 PC에서 보려면 `config.json`의 `host`를 `"0.0.0.0"`으로 바꾸고
`export DASHBOARD_TOKEN="긴 임의 문자열"`을 설정한 뒤 `http://폰IP:8765/?token=...` 으로 접속하세요.
토큰 없이 외부 공개로 실행하면 시작을 거부합니다. 인터넷(공유기 밖)에 그대로 노출하는 것은 권장하지 않습니다.

### 부팅 시 자동 시작
Termux:Boot 앱 설치 → `mkdir -p ~/.termux/boot && cp boot-start.sh ~/.termux/boot/ && chmod +x ~/.termux/boot/boot-start.sh`.
안드로이드 설정에서 Termux의 **배터리 최적화를 해제**해야 오래 살아 있습니다.

## 파일
| 파일 | 역할 |
|---|---|
| `pocket.py` | 상태 수집·HTTP 모니터·텔레그램·배터리 알림 로직 |
| `server.py` | 대시보드 서버 + 백그라운드 모니터 |
| `pocketctl.sh`, `boot-start.sh` | 실행·중지·재시작, 부팅 자동 시작 |
| `config.example.json` | 설정 예시 (`config.json`은 git에서 제외) |
| `test_pocket.py` | 테스트 16개 — `python -m unittest -v` |

## 검증
- 리눅스(Python 3.13)에서 테스트 16개 통과: 설정 검증, 상태 수집, 로컬 웹서버로 장애→복구 판정, 중복 알림 방지,
  텔레그램 요청 형식·토큰 비노출, 대시보드 HTML 이스케이프, 토큰 인증, 외부 공개 시 토큰 강제
- `pocketctl.sh start/status/restart/stop` 동작 확인
- **미검증**: 실제 안드로이드 Termux 기기, Termux:API 배터리 값, 실제 텔레그램 발송
