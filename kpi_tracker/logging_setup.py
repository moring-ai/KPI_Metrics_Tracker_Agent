"""Logging, with secrets stripped on the way out.

Tokens reach this process through the environment and are handed to HTTP
clients. It takes one exception traceback echoing a request to write a live
token into a log file that then gets pasted into a ticket.

Redaction happens in the *formatter*, not in a filter, and that choice matters.
A filter only sees `record.msg` and `record.args`. It never sees the traceback
that `logging.exception()` renders from `exc_info`, and it cannot safely touch
a non-string argument -- coercing every argument to str to redact it breaks
numeric format specifiers like %d, and skipping non-strings lets an exception
object carry a token straight through. Formatting first and redacting the
finished line covers the message, the arguments and the traceback uniformly,
with no format-specifier hazard.
"""

from __future__ import annotations

import logging
import sys

from kpi_tracker.redaction import REDACTED, redact

__all__ = ["RedactingFormatter", "configure", "REDACTED", "redact"]


class RedactingFormatter(logging.Formatter):
    """Formats the record as usual, then strips anything token-shaped."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


DEFAULT_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def configure(verbose: bool = False) -> None:
    """Make every handler on the root logger redact, at process start.

    It ADOPTS the handlers that are already there rather than replacing them,
    which matters on AWS Lambda: the runtime installs its own root handler, and
    clearing it costs the RequestId correlation that makes one invocation's
    logs findable in CloudWatch. Wrapping instead keeps the platform's handler
    and still guarantees nothing token-shaped is written.

    Only when there is no handler at all -- the CLI, a systemd unit -- does it
    install one of its own.
    """
    root = logging.getLogger()
    if root.handlers:
        for handler in root.handlers:
            existing = handler.formatter
            handler.setFormatter(
                RedactingFormatter(
                    getattr(existing, "_fmt", None) or DEFAULT_FORMAT,
                    datefmt=getattr(existing, "datefmt", None),
                )
            )
    else:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(RedactingFormatter(DEFAULT_FORMAT))
        root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    # These are chatty at DEBUG and would echo request URLs containing tokens.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("slack_sdk").setLevel(logging.WARNING)
