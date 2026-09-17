#!/bin/bash
# LOCAL (macOS) option only. For the EC2 deployment use deploy/ instead --
# systemd units, IAM policy and the IMDS block live there.
#
# Wrapper for the scheduled run. launchd and cron start with almost no
# environment, so everything the job needs is set up explicitly here.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

# NO credentials are sourced here, deliberately. The client holds none: the
# MCP servers hold the tokens and fetch them from AWS Secrets Manager. If you
# find yourself adding `source .env` back, the isolation has been undone --
# `kpi-tracker check` fails if a token is set in this process.

mkdir -p logs
exec /opt/homebrew/bin/uv run kpi-tracker run --send >> logs/weekly.log 2>&1
