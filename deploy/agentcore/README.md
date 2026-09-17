# Deploying to AgentCore via AICP

The same KPI tracker, deployed as a LangGraph agent instead of a systemd
service. Read [`../README.md`](../README.md) first if you want the EC2 path —
this one trades isolation for a managed runtime, and the trade is spelled out
below rather than buried.

## What is different from the EC2 deployment

| | EC2 (`deploy/`) | AgentCore (here) |
|---|---|---|
| Credentials | Two MCP server processes, different Unix user, IMDS blocked | **In this container's environment**, from the AICP secret box |
| MCP | A real network boundary | An in-process library seam — same code, no transport |
| Schedule | systemd timer | EventBridge Scheduler → Lambda → `InvokeAgentRuntime` |
| State (history, one-per-week) | Persists on disk | **Ephemeral — see below** |
| Model calls | none | none (the injected virtual key goes unspent) |

AICP does not support MCP today, which is why the credentials sit in the agent
here. Everything else — the counting, the guardrails, the unknown-is-not-zero
contract — is the identical tested code.

## Ephemeral state: the one thing to decide before you rely on this

AgentCore gives each session a fresh microVM. Two files that matter on EC2 do
**not** survive between invocations here:

- `data/history.jsonl` — so the **collapse guardrail never arms**. On EC2 it
  learns what a normal week looks like over 2–3 weeks and then blocks a
  suspicious all-zero table. Here it stays in "note" mode forever.
- the Slack server's one-report-per-week record — so **a second invocation of
  the same week would post again**.

Consequences, and what is already done about them:

1. `create-schedule.sh` sets `MaximumRetryAttempts: 0`. With ephemeral state a
   retry is a duplicate report to the CTO, which cannot be un-sent. A missed
   week is the safer failure: the alert catches it and you re-run by hand.
2. The client-side "already sent this week" skip is also inert. `AGENT_SEND`
   is your manual guard — set it `false` while testing.

If this deployment becomes the primary one, persisting those two files (S3 or
DynamoDB behind `history.py` and the Slack server's state functions) is the
work that makes it as trustworthy as the EC2 path. Until then, EC2 is the one
to schedule and this is the one to invoke on demand.

## 1. Push the repo

AICP deploys from git. The repo already satisfies the template:

```
langgraph.json            -> ./src/agent/graph.py:graph
src/aicp/                 the platform adapter, copied verbatim — do not edit
src/agent/graph.py        one deterministic node, no model call
requirements.txt          the pinned set the injected image installs
kpi_tracker/ kpi_mcp/     the actual logic (381 tests)
```

Verify before pushing — this is the platform's own validator:

```bash
python "<agent-platform>/services/devkit/templates/validate_langgraph.py" .
```

## 2. Deploy from the AICP dashboard

**Agents → Deploy an agent → Framework: LangGraph**, point it at the repo.

Fill the env and secret boxes from [`../../.env.agentcore.example`](../../.env.agentcore.example).
Set `AGENT_SEND=false` for the first deploy so you can invoke it and read the
table without anything reaching the channel.

Model selection is required by the form and goes unused: this agent computes a
KPI table and never asks a model for anything. A number about a named colleague
should not be a model's output.

## 3. Invoke it by hand first

```bash
aws bedrock-agentcore invoke-agent-runtime \
  --agent-runtime-arn <arn> \
  --runtime-session-id "$(python3 -c 'import uuid;print(uuid.uuid4().hex*2)')" \
  --cli-binary-format raw-in-base64-out \
  --payload '{"prompt":"run"}' \
  --content-type application/json --accept application/json out.json && cat out.json
```

`runtimeSessionId` must be **33–256 characters** — a bare uuid4 hex is 32, one
short, which is why the example doubles it.

Pass a date to re-run a past week: `{"prompt":"run 2026-08-28"}`.

## 4. The weekly trigger

AgentCore has no scheduler — it is request/response. The schedule lives in
EventBridge:

```
EventBridge Scheduler  ──Thu 18:00 Asia/Kolkata──▶  Lambda  ──▶  InvokeAgentRuntime
```

```bash
# 1. the Lambda (invoke_lambda.py), python3.12, handler invoke_lambda.handler
#    env: AGENT_RUNTIME_ARN, optionally ALERT_WEBHOOK_URL
#    role: iam-lambda-policy.json
# 2. the scheduler's role: iam-scheduler-role.json  (trust) + lambda:InvokeFunction
# 3. the schedule
LAMBDA_ARN=arn:aws:lambda:eu-north-1:...:function:kpi-weekly-invoke \
SCHEDULER_ROLE_ARN=arn:aws:iam::...:role/kpi-scheduler \
bash create-schedule.sh
```

EventBridge Scheduler understands timezones natively, so the schedule says what
it means — `cron(0 18 ? * THU *)` in `Asia/Kolkata` — rather than being
converted to UTC by hand as the systemd unit has to be.

`ALERT_WEBHOOK_URL` is worth setting. Without it, a failed weekly run is only
visible in CloudWatch, and silence looks exactly like success.

## Verify

```bash
aws scheduler get-schedule --name kpi-weekly --region eu-north-1
aws lambda invoke --function-name kpi-weekly-invoke --payload '{}' /dev/stdout
aws logs tail /aws/bedrock-agentcore/runtimes/<runtime-id> --follow
```
