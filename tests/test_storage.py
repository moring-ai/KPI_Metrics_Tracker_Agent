"""Reading and writing the two files that outlive a single run.

On EC2 they are ordinary files. On Lambda they must be in S3 or they do not
exist at all -- and without them the collapse guardrail never gets a baseline
and a retry posts the report twice.

The distinction that matters here: a MISSING file is normal (it is what the
first run sees) and returns "". Anything else is an error and is raised,
because silently returning "" on a permissions failure looks exactly like a
fresh install, and the baseline would be lost forever without a sound.
"""

import json
import sys
import types

import pytest

from kpi_tracker import storage


# -- local ----------------------------------------------------------------


def test_a_local_roundtrip(tmp_path):
    storage.write_text(tmp_path / "a.txt", "hello\n")
    assert storage.read_text(tmp_path / "a.txt") == "hello\n"


def test_a_missing_local_file_is_empty_not_an_error(tmp_path):
    assert storage.read_text(tmp_path / "nope.txt") == ""


def test_writing_creates_parent_directories(tmp_path):
    storage.write_text(tmp_path / "deep" / "nested" / "a.txt", "x")
    assert (tmp_path / "deep" / "nested" / "a.txt").read_text() == "x"


@pytest.mark.parametrize("location,remote", [
    ("s3://bucket/key.jsonl", True),
    ("s3://bucket/a/b/c.json", True),
    ("/opt/kpi-tracker/data/history.jsonl", False),
    ("data/history.jsonl", False),
    ("", False),
])
def test_remote_locations_are_recognised(location, remote):
    assert storage.is_remote(location) is remote


# -- S3 -------------------------------------------------------------------


class FakeS3:
    """Just enough of the S3 client, recording what it was asked to do."""

    def __init__(self, objects=None, error=None):
        self.objects = dict(objects or {})
        self.error = error
        self.puts = []

    def get_object(self, Bucket, Key):  # noqa: N803 - boto3's casing
        if self.error:
            raise self.error
        if (Bucket, Key) not in self.objects:
            raise _client_error("NoSuchKey")
        body = types.SimpleNamespace(read=lambda: self.objects[(Bucket, Key)].encode())
        return {"Body": body}

    def put_object(self, Bucket, Key, Body, ContentType=None):  # noqa: N803
        self.puts.append((Bucket, Key, Body.decode()))
        self.objects[(Bucket, Key)] = Body.decode()


def _client_error(code):
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": code}}, "GetObject")


@pytest.fixture
def fake_s3(monkeypatch):
    client = FakeS3()
    module = types.SimpleNamespace(client=lambda service, **kw: client)
    monkeypatch.setitem(sys.modules, "boto3", module)
    return client


def test_an_s3_roundtrip(fake_s3):
    storage.write_text("s3://kpi/history.jsonl", "line\n")
    assert fake_s3.puts[0][:2] == ("kpi", "history.jsonl")
    assert storage.read_text("s3://kpi/history.jsonl") == "line\n"


def test_a_missing_s3_object_is_empty_not_an_error(fake_s3):
    """The first run sees this, and it must not look like a failure."""
    assert storage.read_text("s3://kpi/never-written.jsonl") == ""


def test_an_access_denied_is_raised_not_swallowed(monkeypatch):
    """The dangerous case. Returning "" here would be indistinguishable from a
    fresh install, and the collapse guardrail would lose its baseline silently."""
    client = FakeS3(error=_client_error("AccessDenied"))
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda *a, **k: client))
    from botocore.exceptions import ClientError

    with pytest.raises(ClientError):
        storage.read_text("s3://kpi/history.jsonl")


@pytest.mark.parametrize("uri", ["s3://", "s3://bucket", "s3://bucket/", "s3:///key"])
def test_a_malformed_s3_uri_is_refused(uri, fake_s3):
    with pytest.raises(ValueError, match="valid s3"):
        storage.read_text(uri)


def test_boto3_is_never_imported_for_a_local_path(tmp_path, monkeypatch):
    """The offline suite must not need boto3 installed."""
    monkeypatch.setitem(sys.modules, "boto3", None)  # any use would raise
    storage.write_text(tmp_path / "a.txt", "x")
    assert storage.read_text(tmp_path / "a.txt") == "x"


# -- history through S3 ---------------------------------------------------


def test_history_appends_to_s3_without_losing_earlier_lines(fake_s3):
    """S3 has no append, so this is a read-modify-write. Losing a line would
    quietly shorten the guardrail's baseline."""
    from kpi_tracker import history
    from kpi_tracker.models import Report, Row

    uri = "s3://kpi/history.jsonl"
    for week in ("2026-08-27", "2026-09-03"):
        history.append(
            uri,
            Report("W", week, week, "Asia/Kolkata", [Row("A", 2, 0)], True, True),
            sent=True,
        )

    records = history.read_all(uri)
    assert [r["week_start"] for r in records] == ["2026-08-27", "2026-09-03"]
    assert history.already_sent(uri, "2026-08-27") is True
    assert history.recent_linkedin_totals(uri) == [2, 2]


def test_history_on_s3_survives_a_file_with_no_trailing_newline(fake_s3):
    fake_s3.objects[("kpi", "history.jsonl")] = json.dumps({"week_start": "2026-08-20", "sent": True})
    from kpi_tracker import history
    from kpi_tracker.models import Report

    history.append(
        "s3://kpi/history.jsonl",
        Report("W", "2026-08-27", "2026-08-27", "Asia/Kolkata", [], True, True),
        sent=True,
    )
    assert len(history.read_all("s3://kpi/history.jsonl")) == 2
