#!/usr/bin/env bash
# Pull, install, restart, verify. Idempotent.
#
# Run on the server, from the Randy install directory:
#   cd ~/randy && ./scripts/deploy.sh

set -euo pipefail

cd "$(dirname "$0")/.."
ROOT=$(pwd)

echo "→ git pull (in $ROOT)"
git pull --ff-only

echo "→ pip install"
"$ROOT/.venv/bin/pip" install -q -e '.[dev]'

echo "→ restart services"
systemctl --user restart randy.service randy-web.service

# Give them a moment to come up before checking.
sleep 3

echo "→ status"
if systemctl --user is-active --quiet randy.service randy-web.service; then
  echo "  ✓ both services active"
else
  echo "  ✗ at least one service is not active:"
  systemctl --user --no-pager status randy.service randy-web.service | tail -30
  exit 1
fi

echo "→ recent logs (last 5 lines per service):"
journalctl --user -u randy -n 5 --no-pager
echo
journalctl --user -u randy-web -n 5 --no-pager

echo
echo "Done. Web at http://$(hostname -I | awk '{print $1}'):${WEB_PORT:-8000}"
