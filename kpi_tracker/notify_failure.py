"""The dead-man's alert: say something when the weekly run did NOT happen.

Wired to systemd's OnFailure= in deploy/kpi-weekly.service. Without it, a run
that crashed, timed out, or never fired at all is indistinguishable from a
quiet week -- the report simply does not arrive and nobody notices for a
month. Silence must not look like success.

It posts through the Slack MCP server, so it holds no credential of its own.
It is deliberately tiny and deliberately forgiving: an alerter that itself
raises is worse than no alerter, because it turns one failure into two and
buries the original.
"""

from __future__ import annotations

import logging
import socket
import sys

from kpi_tracker import config as config_module
from kpi_tracker import logging_setup, wire
from kpi_tracker.mcp_call import McpCallFailed, call_tool

log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    logging_setup.configure()
    config_path = (argv or sys.argv[1:] or ["config.json"])[0]

    try:
        config = config_module.load(config_path)
        server_url = config.slack_mcp_url
    except config_module.ConfigError as exc:
        log.error("cannot even read the config to raise an alert: %s", exc)
        return 1

    if not server_url:
        log.error("mcp.slack_url is not configured, so no alert can be sent")
        return 1

    text = (
        f":warning: The weekly KPI run failed on {socket.gethostname()} and no report "
        "was sent. Check `journalctl -u kpi-weekly.service -n 50` on the VM."
    )
    try:
        call_tool(server_url, wire.TOOL_POST_ALERT, {"text": text}, timeout_seconds=60.0)
    except McpCallFailed as exc:
        # Both the run and the alert failed. Log loudly and exit non-zero so
        # the journal records it, but do not raise -- there is nothing above us
        # to catch it.
        log.error("could not send the failure alert: %s", exc)
        return 1

    log.info("failure alert sent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
