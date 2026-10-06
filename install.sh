#!/data/data/com.termux/files/usr/bin/bash
# V4Z AI Route installer (Termux)
# by V4Z RASHD | https://t.me/rashdteem
set -e

echo "[*] V4Z AI Route installer"

if ! command -v python >/dev/null 2>&1 && ! command -v python3 >/dev/null 2>&1; then
  echo "[*] Installing python..."
  pkg install -y python
fi

DIR="$(cd "$(dirname "$0")" && pwd)"
if [ ! -f "$DIR/v4zroute.py" ]; then
  echo "[!] v4zroute.py not found next to install.sh."
  echo "    Extract the whole package first, then run install.sh from inside it."
  exit 1
fi

cp "$DIR/v4zroute.py" "$PREFIX/bin/v4zroute"
chmod +x "$PREFIX/bin/v4zroute"
mkdir -p "$HOME/.v4zroute"

cat <<'BANNER'

  V4Z AI Route installed.
  1) Edit your config:   nano ~/.v4zroute/config.json
     (put YOUR provider base URLs + API keys there)
  2) Check providers:    v4zroute test
  3) Run the gateway:    v4zroute serve

  Then point any OpenAI-compatible app at:
     http://127.0.0.1:8787/v1   with model "auto"

  Channel: https://t.me/rashdteem
BANNER
