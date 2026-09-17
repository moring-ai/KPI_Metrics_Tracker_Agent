# Code tour

A walk through every folder and file, and why each exists. If you read one
thing first, read **The one mental model** below; the rest makes sense after it.

For *what* the system does see [../README.md](../README.md); for *why the
decisions are what they are* see [ARCHITECTURE.md](ARCHITECTURE.md); for the
problem and scope see [SPEC.md](SPEC.md).

---

## The one mental model

**Two packages, and the dependency points one way.**

```
kpi_tracker/  ── the CLIENT.  Holds NO credentials. Cannot reach AWS.
     │             Decides what a week is, counts, aggregates, renders.
     │
     │  HTTP over 127.0.0.1  (MCP, streamable HTTP)
     ▼
kpi_mcp/      ── the SERVERS. Hold the credentials. Talk to vendors.
                   Know nothing about weeks, counting, or reports.
```

`kpi_mcp` **may** import `kpi_tracker` — it reuses tested helpers rather than
duplicating them. `kpi_tracker` may **never** import `kpi_mcp`, `mcp` or
`boto3` at module scope. [`tests/test_import_boundary.py`](../tests/test_import_boundary.py)
fails the build if it does, checked both by AST and by importing the whole
client in a subprocess and inspecting `sys.modules`.

That single rule is what makes "the client holds no token" a structural fact
rather than a habit. It also keeps the test suite offline and under a second.

---

## `kpi_tracker/` — the client

### The contracts. Read these two first.

**[`models.py`](../kpi_tracker/models.py)** — every dataclass in the system.

`FetchResult` is the most important type in the project:

```python
FetchResult(source=..., ok=..., items=[...], unknown_keys=frozenset({...}))
```

`ok=False` means **we do not know**, and every count derived from it must be
`None`, which renders `—`. `unknown_keys` says the same thing for one *person*
when the fetch as a whole succeeded. Conflating either with `0` is the failure
this whole system is built to prevent.

**[`wire.py`](../kpi_tracker/wire.py)** — the client↔server contract. Stdlib
only, so both sides import it without dragging `mcp` onto the client path. Its
docstring explains the two load-bearing decisions: no dates cross the wire, and
the server returns one *accounted-for* entry per requested profile so that an
absent profile reads as failed rather than zero.

### The pipeline — the actual work, in order

| File | Role |
|---|---|
| [`cli.py`](../kpi_tracker/cli.py) | Entry point: `run`, `blogs`, `check`. Dry run is the default; `--send` is required to post. `check` asks each server whether it can reach its credential, without the credential crossing the boundary. |
| [`pipeline.py`](../kpi_tracker/pipeline.py) | **The six fixed steps.** The best single file for understanding the system. Note `_safely()`, which keeps one source's failure from killing the other column. |
| [`timewindow.py`](../kpi_tracker/timewindow.py) | What "last week" means: Thursday→Wednesday in a named timezone, returned as UTC. The `fold=0` comment explains why it is already correct across DST gaps — do not "fix" it. |
| [`report.py`](../kpi_tracker/report.py) | Counting, three-tier attribution, and the notes under the table. The zero-fill vs unknown-fill rule lives here. |
| [`guardrails.py`](../kpi_tracker/guardrails.py) | Five checks that run on the finished report and can **block** the send. Each names its trigger, action and fallback. |
| [`slack_delivery.py`](../kpi_tracker/slack_delivery.py) | Builds the Block Kit table — pure, deterministic, 13 tests — then hands the finished blocks to the MCP server to post. Formatting stayed here on purpose; a server deploy does not run its tests. |

### Support

| File | Why it exists |
|---|---|
| [`matching.py`](../kpi_tracker/matching.py) | Identity normalisation. Every join in the system goes through here, so a bug here shows up as somebody's number being quietly wrong. |
| [`redaction.py`](../kpi_tracker/redaction.py) | Strips token shapes from any string. Used by logs **and** by error text that travels into the Slack message and `history.jsonl`. |
| [`logging_setup.py`](../kpi_tracker/logging_setup.py) | A redacting *formatter*, not a filter — a filter never sees the traceback that `logging.exception()` renders. |
| [`history.py`](../kpi_tracker/history.py) | Append-only `history.jsonl`. Feeds the collapse guardrail's baseline and the one-report-per-week skip. |
| [`config.py`](../kpi_tracker/config.py) | Loads `config.json` and `members.json`, validating everything on load. Holds no credentials. |
| [`mcp_call.py`](../kpi_tracker/mcp_call.py) | **The only asyncio in the project.** Catches `BaseException`, because anyio raises `BaseExceptionGroup` and `CancelledError`, neither of which is an `Exception`. Also where the timeout ordering is documented. |
| [`notify_failure.py`](../kpi_tracker/notify_failure.py) | The dead-man's alert. Without it a run that never fired is indistinguishable from a quiet week. |
| [`envfile.py`](../kpi_tracker/envfile.py) | `.env` loading, used by the **servers** in development. The client deliberately does not call it. |

## `kpi_tracker/sources/` — where data comes from

[`base.py`](../kpi_tracker/sources/base.py) defines one protocol:
`fetch(members, week) -> FetchResult`. **This is the swap seam.** Four
implementations satisfy it and nothing downstream can tell which one ran.

| File | Notes |
|---|---|
| [`mcp_linkedin.py`](../kpi_tracker/sources/mcp_linkedin.py) | Production. Asks the server for permalinks, then dates, windows, deduplicates and decides who is unknown — all client-side. |
| [`manual_csv.py`](../kpi_tracker/sources/manual_csv.py) | The escape hatch. Needs no vendor, no server and no credential, so the report can still go out on a bad week. |
| [`fixture.py`](../kpi_tracker/sources/fixture.py) | A JSON file. Lets the whole pipeline run in tests with no network. |
| [`linkedin_urn.py`](../kpi_tracker/sources/linkedin_urn.py) | Decodes a post's publish time from its own permalink. **Read the docstring** — it explains why the shift is a fixed `>> 22` and not the `>> 23` you will find online, and why a self-adjusting shift is actively harmful. |
| [`moring_blog.py`](../kpi_tracker/sources/moring_blog.py) | Reads the site's **RSS feed** (one request, stdlib XML). Rewritten when moring.ai moved off Webflow and every CSS selector broke — its docstring explains what the feed gains and the one thing the rebuild cost. No credential, so it stays in-process. |

## `kpi_mcp/` — the servers

| File | Role |
|---|---|
| [`secrets.py`](../kpi_mcp/secrets.py) | `EnvSecrets` (development) and `AwsSecrets` (production, TTL-cached). **The file to read for the Secrets Manager story**, including why the two tokens are separate secrets and why the cache TTL is what makes rotation work. |
| [`brightdata.py`](../kpi_mcp/brightdata.py) | The vendor client, moved here so the credential lives only in the server. Nearly every comment records a defect this code already cost us — read before simplifying. |
| [`linkedin_server.py`](../kpi_mcp/linkedin_server.py) | `collect_linkedin_posts`, `check_credentials`. Returns permalinks, never dates. Fills in an explicit "failed" entry for any profile the vendor did not mention. |
| [`slack_server.py`](../kpi_mcp/slack_server.py) | `post_weekly_report`, `post_alert`, `check_credentials`. **The destination is not a tool argument** — it is this server's own config, so nothing else on the machine can redirect the report. |

## `tests/` — 367 cases, ~2,500 lines

Grouped so a failure name tells you what broke.

| Area | File |
|---|---|
| Week/timezone maths, DST, anchors | `test_timewindow.py` |
| Timestamp decoding vs LinkedIn's published values | `test_linkedin_urn.py` |
| Identity normalisation and joins | `test_matching.py` |
| Blog scraping against fixtures of the real traps | `test_moring_blog.py` |
| Aggregation: zero-fill vs unknown-fill | `test_report.py` |
| Guardrails: each fires, and each stays quiet | `test_guardrails.py` |
| Slack payload shape and documented limits | `test_slack_payload.py` |
| The client↔server contract | `test_wire.py` |
| The MCP source, via a fake caller | `test_mcp_linkedin.py` |
| The vendor protocol, server-side | `test_mcp_brightdata.py` |
| Both servers via a **real** MCP client, plus true end-to-end | `test_mcp_servers.py` |
| The asyncio bridge and its exception handling | `test_mcp_call.py` |
| Secret providers and AWS error text | `test_secrets.py` |
| Secret redaction | `test_redaction.py` |
| The architecture rule itself | `test_import_boundary.py` |

## Everything else

| Path | Contents |
|---|---|
| [`deploy/`](../deploy/) | **The EC2 target.** systemd units, IAM policy, the IMDS-blocking script, and the runbook. Start at its README. |
| `docs/` | This file, [SPEC.md](SPEC.md), [ARCHITECTURE.md](ARCHITECTURE.md), and the Slack app manifest. |
| [`data/members.json`](../data/members.json) | **The only file you edit** to change who is tracked. See its README. |
| `scripts/dev-servers.sh` | Starts both servers locally. |
| `schedule/` | The older macOS/launchd option, superseded by `deploy/`. |
| `config.example.json` | Copy to `config.json`. Holds no credentials. |

---

## Suggested reading order

1. [`../README.md`](../README.md) — what it does
2. [`models.py`](../kpi_tracker/models.py) — `FetchResult`; everything else follows
3. [`pipeline.py`](../kpi_tracker/pipeline.py) — the six steps
4. [`wire.py`](../kpi_tracker/wire.py) — why the boundary is shaped that way
5. [ARCHITECTURE.md](ARCHITECTURE.md) §8b–8d — the defects found by running it, and what each taught

## The theme worth carrying to the next system

Almost every non-obvious decision in this codebase exists because the naive
version produced a **confident wrong number** rather than an error:

- A vendor returning nothing looked identical to a quiet week.
- A profile the scraper failed on looked identical to a person who posted nothing.
- A CMS timestamp looked identical to a publish date.
- A timeout firing on healthy-but-slow work looked identical to missing data.

None of these throw. All of them produce a plausible table. That is why the
`int | None` contract, the accounted-for wire format and the guardrails exist,
and it is the one idea to take forward.
