#!/bin/bash
# Start both MCP servers locally for development.
#
# Reads .env (KPI_SECRET_BACKEND=env), so no AWS account is needed. On EC2 the
# servers run under systemd and read AWS Secrets Manager instead -- see
# deploy/README.md.
#
#   ./scripts/dev-servers.sh          start both, log to .dev-logs/
#   ./scripts/dev-servers.sh stop     stop both
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ "${1:-start}" == "stop" ]]; then
  pkill -f "kpi_mcp.linkedin_server" || true
  pkill -f "kpi_mcp.slack_server" || true
  echo "stopped"
  exit 0
fi

mkdir -p .dev-logs
uv run python -m kpi_mcp.linkedin_server --port 8801 > .dev-logs/linkedin.log 2>&1 &
uv run python -m kpi_mcp.slack_server --port 8802 > .dev-logs/slack.log 2>&1 &

for log in .dev-logs/linkedin.log .dev-logs/slack.log; do
  until grep -qi "uvicorn running" "$log" 2>/dev/null; do sleep 0.5; done
done

echo "kpi-linkedin  http://127.0.0.1:8801/mcp"
echo "kpi-slack     http://127.0.0.1:8802/mcp"
echo
echo "Next:  uv run kpi-tracker check"
echo "Logs:  tail -f .dev-logs/*.log"
