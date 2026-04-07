#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
/root/sneaker_bot/venv/bin/playwright install chromium --with-deps
