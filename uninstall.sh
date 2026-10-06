#!/data/data/com.termux/files/usr/bin/bash
# V4Z AI Route uninstaller
# by V4Z RASHD | https://t.me/rashdteem
set -e

rm -f "$PREFIX/bin/v4zroute"
echo "[*] v4zroute command removed."
echo "[i] Your config (~/.v4zroute) was kept. Delete it with:  rm -rf ~/.v4zroute"
