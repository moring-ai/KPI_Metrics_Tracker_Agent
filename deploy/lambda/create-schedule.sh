#!/bin/bash
# The weekly trigger: EventBridge Scheduler -> the Lambda, Thursday 11:00 IST.
#
# EventBridge Scheduler understands timezones natively, so the schedule says
# what it means rather than being converted to UTC by hand the way the systemd
# unit has to be.
#
# TWO LAYERS OF RETRY EXIST, and the one below is not the only one.
#
# EventBridge Scheduler invokes Lambda ASYNCHRONOUSLY, so Lambda applies its
# OWN async retry policy (default: 2 more attempts) on top of the RetryPolicy
# here. Left at defaults you can get up to nine executions from one schedule.
#
# Retries are safe here -- the S3 state makes a repeat a no-op, unlike the
# AgentCore deployment whose state is ephemeral -- but "safe" is not a reason
# to leave two uncontrolled layers in place. Turn Lambda's own off so exactly
# one layer retries, and it is this one:
#
#   aws lambda put-function-event-invoke-config --function-name kpi-weekly \
#     --maximum-retry-attempts 0 --region us-east-1
#
# README step 9 does this.
set -euo pipefail

: "${LAMBDA_ARN:?set LAMBDA_ARN to the ARN of the kpi-weekly function}"
: "${SCHEDULER_ROLE_ARN:?set SCHEDULER_ROLE_ARN to the role EventBridge assumes}"
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
    \"RetryPolicy\": {\"MaximumRetryAttempts\": 2, \"MaximumEventAgeInSeconds\": 3600}
  }" \
  --description 'Weekly LinkedIn + blog KPI report, Thursday 11:00 IST'

echo "created schedule '$NAME' in $REGION"
echo
echo "Verify:      aws scheduler get-schedule --name $NAME --region $REGION"
echo "Dry run now: aws lambda invoke --function-name kpi-weekly \\"
echo "               --payload '{\"send\":false}' --cli-binary-format raw-in-base64-out /dev/stdout"
