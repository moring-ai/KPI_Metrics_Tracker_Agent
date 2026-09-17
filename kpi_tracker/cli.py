"""Command line entry point.

    uv run kpi-tracker run                 # dry run: print the table, send nothing
    uv run kpi-tracker run --send          # post it to Slack
    uv run kpi-tracker run --week 2026-08-28   # re-run a past week
    uv run kpi-tracker blogs               # inspect what the blog scraper sees
    uv run kpi-tracker check               # validate config before the first real run

Dry run is the default on purpose. Sending is the one irreversible thing this
program does, so it takes an explicit flag every time rather than being the
behaviour you get by forgetting one.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys

from kpi_tracker import config as config_module
from kpi_tracker import history, logging_setup, pipeline, report
from kpi_tracker.sources.moring_blog import MoringBlogSource
from kpi_tracker.timewindow import last_week


def main(argv: list[str] | None = None) -> int:
    # The shared flags are attached to every subcommand as well as the top
    # level, so both `kpi-tracker -v run` and `kpi-tracker run -v` work.
    # SUPPRESS matters: without it the subparser's default would overwrite a
    # value already given before the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS, help="path to config.json")
    common.add_argument(
        "-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
        help="show per-post detail and source warnings",
    )

    parser = argparse.ArgumentParser(
        prog="kpi-tracker", description=__doc__.split("\n")[0], parents=[common]
    )
    parser.set_defaults(config="config.json", verbose=False)
    commands = parser.add_subparsers(dest="command", required=True)

    run_cmd = commands.add_parser(
        "run", help="collect the week's numbers and report them", parents=[common]
    )
    run_cmd.add_argument("--send", action="store_true", help="actually post to Slack")
    run_cmd.add_argument("--week", help="run as if today were this date (YYYY-MM-DD)")
    run_cmd.add_argument("--force", action="store_true", help="send even if this week was already sent")

    blogs_cmd = commands.add_parser(
        "blogs", help="list every blog post the scraper can see", parents=[common]
    )
    blogs_cmd.add_argument("--week", help="which week to highlight (YYYY-MM-DD)")

    commands.add_parser(
        "check", help="validate config, members and credentials", parents=[common]
    )

    args = parser.parse_args(argv)
    logging_setup.configure(args.verbose)

    # No .env loading here, deliberately. This process needs no credential, so
    # it never reads a credential file -- which is one fewer way for the MCP
    # isolation to be undone by accident. The SERVERS load .env for local
    # development; see kpi_mcp/secrets.py.

    try:
        config = config_module.load(args.config)
    except config_module.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.command == "run":
        return _run(config, args)
    if args.command == "blogs":
        return _blogs(config, args)
    return _check(config)


def _run(config, args) -> int:
    built, decisions = pipeline.run(config, run_at=args.week, send=args.send, force=args.force)

    if built is None:
        print("This week has already been reported. Nothing fetched, nothing sent.")
        print("Use --force to send it again.")
        return 0

    print()
    print(report.render_text(built))
    print()

    fired = [d for d in decisions if d.fired]
    if fired:
        print("Guardrails:")
        for decision in fired:
            print(f"  [{decision.action}] {decision.name}: {decision.reason}")
        print()

    if built.blocked:
        print("NOT SENT - a guardrail blocked this report.", file=sys.stderr)
        return 1
    if not args.send:
        print(f"Dry run. Re-run with --send to post this to {config.slack_destination_label}.")
    return 0


def _blogs(config, args) -> int:
    week = last_week(
        args.week or pipeline._today(config.timezone), config.timezone, config.week_start_day
    )
    result = MoringBlogSource(base_url=config.blog_base_url).fetch(week, resolve_all=True)

    if not result.ok:
        print(f"blog fetch failed: {result.error}", file=sys.stderr)
        return 1

    print(f"Window: {week.label} ({week.tz_name})\n")
    print(f"{'published':11} {'src':10} {'in week':8} {'handle':30} title")
    print("-" * 100)
    for post in sorted(result.items, key=lambda p: (p.published_on is not None, p.published_on), reverse=True):
        marker = "yes" if week.contains_date(post.published_on) else ""
        print(
            f"{str(post.published_on):11} {post.date_source:10} {marker:8} "
            f"{str(post.author_handle):30} {post.title[:40]}"
        )
    for warning in result.warnings:
        print(f"\nwarning: {warning}")
    return 0


def _check(config) -> int:
    """Validate what this process can validate -- and ask the servers the rest.

    This got strictly stronger with the MCP split. It used to assert that a
    token-shaped string existed in the environment, which said nothing about
    whether the token worked. Now it asks each server whether it can actually
    reach its credential, and whether it publishes the schema this client is
    about to rely on. The credential itself never crosses the boundary.
    """
    problems = []

    try:
        members = config_module.load_members(config.members_path)
        print(f"members.json      OK  ({len(members)} people)")
    except config_module.ConfigError as exc:
        problems.append(str(exc))
        print(f"members.json      FAIL  {exc}")

    print(f"timezone          OK  ({config.timezone})")
    print(f"week anchor       OK  ({config.week_start_day})")
    print(f"linkedin source   OK  ({config.linkedin_source})")

    # A credential on THIS box would defeat the whole point of the split.
    stray = [name for name in ("BRIGHTDATA_API_TOKEN", "SLACK_BOT_TOKEN") if os.environ.get(name)]
    if stray:
        problems.append(
            f"{', '.join(stray)} set in this process's environment -- the client must hold "
            "no credentials; remove it and let the MCP servers fetch from Secrets Manager"
        )
        print(f"client is clean   FAIL  {', '.join(stray)} is set here")
    elif pathlib.Path(".env").exists():
        print("client is clean   WARN  .env exists; delete it on the VM (see deploy/README.md)")
    else:
        print("client is clean   OK  (no credentials in this process)")

    if config.linkedin_source == "mcp":
        ok, detail = pipeline.build_linkedin_source(config).check()
        print(f"kpi-linkedin      {'OK  ' if ok else 'FAIL'}  {detail}")
        if not ok:
            problems.append(f"kpi-linkedin: {detail}")

    if config.slack_mcp_url:
        ok, detail = _check_slack_server(config.slack_mcp_url)
        print(f"kpi-slack         {'OK  ' if ok else 'FAIL'}  {detail}")
        if not ok:
            problems.append(f"kpi-slack: {detail}")
    else:
        print("kpi-slack         WARN  mcp.slack_url not set; --send will fail")

    sent_weeks = len([r for r in history.read_all(config.history_path) if r.get("sent")])
    print(f"history           OK  ({sent_weeks} week(s) already sent)")

    if problems:
        print(f"\n{len(problems)} problem(s) to fix before the first real run.", file=sys.stderr)
        return 1
    print("\nAll checks passed.")
    return 0


def _check_slack_server(server_url: str) -> tuple[bool, str]:
    """Ask kpi-slack whether it has a working token and a destination."""
    from kpi_tracker import wire
    from kpi_tracker.mcp_call import McpCallFailed, call_tool

    try:
        payload = call_tool(server_url, wire.TOOL_CHECK, {}, timeout_seconds=30.0)
    except McpCallFailed as exc:
        return False, str(exc)
    if payload.get("schema_version") != wire.SCHEMA_VERSION:
        return False, f"server speaks schema_version {payload.get('schema_version')!r}"
    return bool(payload.get("ok")), str(payload.get("detail") or "")


if __name__ == "__main__":
    raise SystemExit(main())
