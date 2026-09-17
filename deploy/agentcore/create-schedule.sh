#!/bin/bash
# Create the weekly EventBridge Schedule that invokes the KPI agent.
#
# Thursday 11:00 Asia/Kolkata, matching the EC2 timer (deploy/kpi-weekly.timer).
# That reaches the CTO at 01:30 US Eastern, so he sees it when he wakes on
# Thursday -- still ahead of Friday standup.
#
# Unlike systemd, EventBridge Scheduler understands timezones natively, so the
# schedule is written in the timezone we actually mean rather than converted to
# UTC by hand. It also follows daylight saving on the reader's side correctly.
# NOTE ON RETRIES: MaximumRetryAttempts is 0 on purpose. AgentCore containers
# are ephemeral, so the "already reported this week" state does not survive
# between invocations -- a retry would post the report to the channel a second
# time. A missed week is caught by the failure alert and re-run by hand; a
# duplicate report to the CTO cannot be un-sent. See README.md "Ephemeral state".
set -euo pipefail

: "${LAMBDA_ARN:?set LAMBDA_ARN to the ARN of the invoke lambda}"
: "${SCHEDULER_ROLE_ARN:?set SCHEDULER_ROLE_ARN (see iam-scheduler-role.json)}"
NAME="${NAME:-kpi-weekly}"
REGION="${AWS_REGION:-us-east-1}"

aws scheduler create-schedule \
  --name "$NAME" \
  --region "$REGION" \
  --schedule-expression 'cron(0 11 ? * THU *)' \
  --schedule-expression-timezone 'Asia/Kolkata' \
  --flexible-time-window '{"Mode":"FLEXIBLE","MaximumWindowInMinutes":15}' \
  --target "{
    \"Arn\": \"$LAMBDA_ARN\",
    \"RoleArn\": \"$SCHEDULER_ROLE_ARN\",
    \"Input\": \"{}\",
    \"RetryPolicy\": {\"MaximumRetryAttempts\": 0}
  }" \
  --description 'Weekly LinkedIn + blog KPI report, Thursday 11:00 IST'

echo "created schedule '$NAME' in $REGION"
echo
echo "Verify:   aws scheduler get-schedule --name $NAME --region $REGION"
echo "Run now:  aws lambda invoke --function-name \"$LAMBDA_ARN\" --payload '{}' /dev/stdout"
echo "Re-run a past week:"
echo "          aws lambda invoke --function-name \"$LAMBDA_ARN\" \\"
echo "            --payload '{\"week\":\"2026-08-28\"}' --cli-binary-format raw-in-base64-out /dev/stdout"
