#!/bin/bash
# Install the KPI tracker on this EC2 host. Run it ON the box, as root.
#
#   sudo REPO_URL=https://github.com/<you>/<repo>.git bash install.sh
#
# Idempotent: safe to re-run after a code change or a failed attempt.
#
# It does NOT touch AWS. Creating the two secrets and attaching the IAM policy
# happen from your laptop with your own credentials -- see README.md steps 1-2.
# This script only sets up the machine.
set -euo pipefail

REPO_URL="${REPO_URL:-}"
APP_DIR="${APP_DIR:-/opt/kpi-tracker}"
SLACK_DESTINATION="${SLACK_DESTINATION:-C0BV21PC28P}"
AWS_REGION="${AWS_REGION:-us-east-1}"

say() { printf '\n== %s\n' "$*"; }

# --- preflight: fail early and say exactly what is wrong --------------------
say "Checking the host"
[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
[[ -n "$REPO_URL" ]] || { echo "set REPO_URL=https://github.com/.../repo.git"; exit 1; }
command -v systemctl >/dev/null || { echo "this script expects systemd"; exit 1; }

# This is a SHARED host. If something already owns these ports, stop rather
# than fight it -- the ports are configurable in config.json and the units.
for port in 8801 8802; do
  if ss -lntp 2>/dev/null | grep -q ":$port "; then
    echo "port $port is already in use on this host:"
    ss -lntp | grep ":$port "
    echo "pick different ports in the .service files and config.json, then re-run"
    exit 1
  fi
done
echo "  ports 8801/8802 free · $(uname -m) · $(. /etc/os-release && echo "$PRETTY_NAME")"

# --- two users, which is what makes the isolation real ---------------------
# kpi-mcp holds the credentials; kpi-client runs the weekly job and must not be
# able to read them. Same-uid processes can read each other's memory and
# environment, so this split is load-bearing, not cosmetic.
say "Creating service users"
for user in kpi-mcp kpi-client; do
  id -u "$user" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin "$user"
  echo "  $user (uid $(id -u "$user"))"
done

# --- code ------------------------------------------------------------------
say "Installing the code into $APP_DIR"
if [[ -d "$APP_DIR/.git" ]]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$APP_DIR"
fi
# root owns the code: neither service may modify what it runs.
chown -R root:root "$APP_DIR"

if ! command -v uv >/dev/null; then
  echo "  installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/usr/local/bin sh
fi
# --frozen: exactly the versions in uv.lock, no resolution on the box.
(cd "$APP_DIR" && uv sync --frozen)

say "Creating the two writable paths"
# history.jsonl -- the guardrail baseline and the one-report-per-week record.
install -d -o kpi-client -g kpi-client "$APP_DIR/data"
# the Slack server's own duplicate-send record.
install -d -o kpi-mcp -g kpi-mcp /var/lib/kpi-mcp

say "Writing config.json"
# Config holds no credentials -- only the addresses of the two local servers.
# It is gitignored, so it is written here rather than pulled.
cat > "$APP_DIR/config.json" <<JSON
{
  "timezone": "Asia/Kolkata",
  "week_start_day": "thursday",
  "members_path": "data/members.json",
  "history_path": "data/history.jsonl",
  "mcp": {
    "linkedin_url": "http://127.0.0.1:8801/mcp",
    "slack_url": "http://127.0.0.1:8802/mcp"
  },
  "linkedin": { "source": "mcp", "target_posts_per_week": 3 },
  "blog": { "base_url": "https://www.moring.ai" },
  "slack": { "destination_label": "#marketing-kpi" }
}
JSON
chown root:root "$APP_DIR/config.json"

# A credential on the client would defeat the whole design. `kpi-tracker check`
# fails if it finds one; delete the file so it cannot.
rm -f "$APP_DIR/.env"

say "Installing the systemd units"
sed -e "s|^Environment=AWS_REGION=.*|Environment=AWS_REGION=$AWS_REGION|" \
    -e "s|^Environment=KPI_SLACK_DESTINATION=.*|Environment=KPI_SLACK_DESTINATION=$SLACK_DESTINATION|" \
    "$APP_DIR/deploy/kpi-slack-mcp.service" > /etc/systemd/system/kpi-slack-mcp.service
sed -e "s|^Environment=AWS_REGION=.*|Environment=AWS_REGION=$AWS_REGION|" \
    "$APP_DIR/deploy/kpi-linkedin-mcp.service" > /etc/systemd/system/kpi-linkedin-mcp.service
cp "$APP_DIR/deploy/kpi-weekly.service" \
   "$APP_DIR/deploy/kpi-weekly-failed.service" \
   "$APP_DIR/deploy/kpi-weekly.timer" /etc/systemd/system/

systemctl daemon-reload
systemctl enable --now kpi-linkedin-mcp kpi-slack-mcp
systemctl enable --now kpi-weekly.timer

say "Blocking instance metadata for the client user"
# EC2 metadata is UNAUTHENTICATED -- AWS's own docs say any software on the
# instance can read it. Without this, the weekly job could ask IMDS for the
# instance role and fetch the very secrets the servers hold on its behalf, and
# the process split would prove nothing. Scoped to kpi-client's uid, so nothing
# else on this shared host is affected.
CLIENT_USER=kpi-client bash "$APP_DIR/deploy/block-imds-for-client.sh"

say "Done. Verifying"
systemctl --no-pager --lines=0 status kpi-linkedin-mcp kpi-slack-mcp | grep -E 'Loaded|Active' || true
echo
systemctl list-timers kpi-weekly.timer --no-pager || true
echo
echo "Next:"
echo "  sudo -u kpi-client $APP_DIR/.venv/bin/kpi-tracker check      # should be all OK"
echo "  sudo -u kpi-client $APP_DIR/.venv/bin/kpi-tracker run        # dry run, sends nothing"
echo "  sudo -u kpi-client $APP_DIR/.venv/bin/kpi-tracker run --send # the real thing"
