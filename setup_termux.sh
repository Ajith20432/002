#!/usr/bin/env bash
# One-shot Termux setup for ZORA. Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"

pkg update -y
pkg install -y python python-numpy git
# pandas: use the Termux package when it exists, otherwise build it with pip
pkg install -y python-pandas || pip install pandas
pip install --upgrade pip
pip install "ccxt>=4.3.0" "python-dotenv>=1.0.0" "requests>=2.31.0"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example (paper mode). Edit it before running."
fi

python bot.py doctor || true
echo
echo "Next:  python bot.py learn   |   python bot.py run"
echo "Optional FastAPI dashboard (needs a Rust toolchain on Termux): pip install fastapi uvicorn"
