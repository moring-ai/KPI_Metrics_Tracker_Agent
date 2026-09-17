"""Secret redaction.

This file exists because a mutation test found the hole: disabling redaction
entirely broke no test at all. Every case below is a path a real token can
actually travel down in this program.

The two that matter most are the ones a naive filter misses -- the traceback
rendered from exc_info, and an exception object passed as a log argument --
plus the non-log path, where a vendor's error text is quoted under the table
in the CTO's Slack DM and written to data/history.jsonl.
"""

from __future__ import annotations

import io
import json
import logging

import pytest

from kpi_tracker import report
from kpi_tracker.logging_setup import REDACTED, RedactingFormatter
from kpi_tracker.models import FetchResult
from kpi_tracker.redaction import redact
from kpi_tracker.slack_delivery import build_blocks, fallback_text
from kpi_tracker.timewindow import last_week

# Split so the whole token never appears as one literal in this file. It is a
# fake, but it is fake in exactly the shape a real one has -- which is the point
# of the test, and also what GitHub push protection matches on. The value that
# reaches redact() is unchanged, so the test is exactly as strict as before.
SLACK_TOKEN = "xoxb-" + "9999999999-8888888888-abcdefghijklmnopqrstuvwx"
BRIGHT_TOKEN = "ab12cd34ef56ab78cd90ef12ab34cd56ef78ab90"
WEEK = last_week("2026-09-03", "Asia/Kolkata", "thursday")


@pytest.fixture
def captured_log():
    """A logger wired exactly the way configure() wires the real one."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingFormatter("%(levelname)s %(message)s"))

    log = logging.getLogger("redaction-probe")
    log.handlers = [handler]
    log.setLevel(logging.DEBUG)
    log.propagate = False
    return log, stream


# -- the string-level primitive -------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        f"token is {SLACK_TOKEN}",
        f"Authorization: Bearer {BRIGHT_TOKEN}",
        f"https://api.brightdata.com/x?api_token={BRIGHT_TOKEN}",
        f'{{"api_key": "{BRIGHT_TOKEN}"}}',
        f"password={BRIGHT_TOKEN}",
    ],
)
def test_token_shaped_text_is_stripped(text):
    cleaned = redact(text)
    assert SLACK_TOKEN not in cleaned
    assert BRIGHT_TOKEN not in cleaned
    assert REDACTED in cleaned


def test_ordinary_text_is_left_alone():
    """Over-redacting would make the logs useless."""
    text = "reporting week Thu 2026-08-27 to Wed 2026-09-02 for 5 members"
    assert redact(text) == text


# -- log paths -------------------------------------------------------------


def test_a_token_in_the_message_is_stripped(captured_log):
    log, stream = captured_log
    log.info("using %s", SLACK_TOKEN)
    assert SLACK_TOKEN not in stream.getvalue()


def test_a_token_interpolated_before_logging_is_stripped(captured_log):
    log, stream = captured_log
    log.info("using " + SLACK_TOKEN)
    assert SLACK_TOKEN not in stream.getvalue()


def test_a_token_inside_a_traceback_is_stripped(captured_log):
    """log.exception() renders exc_info through the Formatter, which a
    logging.Filter never sees. This is the case a filter-based approach
    silently misses."""
    log, stream = captured_log
    try:
        raise RuntimeError(f"vendor rejected token {SLACK_TOKEN}")
    except RuntimeError:
        log.exception("bright data fetch failed")

    output = stream.getvalue()
    assert "Traceback" in output, "the traceback should still be logged"
    assert SLACK_TOKEN not in output


def test_an_exception_passed_as_an_argument_is_stripped(captured_log):
    """`log.error("failed: %s", exc)` passes an Exception, not a str. A filter
    that only redacts str arguments lets this straight through."""
    log, stream = captured_log
    try:
        raise RuntimeError(f"vendor rejected token {SLACK_TOKEN}")
    except RuntimeError as exc:
        log.error("fetch failed: %s", exc)

    assert SLACK_TOKEN not in stream.getvalue()


def test_numeric_format_specifiers_still_work(captured_log):
    """Regression: redacting by coercing every argument to str breaks %d."""
    log, stream = captured_log
    log.info("reporting for %d members in %s", 5, "Asia/Kolkata")
    assert "reporting for 5 members in Asia/Kolkata" in stream.getvalue()


def test_dict_style_formatting_still_works(captured_log):
    log, stream = captured_log
    log.info("%(count)d posts", {"count": 3})
    assert "3 posts" in stream.getvalue()


# -- the non-log path: Slack and history ----------------------------------


def test_a_failed_fetch_redacts_its_error_text():
    result = FetchResult.failed("brightdata", f"401 for https://x?api_token={BRIGHT_TOKEN}")
    assert BRIGHT_TOKEN not in result.error
    assert REDACTED in result.error


def test_a_vendor_token_cannot_reach_the_slack_message(members):
    """FetchResult.error is quoted in a note under the table. Without
    redaction at construction, a token is DMed to the CTO."""
    broken = FetchResult.failed("brightdata", f"auth failed: Bearer {BRIGHT_TOKEN}")
    built = report.build(members, broken, FetchResult("blog", True, []), WEEK)

    payload = json.dumps(build_blocks(built)) + fallback_text(built) + report.render_text(built)
    assert BRIGHT_TOKEN not in payload
    assert "REDACTED" in payload


def test_a_vendor_token_cannot_reach_history_on_disk(members, tmp_path):
    from kpi_tracker import history

    broken = FetchResult.failed("brightdata", f"auth failed: Bearer {BRIGHT_TOKEN}")
    built = report.build(members, broken, FetchResult("blog", True, []), WEEK)

    path = tmp_path / "history.jsonl"
    history.append(path, built, sent=False)
    assert BRIGHT_TOKEN not in path.read_text()


# -- gaps found while planning the AWS Secrets Manager move ----------------
#
# All four of these leaked before the rewrite. The first two are the shape this
# system's own credentials take, and the last two arrive with boto3.


def test_a_secret_named_inside_an_identifier_is_redacted():
    """The original pattern needed a word boundary before "token", and there
    is none between "_" and "TOKEN" -- so this exact string, our own env var,
    passed through untouched into Slack notes and history.jsonl."""
    out = redact("BRIGHTDATA_API_TOKEN=deadbeef-0000-4000-8000-000000000000")
    assert "deadbeef" not in out
    assert out == "BRIGHTDATA_API_TOKEN=***REDACTED***"


def test_the_json_shape_secrets_manager_returns_is_redacted():
    out = redact('{"BRIGHTDATA_API_TOKEN": "deadbeef000040008000000000000000"}')
    assert "deadbeef" not in out


def test_aws_access_key_ids_are_redacted_without_a_label():
    """Self-identifying by prefix, so it needs no surrounding context."""
    assert redact("AKIAIOSFODNN7EXAMPLE") == "***REDACTED***"
    assert "ASIAZZZZZZZZZZZZZZZZ" not in redact("creds: ASIAZZZZZZZZZZZZZZZZ")


def test_aws_secret_key_and_session_token_are_redacted():
    """Only recognisable by their label, and the label has words between the
    keyword and the '=' -- aws_SECRET_access_key, not aws_secret=."""
    assert "wJalrXUtnFEMI" not in redact(
        "aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    )
    assert "FwoGZXIvYXdz" not in redact("AWS_SESSION_TOKEN=FwoGZXIvYXdzEBYaDHt0aGlzaXNhdGVzdA")


def test_the_label_survives_so_an_operator_can_tell_what_was_wrong():
    """'***REDACTED***' alone does not say which credential failed."""
    assert redact("SLACK_BOT_TOKEN=xoxb-111-222-abcdefghij").startswith("SLACK_BOT_TOKEN=")


@pytest.mark.parametrize("benign", [
    "Weekly KPI - Thu 2026-08-27 to Wed 2026-09-02",
    "vendor could not scrape balajinagarajkumar: Crawler error: Minimal layout detected",
    "https://www.linkedin.com/posts/ashvath-narayanan_x-activity-7499027998312443453-aB3x",
    "https://www.moring.ai/blogs/who-is-accountable-when-an-ai-agent-makes-a-bad-decision",
    "listing page is now paginated; this scraper reads page 1 only and is undercounting",
    "snapshot sd_mtr8r0yb1cwzl3067b triggered for 6 profiles",
    "activity id 7054425663348363266 decodes to 2023-04-19T12:09:03",
    "monkey=abcdefghijklmnopqrst",
])
def test_real_report_text_is_left_alone(benign):
    """Over-redaction is its own failure: it would blank out the diagnostics
    the notes exist to carry."""
    assert redact(benign) == benign


def test_a_secretsmanager_arn_is_not_mistaken_for_a_secret():
    """The ARN is an identifier we may want to log; only its value is secret."""
    arn = "arn:aws:secretsmanager:eu-north-1:123456789012:secret:kpi/tokens-AbCdEf"
    assert "123456789012" in redact(arn)


# -- configure() must not destroy a platform's own handler ----------------


def test_configure_adopts_an_existing_handler_rather_than_replacing_it():
    """AWS Lambda installs its own root handler, and clearing it costs the
    RequestId correlation that makes one invocation's logs findable."""
    import io
    import logging as _logging

    from kpi_tracker.logging_setup import RedactingFormatter, configure

    root = _logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        root.handlers.clear()
        platform_handler = _logging.StreamHandler(io.StringIO())
        platform_handler.setFormatter(_logging.Formatter("PLATFORM %(message)s"))
        root.addHandler(platform_handler)

        configure()

        assert platform_handler in root.handlers, "the platform's handler was removed"
        assert isinstance(platform_handler.formatter, RedactingFormatter)
        # ...and the platform's own format string survived.
        record = _logging.LogRecord("t", _logging.INFO, "f", 1,
                                    "tok SLACK_BOT_TOKEN=xoxb-9999999999-abcdefghij", (), None)
        rendered = platform_handler.formatter.format(record)
        assert rendered.startswith("PLATFORM ")
        assert "xoxb-9999999999" not in rendered
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_configure_installs_a_handler_when_there_is_none():
    import logging as _logging

    from kpi_tracker.logging_setup import RedactingFormatter, configure

    root = _logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        root.handlers.clear()
        configure()
        assert root.handlers, "the CLI has no pre-existing handler; one must be added"
        assert isinstance(root.handlers[0].formatter, RedactingFormatter)
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
