"""Read and write a small text file, locally or in S3.

Two things in this system outlive a single run: the history log and the Slack
server's record of which weeks it has already posted. On EC2 both are ordinary
files. On Lambda the filesystem is thrown away after every invocation, so they
have to live in S3 or they do not exist at all -- and without them the collapse
guardrail never learns what a normal week looks like and a retry posts the
report twice.

Rather than teach two modules about S3, both go through here. A location is a
plain path or an `s3://bucket/key` URI, and the callers do not care which.

boto3 is imported lazily, inside the S3 branch, so the offline test suite never
loads it and a local deployment never needs it installed.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

S3_PREFIX = "s3://"


def is_remote(location: str | Path) -> bool:
    return str(location).startswith(S3_PREFIX)


def _split(uri: str) -> tuple[str, str]:
    bucket, _, key = str(uri)[len(S3_PREFIX):].partition("/")
    if not bucket or not key:
        raise ValueError(f"{uri!r} is not a valid s3://bucket/key URI")
    return bucket, key


def read_text(location: str | Path) -> str:
    """The file's contents, or "" when it does not exist yet.

    A missing file is normal -- it is what the first run sees -- so it is not an
    error. Anything else IS an error and is raised: silently returning "" on a
    permissions failure would look exactly like a fresh install, and the
    collapse guardrail would quietly lose its baseline forever.
    """
    if not is_remote(location):
        path = Path(location)
        return path.read_text(encoding="utf-8") if path.exists() else ""

    import boto3
    from botocore.exceptions import ClientError

    bucket, key = _split(str(location))
    try:
        response = boto3.client("s3").get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return ""
        raise
    return response["Body"].read().decode("utf-8")


def write_text(location: str | Path, text: str) -> None:
    """Replace the file's contents.

    S3 has no append, so both callers read-modify-write. That is safe here
    because exactly one process writes each file, once a week.
    """
    if not is_remote(location):
        path = Path(location)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return

    import boto3

    bucket, key = _split(str(location))
    boto3.client("s3").put_object(
        Bucket=bucket, Key=key, Body=text.encode("utf-8"), ContentType="application/json"
    )
    log.debug("wrote %s", location)
