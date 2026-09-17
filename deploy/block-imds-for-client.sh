#!/bin/bash
# Make the secret isolation REAL rather than nominal.
#
# THE PROBLEM
# -----------
# EC2 instance metadata is unauthenticated: AWS's own documentation says it is
# readable by any software running on the instance. So by default, the weekly
# job could simply ask IMDS for the instance role's credentials and read the
# very secrets the MCP servers were built to hold on its behalf. Splitting the
# processes would look like isolation while providing none.
#
# THE FIX
# -------
# Block the IMDS address for the client's uid specifically. The MCP servers
# (running as kpi-mcp) keep their access; the weekly job (kpi-client) does not.
#
# Verify it afterwards with the acceptance test at the bottom -- do not take
# this script's word for it.
set -euo pipefail

CLIENT_USER="${CLIENT_USER:-kpi-client}"
IMDS_ADDRESS="169.254.169.254"

if ! id -u "$CLIENT_USER" >/dev/null 2>&1; then
  echo "user $CLIENT_USER does not exist; create it first" >&2
  exit 1
fi

CLIENT_UID="$(id -u "$CLIENT_USER")"

# -A OUTPUT applies to locally generated packets; --uid-owner scopes it to one
# user. REJECT rather than DROP so the client fails fast with a clear error
# instead of hanging until a timeout.
iptables -C OUTPUT -m owner --uid-owner "$CLIENT_UID" -d "$IMDS_ADDRESS" -j REJECT 2>/dev/null \
  || iptables -A OUTPUT -m owner --uid-owner "$CLIENT_UID" -d "$IMDS_ADDRESS" -j REJECT

echo "blocked $IMDS_ADDRESS for $CLIENT_USER (uid $CLIENT_UID)"

# Persist across reboots. Without this the rule silently disappears and the
# isolation quietly reverts on the next restart.
if command -v netfilter-persistent >/dev/null 2>&1; then
  netfilter-persistent save
  echo "rule persisted via netfilter-persistent"
else
  echo "WARNING: iptables-persistent is not installed, so this rule will be" >&2
  echo "         LOST ON REBOOT. Install it:  apt-get install iptables-persistent" >&2
fi

cat <<'ACCEPTANCE'

--- ACCEPTANCE TEST: run these two commands and read the results ---

  # 1. The client must NOT be able to reach instance metadata.
  sudo -u kpi-client curl -s --max-time 3 http://169.254.169.254/latest/meta-data/ \
    && echo "FAIL: the client can still read IMDS" \
    || echo "PASS: the client cannot read IMDS"

  # 2. The servers still must be able to.
  sudo -u kpi-mcp curl -s --max-time 3 -o /dev/null -w '%{http_code}\n' \
    http://169.254.169.254/latest/api/token -X PUT \
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 60'
  # ...expect 200. Anything else and the servers cannot fetch their secrets.

  # 3. And the end-to-end proof, which is the one that matters:
  sudo -u kpi-client /opt/kpi-tracker/.venv/bin/kpi-tracker check
  # ...expect "client is clean OK" and both servers OK.
ACCEPTANCE
