# Deploying on EC2

Target: `i-0f954ca45277e011b` (**prudential-demo-host**), `us-east-1`,
account `603011031216`, instance role `prudential-demo-ec2-role`.

Runs **Thursday 11:00 IST** (`05:30 UTC`).

```
┌─ kpi-client  (no credentials, cannot reach AWS) ────────────────┐
│  kpi-weekly.timer  ->  kpi-tracker run --send                   │
│         │ 127.0.0.1:8801        │ 127.0.0.1:8802                │
└─────────┼──────────────────────┼───────────────────────────────┘
          v                      v
┌─ kpi-mcp  (holds the credentials) ──────────────────────────────┐
│  kpi-linkedin-mcp          kpi-slack-mcp                        │
│         └──── AWS Secrets Manager (via the instance role) ──────┘
└─────────────────────────────────────────────────────────────────┘
```

The blog source needs no credential — it reads the site's public RSS feed — so
it stays inside the client.

## This is a shared host. Three things follow.

1. **The IAM policy is scoped to two secret ARNs.** `prudential-demo-ec2-role`
   is used by other workloads on this box; it must not gain broad Secrets
   Manager access. Check [`iam-policy.json`](iam-policy.json) before attaching.
2. **The IMDS block is scoped to one uid.** It rejects `169.254.169.254` for
   `kpi-client` only, so nothing else on the host loses metadata access.
3. **Ports 8801/8802** are loopback-only. `install.sh` refuses to run if
   anything already holds them rather than fighting for them.

## 1. Create the two secrets (from your laptop)

Separate secrets, so each server's policy names exactly one ARN — the LinkedIn
server physically cannot read the Slack token.

```bash
aws secretsmanager create-secret --region us-east-1 \
  --name kpi/brightdata-api-token --secret-string 'YOUR_ROTATED_BRIGHTDATA_KEY'

aws secretsmanager create-secret --region us-east-1 \
  --name kpi/slack-bot-token --secret-string 'xoxb-YOUR-BOT-TOKEN'
```

$0.40/secret/month, so **$0.80/month**. The servers cache each value for an
hour, which is also what makes rotation work without a restart.

## 2. Attach the policy to the existing role

```bash
aws iam put-role-policy --role-name prudential-demo-ec2-role \
  --policy-name kpi-tracker-secrets \
  --policy-document file://deploy/iam-policy.json
```

`put-role-policy` adds an inline policy; it does not replace the role's
existing permissions.

## 3. Install, on the box

```bash
sudo REPO_URL=https://github.com/<you>/<repo>.git bash deploy/install.sh
```

One script, idempotent, safe to re-run. It creates the two users, installs the
code under `/opt/kpi-tracker` with `uv sync --frozen`, writes `config.json`,
installs and starts the units, and applies the IMDS block. It does not touch
AWS.

## 4. Verify, in this order

```bash
# credentials reachable by the servers, and NOT by the client
sudo -u kpi-client /opt/kpi-tracker/.venv/bin/kpi-tracker check

# the acceptance test that makes the isolation real rather than nominal
sudo -u kpi-client curl -s --max-time 3 http://169.254.169.254/latest/meta-data/ \
  && echo "FAIL: the client can still read IMDS" || echo "PASS"

# a dry run: real vendor call, real numbers, nothing sent
sudo -u kpi-client /opt/kpi-tracker/.venv/bin/kpi-tracker -v run

# then, once the table looks right
sudo -u kpi-client /opt/kpi-tracker/.venv/bin/kpi-tracker run --send

systemctl list-timers kpi-weekly.timer
```

`check` is stronger than a credential check: it asks each server whether it can
actually reach its secret and whether it publishes the schema the client is
about to rely on — without the secret crossing the boundary. It also **fails**
if it finds a token in the client's own environment.

## 5. Reading what happened

```bash
journalctl -u kpi-weekly.service -n 100        # last run
journalctl -u kpi-linkedin-mcp.service -f      # a server, live
journalctl -u kpi-weekly.service --since '3 weeks ago' | grep -i guardrail
```

Every log line passes through a redacting formatter covering messages,
arguments **and tracebacks**, so a credential in an exception cannot reach the
journal.

## On the timing

`05:30 UTC` = **11:00 IST** = **01:30 US Eastern**.

The CTO is on US Eastern, so he receives it at 01:30 and reads it when he wakes
— still ahead of Friday standup. If you would rather it arrive during his
morning, `12:30 UTC` puts it at 18:00 IST / 08:30 ET. One line in
[`kpi-weekly.timer`](kpi-weekly.timer).

The fire time only changes *when* it is sent. The seven days it measures come
from `config.timezone`, so the numbers are identical whenever it runs, and
re-running mid-week is safe.

## Shipping a change

```bash
cd /opt/kpi-tracker
sudo git pull && sudo uv sync --frozen
sudo uv run pytest -q                    # 395 tests, offline, under a second
sudo systemctl restart kpi-linkedin-mcp kpi-slack-mcp
sudo -u kpi-client /opt/kpi-tracker/.venv/bin/kpi-tracker check
```

Restarting a server mid-week is safe: the client only talks to them during the
weekly run, and a failed call renders an em dash with a reason rather than a
wrong number.

## What to expect the first few weeks

The collapse guardrail — which blocks a suspicious all-zero table — needs 2–3
weeks of history before it can tell a broken scraper from a quiet week. Until
then it only *notes*. Glance at the first couple of reports rather than
trusting them blind.

## Known limitation

One roster member's LinkedIn profile (`balajinagarajkumar`) cannot be scraped:
`crawl_error: Minimal layout detected`, reproduced seven times with colleagues
succeeding in the same request. He renders `—` with the reason stated. That is
a vendor-side problem, not a deployment one.
