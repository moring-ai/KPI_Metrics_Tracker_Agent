# Deploying to AWS Lambda

Weekly report as a single Lambda, triggered Thursday 11:00 IST.

```
EventBridge Scheduler ──Thu 11:00 IST──▶ kpi-weekly (Lambda)
                                              │
                          ┌───────────────────┼───────────────────┐
                          ▼                   ▼                   ▼
                  Secrets Manager       Bright Data          S3 (state)
                  (2 secrets)           + moring.ai RSS      + Slack
```

Everything below is copy-pasteable. Replace `ACCOUNT_ID` and `BUCKET`.

## Two ways to ship the code — pick one

| | Container image | Zip |
|---|---|---|
| Deploy | **one script** | ~6 aws commands |
| Needs Docker | yes | no |
| Size | 845 MB (limit 10 GB) | 9 MB (limit 50 MB) |
| Wheels | correct by construction — pip runs inside the target image | forced with `--platform`; a plain `pip install` on a Mac silently produces macOS binaries |
| Test locally | **yes** — you can run the real image | no |

**Use the container** unless you have no Docker. It is one command, and being
able to run the exact artefact on your laptop is worth more than the size
difference.

```bash
BUCKET=your-bucket bash deploy/lambda/deploy.sh
```

That builds the image, pushes it to ECR, creates the IAM role, creates or
updates the function, and turns off Lambda's duplicate retry layer. Re-running
it just ships new code. It starts with `AGENT_SEND=false`, so nothing reaches
Slack until you say so.

The zip route is steps 3–6 below and is still fully supported.

## Before you start

Two things are worth doing first because they are awkward to change later.

1. **Rotate the Bright Data key.** It has been in a chat transcript; creating
   the secret from the old value just moves the exposure into AWS.
2. **Decide the S3 bucket.** Lambda's filesystem is read-only and thrown away
   after every invocation, so two small files must live in S3:

   | File | Without it |
   |---|---|
   | `history.jsonl` | the collapse guardrail never learns what a normal week looks like, so it can never block a suspicious all-zero table |
   | `slack-sent.json` | the "already reported this week" check is always false, so a retry posts the report to the CTO twice |

   `lambda_handler` refuses to run if the history path is not an `s3://` URI,
   rather than discovering it after the vendor call.

## What is different from the EC2 deployment

**The function holds both tokens.** On EC2 the LinkedIn and Slack MCP servers
are separate processes under a different Unix user that the weekly job cannot
read. A Lambda is one process, so the same servers run in-process — identical
code, no transport — and the credentials are in this function's memory.
Secrets Manager still holds the values and the execution role fetches them, but
the process isolation is gone. That is inherent to Lambda, not a shortcut.

---

## 1. Create the two secrets

```bash
aws secretsmanager create-secret --region us-east-1 \
  --name kpi/brightdata-api-token --secret-string 'YOUR_ROTATED_KEY'

aws secretsmanager create-secret --region us-east-1 \
  --name kpi/slack-bot-token --secret-string 'xoxb-YOUR-BOT-TOKEN'
```

Two separate secrets, so the IAM policy names two ARNs and nothing wider.
$0.40 each per month. The servers cache values for an hour, which is also what
makes rotation take effect without a redeploy.

## 2. Create the S3 prefix

```bash
aws s3api put-object --bucket BUCKET --key kpi-tracker/ --region us-east-1
```

Any existing bucket is fine — this is two small JSON files.

## 3. Build the zip *(zip route only — skip if you used deploy.sh)*

```bash
bash deploy/lambda/build.sh
```

Produces `dist/kpi-tracker-lambda.zip`, about **9 MB zipped / 32 MB unzipped**
(limits are 50 MB direct upload and 250 MB unzipped, so no S3 upload needed).

The script forces **Linux aarch64** wheels. That matters: `pydantic-core` is a
compiled Rust extension, and a plain `pip install` on a Mac produces macOS
binaries that fail at import inside Lambda with an unhelpful error. Verify if
you like:

```bash
unzip -p dist/kpi-tracker-lambda.zip \
  pydantic_core/_pydantic_core.cpython-312-aarch64-linux-gnu.so | file -
# expect: ELF 64-bit LSB shared object, ARM aarch64
```

## 4. Create the execution role *(zip route only)*

```bash
cat > /tmp/trust.json <<'JSON'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
 "Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}
JSON

aws iam create-role --role-name kpi-weekly-lambda \
  --assume-role-policy-document file:///tmp/trust.json

sed -e "s/ACCOUNT_ID/<your-account-id>/g" -e "s/BUCKET/<your-bucket>/g" \
  deploy/lambda/iam-policy.json > /tmp/kpi-policy.json

aws iam put-role-policy --role-name kpi-weekly-lambda \
  --policy-name kpi-weekly --policy-document file:///tmp/kpi-policy.json
```

## 5. Create the function *(zip route only)*

```bash
aws lambda create-function \
  --function-name kpi-weekly \
  --region us-east-1 \
  --runtime python3.12 \
  --architectures arm64 \
  --handler lambda_handler.handler \
  --role arn:aws:iam::ACCOUNT_ID:role/kpi-weekly-lambda \
  --zip-file fileb://dist/kpi-tracker-lambda.zip \
  --timeout 900 \
  --memory-size 512
```

**`--timeout 900` is the maximum Lambda allows**, and it is the reason for the
`KPI_VENDOR_POLL_TIMEOUT` setting in the next step.

`arm64` is cheaper than x86 and is what the zip was built for. Memory is
generous for what this does — the job is HTTP calls and arithmetic — but more
memory also means more CPU, which shortens the cold start.

## 6. Set the environment *(zip route only)*

```bash
aws lambda update-function-configuration \
  --function-name kpi-weekly --region us-east-1 \
  --environment 'Variables={
    KPI_SECRET_BACKEND=aws,
    KPI_LINKEDIN_SECRET_ID=kpi/brightdata-api-token,
    KPI_SLACK_SECRET_ID=kpi/slack-bot-token,
    KPI_SLACK_DESTINATION=C0BV21PC28P,
    KPI_HISTORY_PATH=s3://BUCKET/kpi-tracker/history.jsonl,
    KPI_SLACK_STATE_PATH=s3://BUCKET/kpi-tracker/slack-sent.json,
    KPI_VENDOR_POLL_TIMEOUT=780,
    AGENT_SEND=false
  }'
```

Two of these deserve a moment:

**`KPI_VENDOR_POLL_TIMEOUT=780`** — everywhere else in this system the inner
timeout fires before the outer one, so a slow vendor produces a readable
*"snapshot not ready after Ns"* rather than an opaque kill. On Lambda the outer
timeout is the function's own 900s cap, which is *below* this project's 1800s
default. 780s keeps the ordering: the poll gives up, the report renders an em
dash with a reason, and the function returns normally. The handler logs a loud
`MISCONFIGURED` error if you get this wrong.

For context: measured vendor collections were 1m35s–9m39s, so 13 minutes is
real headroom — but less than the 30 minutes EC2 allows.

**`AGENT_SEND=false`** — start here. Nothing reaches the channel until you
change it.

## 7. Dry run

```bash
aws lambda invoke --function-name kpi-weekly --region us-east-1 \
  --payload '{"send":false}' --cli-binary-format raw-in-base64-out \
  /tmp/kpi-out.json > /dev/null && python3 -m json.tool /tmp/kpi-out.json
```

(The response goes to a file rather than `/dev/stdout`: the CLI also writes its
own status JSON to stdout, so piping both into a parser fails with
`Extra data: line 1 column 51`, which looks like your function broke when it
did not.)

Expect a JSON summary with a row per person. Check the numbers before going
further. Logs:

```bash
aws logs tail /aws/lambda/kpi-weekly --follow --region us-east-1
```

A specific past week: `--payload '{"week":"2026-09-10","send":false}'`.

## 8. Send it for real

```bash
aws lambda update-function-configuration --function-name kpi-weekly \
  --region us-east-1 --environment 'Variables={...same as above but AGENT_SEND=true}'

aws lambda invoke --function-name kpi-weekly --region us-east-1 \
  --payload '{}' --cli-binary-format raw-in-base64-out /tmp/kpi-out.json \
  > /dev/null && python3 -m json.tool /tmp/kpi-out.json
```

Re-running the same week is a no-op: the S3 record makes the second invocation
return `already_reported` without fetching anything.

## 9. The weekly schedule

```bash
# the role EventBridge assumes to invoke the function
aws iam create-role --role-name kpi-scheduler \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
    "Principal":{"Service":"scheduler.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
aws iam put-role-policy --role-name kpi-scheduler --policy-name invoke \
  --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
    "Action":"lambda:InvokeFunction",
    "Resource":"arn:aws:lambda:us-east-1:ACCOUNT_ID:function:kpi-weekly"}]}'

LAMBDA_ARN=arn:aws:lambda:us-east-1:ACCOUNT_ID:function:kpi-weekly \
SCHEDULER_ROLE_ARN=arn:aws:iam::ACCOUNT_ID:role/kpi-scheduler \
bash deploy/lambda/create-schedule.sh

# Turn OFF Lambda's own async retries, so exactly one layer retries.
# EventBridge invokes Lambda asynchronously, so Lambda adds its own 2 retries
# on top of the schedule's -- up to nine executions from one weekly trigger.
aws lambda put-function-event-invoke-config --function-name kpi-weekly \
  --region us-east-1 --maximum-retry-attempts 0
```

EventBridge Scheduler understands timezones natively, so the schedule reads
`cron(0 11 ? * THU *)` in `Asia/Kolkata` rather than being converted to UTC by
hand the way the systemd unit has to be.

Retries are **on** here (unlike the AgentCore schedule), because the S3 state
survives between invocations and makes a repeat a no-op. The extra command
above disables Lambda's *second*, independent retry layer so the behaviour is
the one written in the schedule rather than a multiple of it.

## 10. Alarm on failure — do not skip this

On EC2 a failed run trips a systemd `OnFailure` unit that posts an alert.
Lambda's equivalent is the function's own `Errors` metric, which is why the
handler **raises** whenever no report reached the channel. Without an alarm,
the schedule fires into silence and silence looks exactly like success.

```bash
aws cloudwatch put-metric-alarm --region us-east-1 \
  --alarm-name kpi-weekly-failed \
  --metric-name Errors --namespace AWS/Lambda --statistic Sum \
  --dimensions Name=FunctionName,Value=kpi-weekly \
  --period 3600 --evaluation-periods 1 --threshold 1 \
  --comparison-operator GreaterThanOrEqualToThreshold \
  --treat-missing-data notBreaching \
  --alarm-actions arn:aws:sns:us-east-1:ACCOUNT_ID:YOUR_TOPIC
```

## Shipping a change

**Container:** `BUCKET=your-bucket bash deploy/lambda/deploy.sh`

**Zip:**

```bash
bash deploy/lambda/build.sh
aws lambda update-function-code --function-name kpi-weekly --region us-east-1 \
  --zip-file fileb://dist/kpi-tracker-lambda.zip
aws lambda invoke --function-name kpi-weekly --region us-east-1 \
  --payload '{"send":false}' --cli-binary-format raw-in-base64-out /tmp/kpi-out.json \
  > /dev/null && python3 -m json.tool /tmp/kpi-out.json
```

Run `uv run pytest -q` first — 439 tests, offline, under a second.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `Runtime.ImportModuleError` on `pydantic_core` | the zip was built with macOS wheels; rebuild with `build.sh` |
| `MISCONFIGURED` in the logs | `KPI_VENDOR_POLL_TIMEOUT` is ≥ the function timeout |
| Raises about `s3://` at startup | `KPI_HISTORY_PATH` is not set to an S3 URI |
| Every count is an em dash | a source failed — the reason is in the report notes and the logs |
| `Task timed out after 900.00 seconds` | the vendor took longer than the poll ceiling *and* the ceiling was set too high to fire first |
| `AccessDenied` on GetObject, first run | the role is missing `s3:ListBucket`; without it S3 answers a *missing* key with 403 rather than 404 |
| `PackageNotFoundError` for httpx2 | the zip was built without `*.dist-info`; rebuild with the current `build.sh` |
| `Extra data: line 1 column 51` | the invoke response was piped to a parser; write it to a file first (step 7) |
| The CTO got the report twice | Lambda's own async retries are still on; run the `put-function-event-invoke-config` command in step 9 |
