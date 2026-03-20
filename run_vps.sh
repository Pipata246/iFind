#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [[ -f ".venv/bin/activate" ]]; then
  source .venv/bin/activate
elif [[ -f "venv/bin/activate" ]]; then
  source venv/bin/activate
fi

# Example:
# ./run_vps.sh --mode wb --keyword "iPhone" --model "13" --price-min 22000 --price-max 24000 --precision 7 --headless
python3 main.py "$@"
