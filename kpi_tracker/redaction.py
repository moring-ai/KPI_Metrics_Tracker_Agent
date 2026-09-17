"""Strip anything token-shaped out of a string.

Kept in its own module because two very different places need it and neither
should depend on the other: log output, and the error text that travels inside
a FetchResult all the way into the Slack message and data/history.jsonl.

That second path is the one that bites. A vendor client is entitled to put the
request it attempted -- headers included -- into an exception message. Without
this, "LinkedIn data unavailable (401 for https://...?token=...)" becomes a
note under a table in the CTO's DM, and a line in a file on disk.

Two rules the patterns below follow, both learned from real misses:

* Match the keyword ANYWHERE inside an identifier, not at a word boundary.
  `\\btoken` cannot match inside BRIGHTDATA_API_TOKEN, because "_" and "T" are
  both word characters -- so the whole name slid past unredacted. That is
  precisely the JSON shape AWS Secrets Manager returns.

* Keep the label, redact only the value. `BRIGHTDATA_API_TOKEN=***REDACTED***`
  tells an operator which credential was wrong; `***REDACTED***` does not.
"""

from __future__ import annotations

import re

REDACTED = "***REDACTED***"

# Any identifier containing one of these words is treated as naming a secret.
# Deliberately matched mid-identifier, so aws_secret_access_key and
# BRIGHTDATA_API_TOKEN are both caught.
_SECRET_WORDS = r"token|secret|password|passwd|credential|api[_-]?key|access[_-]?key|auth"

# label = value  /  "label": "value"  -- the label is preserved.
_LABELLED_VALUE = re.compile(
    r"(?P<label>[A-Za-z0-9_.-]*(?:" + _SECRET_WORDS + r")[A-Za-z0-9_.-]*"
    r"['\"]?\s*[=:]\s*['\"]?)"
    r"(?P<value>[A-Za-z0-9._/+=-]{12,})",
    re.IGNORECASE,
)

_BARE_PATTERNS = [
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),  # Slack tokens
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{16,}", re.IGNORECASE),  # Authorization headers
    # An AWS access key id is self-identifying by prefix, so it needs no label.
    # The matching secret key and session token are only recognisable by their
    # label, and are handled by _LABELLED_VALUE above.
    re.compile(r"\b(?:AKIA|ASIA|AIDA|AROA|ANPA|ANVA|APKA)[A-Z0-9]{12,}\b"),
]


def redact(text: str) -> str:
    """Replace every token-shaped run in `text`. Safe on any string."""
    for pattern in _BARE_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return _LABELLED_VALUE.sub(lambda m: m.group("label") + REDACTED, text)
