# KPI Metrics Tracker

Counts, once a week, how many LinkedIn posts and Moring blog posts each person
published — and DMs the table to the CTO.

Runs **every Thursday**, reporting the seven days up to Wednesday, so the
numbers are fresh for Friday standup.

```
Weekly KPI - Thu 2026-08-27 to Wed 2026-09-02

Name               LinkedIn posts (target 3)  Blogs
Ashvath Narayan    4                          1
Balaji Nagaraj     2                          0
Ellakkiaa          0                          1
Vignesh Nagarajan  ?                          0
```

`?` means *we could not find out* — never silently `0`. That distinction is the
core of the design; see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Quick start

```bash
uv sync
cp config.example.json config.json
cp .env.example .env          # then fill in the two tokens
uv run kpi-tracker check      # validates everything before the first real run
uv run kpi-tracker run        # dry run: prints the table, sends nothing
```

Nothing is sent without `--send`. That is deliberate — posting is the one
irreversible thing this program does, so it takes an explicit flag every time.

```bash
uv run kpi-tracker run --send
```

## Who gets tracked

One file: **[`data/members.json`](data/members.json)**. Paste each person's
LinkedIn URL straight from the browser and give the name they are credited
under on the blog.

```json
{
  "name": "Ashvath Narayan",
  "linkedin_url": "https://www.linkedin.com/in/ashvath-narayanan/",
  "blog_author": "Ashvath Narayan"
}
```

Trailing slashes, `www`, and `?trk=` parameters are all fine — handles are
normalised on load. `blog_author` is optional and only needed when the byline
on moring.ai differs from `name`; it accepts the byline or the
`/authors/<slug>` path. Full details in
[`data/members.README.md`](data/members.README.md).

A blog post whose author matches nobody is **not** dropped — it is reported as
unattributed in the Slack message, so a missing entry shows up as a line to fix
rather than a number that is quietly one too low.

## Commands

| Command | What it does |
|---|---|
| `kpi-tracker run` | Dry run. Prints the table, sends nothing. |
| `kpi-tracker run --send` | Posts to Slack. Skips a week already sent. |
| `kpi-tracker run --week 2026-08-28` | Re-run a past week. |
| `kpi-tracker run --send --force` | Re-send a week that was already sent. |
| `kpi-tracker blogs` | Every blog post the scraper can see, with dates and authors. |
| `kpi-tracker check` | Validate config, members and credentials. |
| `kpi-tracker -v run` | Add `-v` (either before or after the subcommand) for per-post detail and source warnings. |

## Configuration

`config.json` (copy from `config.example.json`). It contains **no credentials
and no Slack destination** — both live on the MCP servers:

| Key | Default | Notes |
|---|---|---|
| `timezone` | `Asia/Kolkata` | Week boundaries are local midnight in this zone. |
| `week_start_day` | `thursday` | The job runs Thursday, so the window is the last complete Thu–Wed. Set to `monday` for calendar weeks. |
| `linkedin.source` | `mcp` | `mcp`, `manual_csv`, or `fixture`. One line to swap. |
| `mcp.linkedin_url` | `http://127.0.0.1:8801/mcp` | Loopback only. |
| `mcp.slack_url` | `http://127.0.0.1:8802/mcp` | Loopback only. |
| `linkedin.target_posts_per_week` | `3` | Shown in the table header. |

The Slack destination is set on the **server** (`KPI_SLACK_DESTINATION`), not
here, so nothing running on the client's machine can redirect the weekly report
at someone else.

`kpi-tracker check` **fails** if it finds `BRIGHTDATA_API_TOKEN` or
`SLACK_BOT_TOKEN` in its own environment. If a credential can reach the client,
the isolation is a convention rather than a structure.

### Slack setup

Create a Slack app from [docs/slack-app-manifest.json](docs/slack-app-manifest.json)
— it requests only **`chat:write`**. Install it to the workspace, invite the bot
to the channel (`/invite @KPI Tracker`), and store the **Bot User OAuth Token**
(`xoxb-…`) in AWS Secrets Manager as `kpi/slack-bot-token`.

An `xapp-` App-Level Token will not work — it is for Socket Mode and cannot
post messages. The kpi-slack server's `check_credentials` names that mistake
explicitly, because it otherwise fails only at send time with a bare
`invalid_auth`. If a DM fails with
`channel_not_found`, add `im:write` and reinstall.

### LinkedIn data

There is no usable official LinkedIn API for this — the permission that would
return a member's posts is closed to new applicants. The default is Bright
Data's Web Scraper API, whose free tier (5,000 records/month) comfortably
covers a team this size.

`manual_csv` is the fallback and needs no vendor at all: paste post URLs into a
CSV and the exact timestamps are decoded locally from the permalinks. It exists
because LinkedIn litigates scraping vendors out of existence — Proxycurl shut
down in July 2025 — and a report that cannot go out is worse than one somebody
filled in by hand.

## Deployment

Three targets: **[EC2](deploy/README.md)** (the isolation is real there),
**[Lambda](deploy/lambda/README.md)** (cheap, no host to patch), and
**[AgentCore](deploy/agentcore/README.md)** (via AICP).

**EC2 is the reference target: see [deploy/README.md](deploy/README.md)** for
the systemd units, the IAM policy, the IMDS block and the runbook. It includes
a dead-man's alert, because a weekly run that never fires is otherwise
indistinguishable from a quiet week.

`schedule/` holds the older macOS/launchd option and
`.github/workflows/weekly-kpi.yml` a CI-based one; both still work but the
EC2 path in `deploy/` is the one with the secret isolation.

The timer fires Thursday 12:30 UTC = 18:00 IST = 08:30 US Eastern, so it lands
on the CTO's Thursday morning ahead of Friday standup. The fire time only
affects *when* it is sent — the seven days it measures come from
`config.timezone`, so re-running mid-week is safe.

Re-running mid-week is safe: the window is anchored to the calendar, not the
run time, so every run between Thursday and Wednesday reports the same numbers,
and a week already sent is skipped unless you pass `--force`.

## Tests

```bash
uv run pytest
```

`tests/test_import_boundary.py` fails the build if any client module imports
`mcp`, `boto3` or `kpi_mcp` at module scope — that is what keeps the suite
offline, keeps it fast, and keeps the client incapable of reaching a secret.

439 cases, no network and no AWS. They cover DST and timezone boundaries, LinkedIn
timestamp decoding against LinkedIn's own published values, all three traps on
the Moring blog, secret redaction, and every guardrail firing and staying quiet.

The suite is mutation-tested: 28 deliberate breakages were applied one at a
time to check the tests actually catch them. That found a real hole — nothing
was testing secret redaction — which led to fixing two genuine leaks.

The code was also reviewed adversarially across four lenses, which found 12
verified defects (plus one false-positive guardrail of my own), all fixed with a regression test each. Nine were the same
failure mode: something goes wrong and the report presents a number anyway.
See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the list.

## How it works

```
kpi_tracker/  (the client — holds NO credentials, cannot reach AWS)
  members.json ─┐
                ├→ kpi-linkedin MCP ──┐
  week window ──┤                     ├→ aggregate → guardrails → kpi-slack MCP
                └→ moring.ai blog ────┘        (in-process; public, no secret)

kpi_mcp/      (the servers — hold the credentials)
  kpi-linkedin ──┐
  kpi-slack ─────┴→ AWS Secrets Manager
```

Five fixed steps and no LLM. The shape of the work is known in advance, so it
is written down rather than rediscovered on every run — and a KPI number about
a named colleague is never left to a model's judgement.

The two credential-holding boundaries run as separate processes under a
different Unix user, so the weekly job cannot read a token even in principle.
That needs three things to be true, not one — see
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §8d, and note that the IMDS
firewall rule is the one that looks optional and is not.

## Documentation

| Document | What it answers |
|---|---|
| [docs/SPEC.md](docs/SPEC.md) | The problem, the users, what is in and out of scope, the ready-to-ship gate, and the PII posture. |
| [docs/CODE_TOUR.md](docs/CODE_TOUR.md) | Every folder and file, why it exists, and a suggested reading order. |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Why the design is what it is, which parts of the rubric do not apply and why, and every defect found by running it. |
| [deploy/README.md](deploy/README.md) | The EC2 runbook. |
