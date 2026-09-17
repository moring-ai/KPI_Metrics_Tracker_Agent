"""EventBridge Scheduler -> this Lambda -> AgentCore. The weekly trigger.

WHY A LAMBDA AND NOT A DIRECT SCHEDULER TARGET
----------------------------------------------
EventBridge Scheduler can call AWS SDK actions directly ("universal targets"),
which would remove this function. Two reasons not to:

  * runtimeSessionId must be 33-256 characters and must differ per invocation.
    A static schedule input cannot generate one.
  * A direct target gives nowhere to notice failure. This job's whole design
    rests on "silence must not look like success" -- a weekly report that
    never fires has to tell somebody. Here there is somewhere to put that.

WHAT IT DOES NOT DO
-------------------
It does not decide anything about the report. It invokes the agent and reports
the outcome. All the logic -- which week, whether to send, what the numbers are
-- lives in the agent, where it is tested.
"""
from __future__ import annotations

import json
import logging
import os
import uuid

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

AGENT_RUNTIME_ARN = os.environ["AGENT_RUNTIME_ARN"]
QUALIFIER = os.environ.get("AGENT_QUALIFIER", "DEFAULT")
# Set to a Slack Incoming Webhook to be told when the weekly run fails. Without
# it a failure is only visible in CloudWatch, which nobody reads on a Thursday.
ALERT_WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL", "")


def _session_id() -> str:
    """AgentCore requires 33-256 characters. A uuid4 hex is 32 -- one short."""
    return uuid.uuid4().hex + uuid.uuid4().hex


def handler(event, context):
    client = boto3.client("bedrock-agentcore")
    # The scheduler may pass {"week": "2026-08-28"} to re-run a specific week;
    # normally it passes nothing and the agent uses the current window.
    week = (event or {}).get("week")
    prompt = f"run {week}" if week else "run"

    log.info("invoking %s (prompt=%r)", AGENT_RUNTIME_ARN, prompt)
    try:
        response = client.invoke_agent_runtime(
            agentRuntimeArn=AGENT_RUNTIME_ARN,
            qualifier=QUALIFIER,
            runtimeSessionId=_session_id(),
            contentType="application/json",
            accept="application/json",
            payload=json.dumps({"prompt": prompt}).encode("utf-8"),
        )
        body = response["response"].read().decode("utf-8")
    except Exception as exc:  # noqa: BLE001 - every failure must be reported, not raised into the void
        log.exception("invoking the KPI agent failed")
        _alert(f":warning: The weekly KPI agent could not be invoked: {type(exc).__name__}")
        raise

    log.info("agent responded: %s", body[:2000])

    # The agent returns {"result": "..."} on success and {"error": "..."} when
    # it could not even initialise. The second is invisible without this check:
    # the invoke succeeded, so nothing else would flag it.
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        parsed = {}
    if isinstance(parsed, dict) and parsed.get("error"):
        _alert(f":warning: The weekly KPI agent reported an error: {parsed['error']}")

    return {"statusCode": 200, "body": body}


def _alert(text: str) -> None:
    """Best effort. An alerter that raises turns one failure into two."""
    if not ALERT_WEBHOOK_URL:
        log.warning("no ALERT_WEBHOOK_URL set; failure is only in CloudWatch: %s", text)
        return
    try:
        import urllib.request

        request = urllib.request.Request(
            ALERT_WEBHOOK_URL,
            data=json.dumps({"text": text}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(request, timeout=10).read()
    except Exception:  # noqa: BLE001
        log.exception("could not send the failure alert")
