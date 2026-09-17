#!/bin/bash
# Build the image, push it to ECR, and create or update the Lambda.
#
#   BUCKET=my-kpi-bucket bash deploy/lambda/deploy.sh
#
# Safe to run again -- the second run just updates the function's code.
# Needs: docker running, and aws configured for the right account.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

: "${BUCKET:?set BUCKET to the S3 bucket that will hold the two state files}"
REGION="${AWS_REGION:-us-east-1}"
NAME="${NAME:-kpi-weekly}"
SLACK_DESTINATION="${SLACK_DESTINATION:-C0BV21PC28P}"

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REPO="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/$NAME"

say() { printf '\n== %s\n' "$*"; }

say "Account $ACCOUNT · region $REGION · bucket $BUCKET"

say "1/5  ECR repository"
aws ecr describe-repositories --repository-names "$NAME" --region "$REGION" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name "$NAME" --region "$REGION" >/dev/null
aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com" >/dev/null
echo "  $REPO"

say "2/5  Build and push (arm64)"
docker build --platform linux/arm64 -f deploy/lambda/Dockerfile -t "$REPO:latest" . 
docker push "$REPO:latest" >/dev/null
echo "  pushed"

say "3/5  Execution role"
if ! aws iam get-role --role-name "$NAME-lambda" >/dev/null 2>&1; then
  aws iam create-role --role-name "$NAME-lambda" \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
      "Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
  echo "  created $NAME-lambda (waiting 10s for IAM to propagate)"
  sleep 10
fi
sed -e "s/ACCOUNT_ID/$ACCOUNT/g" -e "s/BUCKET/$BUCKET/g" deploy/lambda/iam-policy.json > /tmp/kpi-policy.json
aws iam put-role-policy --role-name "$NAME-lambda" \
  --policy-name "$NAME" --policy-document file:///tmp/kpi-policy.json
echo "  policy attached (2 secrets, 1 S3 prefix, logs)"

say "4/5  Lambda function"
ENVIRONMENT="Variables={
KPI_SECRET_BACKEND=aws,
KPI_LINKEDIN_SECRET_ID=kpi/brightdata-api-token,
KPI_SLACK_SECRET_ID=kpi/slack-bot-token,
KPI_SLACK_DESTINATION=$SLACK_DESTINATION,
KPI_HISTORY_PATH=s3://$BUCKET/kpi-tracker/history.jsonl,
KPI_SLACK_STATE_PATH=s3://$BUCKET/kpi-tracker/slack-sent.json,
KPI_VENDOR_POLL_TIMEOUT=780,
AGENT_SEND=${AGENT_SEND:-false}
}"

if aws lambda get-function --function-name "$NAME" --region "$REGION" >/dev/null 2>&1; then
  aws lambda update-function-code --function-name "$NAME" --region "$REGION" \
    --image-uri "$REPO:latest" >/dev/null
  aws lambda wait function-updated --function-name "$NAME" --region "$REGION"
  # The whole environment is REPLACED by this call, which is why every variable
  # is listed above rather than just the one being changed.
  aws lambda update-function-configuration --function-name "$NAME" --region "$REGION" \
    --timeout 900 --memory-size 512 --environment "$ENVIRONMENT" >/dev/null
  echo "  updated"
else
  aws lambda create-function --function-name "$NAME" --region "$REGION" \
    --package-type Image --code ImageUri="$REPO:latest" \
    --role "arn:aws:iam::$ACCOUNT:role/$NAME-lambda" \
    --architectures arm64 --timeout 900 --memory-size 512 \
    --environment "$ENVIRONMENT" >/dev/null
  echo "  created"
fi
aws lambda wait function-updated --function-name "$NAME" --region "$REGION"

say "5/5  One retry layer, not two"
# EventBridge invokes Lambda asynchronously, so Lambda adds its OWN retries on
# top of the schedule's. Turning these off leaves exactly one layer.
aws lambda put-function-event-invoke-config --function-name "$NAME" --region "$REGION" \
  --maximum-retry-attempts 0 >/dev/null
echo "  Lambda's own async retries disabled"

say "Done. AGENT_SEND=${AGENT_SEND:-false}"
cat <<NEXT

Try it (sends nothing while AGENT_SEND=false):

  aws lambda invoke --function-name $NAME --region $REGION \\
    --payload '{"send":false}' --cli-binary-format raw-in-base64-out \\
    /tmp/kpi-out.json > /dev/null && python3 -m json.tool /tmp/kpi-out.json

Watch the logs:

  aws logs tail /aws/lambda/$NAME --follow --region $REGION

When the numbers look right, send for real:

  AGENT_SEND=true BUCKET=$BUCKET bash deploy/lambda/deploy.sh
  aws lambda invoke --function-name $NAME --region $REGION --payload '{}' \\
    --cli-binary-format raw-in-base64-out /tmp/kpi-out.json > /dev/null

Then the weekly schedule and the alarm -- see deploy/lambda/README.md steps 9 and 10.
NEXT
