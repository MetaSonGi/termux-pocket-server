#!/data/data/com.termux/files/usr/bin/sh
# Termux:Boot 앱용: 이 파일을 ~/.termux/boot/ 에 복사하면 폰 재부팅 후 자동 시작합니다.
# 저장소 위치가 다르면 아래 경로를 고치세요.
cd "$HOME/termux-pocket-server" && bash pocketctl.sh start
