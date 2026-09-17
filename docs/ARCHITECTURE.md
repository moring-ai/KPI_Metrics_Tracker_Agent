# Architecture decisions

Written against the AI-engineering rubric. Where a section does not apply it
says so and why, rather than inventing work to fill it in.

---

## 1. Shape (rubric 2.1)

**Decision: a workflow. Five fixed steps, no LLM anywhere in the control path.**

Walking the decision test in order:

| Question | Answer |
|---|---|
| Can one prompt with a good output format do it? | No — it needs two network sources and arithmetic. |
| 2–3 fixed steps, each feeding the next? | Close, but it needs branching on source failure. |
| **Do we know the shape of the work upfront?** | **Yes.** Fetch → fetch → join → aggregate → render → send. Same order every week. |
| Is the number or order of steps genuinely unknowable? | No. Nothing here is open-ended. |

So it stops at **workflow**, and there is no class of cases that a workflow
cannot handle. Anything above that would be dressing a cron job up as an agent.

**Patterns used:** prompt chaining (a linear pipeline, `kpi_tracker/pipeline.py`)
plus parallel-ish independent fetches, and the strategy pattern at the LinkedIn
source seam.

### How much of this needs an LLM? None of it.

This was worth checking rather than assuming. The one genuinely fuzzy-looking
step is attributing a blog byline to a team member — display names on the site
do not match LinkedIn handles ("Ashvath Narayan" vs `ashvath-narayanan`), which
looks like a job for a model.

It is not, because the site hands us a better key. Every post's JSON-LD carries
`author.sameAs[0]` — the author's LinkedIn profile URL — which is the same key
`members.json` uses. The join is an exact string comparison after
normalisation, with the author-page slug and byline as deterministic fallbacks
(`report._attribute`).

That matters beyond elegance: **a KPI number shown to the CTO about a named
colleague should never be nondeterministic.** A model that is 97% accurate at
name matching is a model that quietly gets someone's number wrong a few times a
year, with no error to notice.

---

## 2. Interfaces (rubric 2.2)

Every step hands the next a typed object from `kpi_tracker/models.py`, so a
break surfaces at the step that caused it.

```
config.json + members.json  →  Config, list[Member]
Week (timewindow)           →  half-open [start, end) in aware UTC
LinkedInSource.fetch()      →  FetchResult[LinkedInPost]
MoringBlogSource.fetch()    →  FetchResult[BlogPost]
report.build()              →  Report (rows of Row)
slack_delivery.build_blocks()→ list[dict]  (pure function, asserted in tests)
```

### The designed failure output

`FetchResult` is the contract, and it is the single most important design
decision in this system:

```python
FetchResult(source=..., ok=False, items=[], error="vendor returned 503")
```

`ok=False` means **we do not know**, and every count derived from it is `None`,
rendered `?` — never `0`.

A broken scraper that reports zero posts for everybody is indistinguishable
from a quiet week unless the two are kept apart all the way to the table. This
is the difference between "the tool is down" and a false statement about a
colleague's work. `Row.cell()` is the only place a count becomes text, and it
never returns an empty string (Slack rejects an entire message on a zero-length
table cell).

---

## 3. Prompts (rubric 2.3) — not applicable

There are no prompts. See section 1.

If an LLM is ever added, the natural place is a *fallback* for blog bylines
that match nobody — currently reported as unattributed in the Slack message.
It would need to sit outside the control path so an outage degrades one
column's attribution rather than killing the run, and its output would have to
be validated against the closed set of names in `members.json`.

---

## 4. Retrieval (rubric 2.4) — not applicable

No RAG. There is no corpus, no embedding, no vector store and no generation
grounded in retrieved text. The two "retrievals" are an HTTP scrape of a known
listing page and a vendor API call, both deterministic and both fully covered
by code evals.

---

## 5. Memory (rubric 2.5) — deliberately none

**No memory.** Each weekly run is independent and computes its answer from
scratch. Carrying state between runs would add failure modes without adding
capability.

The one thing that *is* persisted is `data/history.jsonl`, and it is not
memory in the rubric's sense — it is an append-only audit log of past reports,
used for exactly two mechanical purposes: the collapse guardrail needs a
baseline, and idempotency needs to know whether this week was already sent. It
holds no user content, is scoped to nobody, and is never fed to a model.

---

## 6. Evals (rubric 2.6)

**Scope inherited from the architecture:** workflow ⇒ inputs, outputs, and
which path was taken. Each step is evaluated independently so a compounding
error is caught where it starts.

### The mix

| Type | Share | Why |
|---|---|---|
| Code-based | **~100%** (439 cases) | Every output is deterministic and checkable. |
| LLM-as-judge | **0%** | Nothing here requires judgement. See below. |
| Human review | Weekly, ~5 min | Eyeball the Slack table; spot-check one profile by hand. |

The rubric suggests 60–70% code evals. That figure is a floor written for
generative systems. Here the honest number is ~100%, and **writing an LLM judge
would be ceremony** — there is no subjective output to judge. Tone, faithfulness
and helpfulness do not apply to an integer.

### What the suite covers

| Area | File | Cases |
|---|---|---|
| Week window: DST, fold, anchors, boundaries | `test_timewindow.py` | 40 |
| LinkedIn timestamp decoding | `test_linkedin_urn.py` | 16 |
| Identity normalisation and joins | `test_matching.py` | 24 |
| Blog scraping, incl. all three site traps | `test_moring_blog.py` | 12 |
| Aggregation, zero-fill vs unknown-fill | `test_report.py` | 15 |
| Guardrails: each fires and each stays quiet | `test_guardrails.py` | 12 |
| Slack payload shape and documented limits | `test_slack_payload.py` | 12 |
| Config and members validation | `test_config.py` | 19 |
| Source adapters incl. Bright Data | `test_sources.py` | 15 |
| Secret redaction: logs, tracebacks, Slack, history | `test_redaction.py` | 15 |
| Loading secrets from .env | `test_envfile.py` | 13 |
| The client/server wire contract | `test_wire.py` | 22 |
| The MCP LinkedIn source (fake caller) | `test_mcp_linkedin.py` | 19 |
| The vendor protocol, server-side | `test_mcp_brightdata.py` | 22 |
| Both servers via a real MCP client | `test_mcp_servers.py` | 28 |
| The synchronous asyncio bridge | `test_mcp_call.py` | 12 |
| Secret providers and AWS failure text | `test_secrets.py` | 13 |
| The dead-man's alert | `test_notify_failure.py` | 4 |
| The client/server import boundary | `test_import_boundary.py` | 3 |
| Every shell script parses | `test_shell_scripts.py` | 11 |
| Review regressions (in the files above) | — | 24 |
| End-to-end, send path, idempotency, history baseline | `test_pipeline.py` | 15 |

The edge cases the rubric asks for, all present and each from a real
observation rather than imagination:

1. A person who published nothing → `0`, not absent from the table.
2. A post exactly on a window boundary → the IST-offset cases in the fixture:
   00:01 IST Monday counts, 22:30 IST Sunday does not.
3. A post by someone not in `members.json` → reported as unattributed, never
   silently dropped.
4. The same post listed twice → counted once.
5. A blog post with no publish date at all → excluded and warned about.
6. A scraper returning zero for everyone → guardrail, not a table of zeros.
7. A truncated or malformed activity id → rejected, not decoded to a plausible
   wrong date.

### Mutation-tested

Passing tests are not the same as load-bearing tests, so the suite was checked
by breaking the code on purpose — 28 plausible wrong edits, each applied, run,
and reverted.

- **26 were caught** immediately by an existing test.
- **1 survived and was a real hole**: disabling secret redaction entirely broke
  nothing. Investigating found two genuine leaks, now fixed and covered (§7).
- **1 survived and was correct**: removing `except TypeError` from
  `_dates_agree`. Three redundant guards protect that comparison and the other
  two still hold — a fuzz over malformed date strings confirms the branch is
  unreachable. It stays as a documented backstop, not as coverage.

### Reviewed adversarially

An automated multi-lens review (dates, counting, failure handling, parsing)
found **12 defects that survived independent verification** — all now fixed
with a regression test each. They are worth recording because they were all the
same *kind* of bug, and it is the kind this system is most exposed to:

| Defect | Why it mattered |
|---|---|
| A naive `date_posted` from the vendor raised `TypeError` | An **advisory** cross-check field took down the entire LinkedIn column. |
| A non-list snapshot body (`{"status":"running"}`) decayed to `[]` | Bright Data answers 200 with an object in several non-success states. Reported 0 posts for everyone as fact. |
| No dedupe of vendor records | A repeated snapshot row inflated one person's count. |
| All-undecodable records returned `ok=True` | If LinkedIn changes its URL shape, every row reads 0 rather than `?`. |
| A failed per-post blog fetch left `published_on=None` | The post was dropped from someone's count while the column still claimed to be healthy. |
| `FetchResult.warnings` never reached the report | The scraper's own *"is undercounting"* warning rendered as confident numbers. |
| "This is a real zero" printed despite warnings | Converted an unknown into an explicit claim of certainty — the worst version of the bug. |
| CMS stamp dated in UTC, compared against local dates | In IST, everything from 18:30Z shifted a day, moving posts between weeks. |
| An unreadable editorial date fell back to the CMS stamp | Silently re-dated posts onto a field documented as wrong by up to 19 days. |
| Slack context hardcoded "Mon-Sun" | Contradicted the header for 6 of the 7 possible anchor days. |

A twelfth defect surfaced only once the warning path above was fixed, and it is
worth recording because it was **mine, and it was a guardrail crying wolf**:
the pagination check warned whenever the listing had a `w-pagination-next`
link. Webflow renders that link whenever any collection list on the page has
pagination enabled, even with no page 2 of content — following it returns the
same cards. So the warning fired on *every* run, which both trained the reader
to ignore warnings and (because the "real zero" note is now gated on warnings)
would have permanently suppressed that note. The fix was to stop warning and
actually follow pagination, stopping when a page adds nothing new. A guardrail
that always fires is worse than no guardrail.

Nine of the ten are the **same failure mode**: something goes wrong, and the
report presents a number anyway. That is what §8 predicted, and it is why the
`int | None` contract and the warning path are the two things to protect in any
future change.

### Living infrastructure

`uv run pytest` runs on every change, and in CI before the scheduled send
(`.github/workflows/weekly-kpi.yml` runs the suite *before* posting). Every
production failure becomes a case in the suite the same week.

### Launch bar

Before this posts to the CTO unattended:

- [x] 100% of the code eval suite passing.
- [x] Every guardrail has a case proving it fires and a case proving it does not.
- [x] Blog scraper verified against the live site, output checked by hand.
- [ ] One real Slack message sent to yourself and read on desktop **and** mobile.
- [ ] Two consecutive weeks in dry-run mode where the table matches a manual count.

---

## 7. Guardrails (rubric 2.7)

Evals measure offline; guardrails run on the live report and can hold it back.

### Input guardrails — mostly not applicable, and here is why

| Guardrail | Applies? | Reason |
|---|---|---|
| Input sanitisation | Partial | The only inputs are config files we own. They are schema-validated on load (`config.py`). |
| Scope filtering | No | No free-text request to be in or out of scope. |
| PII detection | No | Names and public profile URLs of colleagues, deliberately. Nothing sensitive reaches a model, because nothing reaches a model. |
| Prompt-injection detection | **No** | There is no prompt. Scraped blog text never reaches an interpreter — it is parsed for a date and a name, and both are pattern-validated. |
| Jailbreak detection | No | No free-text input, no external users. |
| Sensitive-data blocking | Yes | Tokens come from the environment and are redacted in two places — see below. |
| Memory-write sanitisation | No | No memory. |

### Output guardrails — the ones that matter here

All are data-integrity checks, because the failures this system actually has
produce a *plausible* report rather than an error.

| Guardrail | Trigger | Action | Fallback |
|---|---|---|---|
| `both_sources_down` | Neither source returned data | **Block** | Nothing sent; operator fixes and re-runs. |
| `linkedin_collapse` | LinkedIn "succeeded" with 0 posts for everyone, after non-zero weeks | **Block** | Named as a probable silent scraper break. With no history to compare, notes instead of blocking. |
| `implausible_count` | Any count > 25/week | **Block** | Means comments or reshares are being counted. |
| `no_members` | Empty table | **Block** | An empty report is noise. |
| `empty_cells` | A cell would render as `""` | **Block** | Slack rejects the whole message with `invalid_blocks`. |
| Unknown ≠ zero | A source failed | Note | Renders `?` and says so under the table. |
| Unattributed post | A byline matches nobody | Note | Named in the message so the roster gets fixed. |
| Action-boundary | — | Enforced by construction | `--send` is required; a dry run cannot reach the Slack client, and the send is only recorded to history after Slack returns a `ts`. |

### Secret redaction, in two places

Redaction runs at two choke points, because tokens travel down two very
different paths and a single filter misses one of them.

1. **`logging_setup.RedactingFormatter`** — redacts the *fully formatted* log
   line. This has to be a formatter, not a `logging.Filter`: a filter only sees
   `record.msg` and `record.args`, so it never sees the traceback that
   `logging.exception()` renders from `exc_info`, and it cannot safely touch a
   non-string argument (`log.error("failed: %s", exc)` passes an `Exception`,
   not a `str`). Formatting first and redacting afterwards covers message,
   arguments and traceback uniformly, with no `%d` hazard.

2. **`FetchResult.failed()`** — redacts the error text at construction. This
   one is not about logs at all: `FetchResult.error` is quoted in a note under
   the table **in the CTO's Slack DM** and written to `data/history.jsonl`. A
   vendor client is entitled to put the request it attempted, headers included,
   into an exception message.

Both are mutation-tested: disabling either one fails the suite.

**Strictness: strict.** This posts to the CTO about named colleagues. A
blocked report costs one Slack message; a confident wrong one costs someone's
credibility in a standup.

**Layering:** every check is a comparison over integers, so ordering is not a
latency concern here — the cheap-before-expensive rule has nothing expensive to
order against.

**Rollout:** all of the above ship on day one. There is no phase two, because
the set is small and each check is a few lines.

---

## 8. The failure this system will actually hit

Not a model failure. **A scraper that starts returning nothing without
erroring** — LinkedIn changes a response shape, or Webflow's generated class
names change on a blog redesign.

Three decisions already made against it:

1. `FetchResult.ok` — an empty result is only ever "zero" when the source
   genuinely succeeded.
2. `linkedin_collapse` — needs `history.jsonl`, which is why it is written from
   run one rather than added later.
3. The blog source treats zero parsed cards as a **failure**, not an empty
   week, because Webflow class names are design-tool output and will change.

## 8b. What the first live run actually taught us

Three defects only appeared once real credentials were in place, which is the
argument for doing a live dry run before trusting any of this:

1. **Nothing loaded `.env`.** The CLI read `os.environ` and only the scheduled
   wrapper sourced the file, so `check` reported a missing token that was
   sitting right there in `.env`. Now loaded at startup, with real environment
   variables taking precedence so CI secrets still win.
2. **A transient 400 from Bright Data killed the run.** A byte-identical
   request replayed minutes later succeeded. A once-a-week job that does not
   retry turns a two-second blip into a missing week, so the trigger now
   retries three times with backoff, honours `Retry-After`, and refuses to
   retry 401/403 where retrying cannot help.
3. **The error discarded the vendor's explanation.** `raise_for_status()` gives
   you `HTTP 400` and nothing else. Every Bright Data error now carries the
   response body, which is the difference between a diagnosis and an
   afternoon of guessing.

4. **A per-profile failure rendered as a confident `0`** — the worst of the
   four, and the only one that produced a false statement rather than an
   outage. Bright Data returns a *flat list of posts*, so a profile it fails
   to scrape simply has no rows, which is byte-for-byte indistinguishable from
   a person who published nothing. A real crawler error
   (`"Crawler error: Minimal layout detected"`) on a colleague's profile
   therefore reported **0 posts for his week** — presented to the CTO as fact.

   The vendor *does* report these, but only when the request asks:
   `include_errors=true`. It is now always sent, error rows are detected before
   anything tries to parse them as permalinks, and the affected people are
   carried through the pipeline in `FetchResult.unknown_keys` so they render
   `?` and are **named** under the table. If every profile fails the whole
   column fails, rather than producing a table of question marks.

   This is the §2 contract failing at a granularity the contract did not
   originally cover: it was written per *source*, and the real world failed
   per *person*. `FetchResult.unknown_keys` and `is_unknown()` close that gap,
   and the collapse guardrail now ignores a column containing any `?`, because
   the collapse signal is "we asked everyone and every answer was zero".

5. **We were asking the vendor to filter by date, and that was the bug.** The
   first live run reported `0` for all five people. It was not a quiet week —
   the vendor had scraped nobody. Its own message explained why:

   > *"Total posts: 17, with dates: 6. No posts found for the selected period
   > ... not all posts contain dates, so they will be filtered out if the date
   > is specified in the input. For a better experience, we recommend leaving
   > input.start_date, input.end_date empty."*

   Server-side filtering discarded 11 of 17 posts and returned nothing for
   every profile. Removing `start_date`/`end_date` produced 16 posts and 4 of
   5 profiles healthy.

   This vindicated an early decision made for a different reason. The design
   already treated the vendor as a **permalink enumerator** and derived every
   timestamp from the activity URN, specifically so it would never depend on
   vendor date handling (§ the `linkedin_urn` docstring). That turned out to be
   load-bearing: our decoder dates *all* the posts, not the 6 the vendor could
   parse. Fetching a whole profile and windowing locally costs ~85 records a
   week against a 5,000/month free tier.

   A follow-on subtlety: the vendor phrases "no posts in this period" as an
   *error* row too. The per-person fix above would have rendered those as `?`.
   They are now recognised as genuine zeros, while unrecognised errors still
   become `?` — biased deliberately, because a failure read as zero is a false
   claim about someone's week, whereas a zero read as a failure is a `?` you
   can re-run.

The reassuring part: when the transient 400 hit, the table rendered `?` for all
five people, stated the reason, and sent nothing. The zero-vs-unknown contract
held there unprompted — it was the per-person case that needed closing.

### What mutation testing caught in the fixes themselves

Two of the guards added while fixing the above turned out to be **redundant**,
and mutation testing is what exposed it: deleting them broke no test. The
row-level comparison `all(row.blogs == 0)` already excludes unknowns, because
an unknown row is `None` and `None == 0` is false. One of them was also subtly
*wrong* — it suppressed the "real zero" note when the vendor failed on a
profile that is not even on the roster, where every member's number is in fact
known. Both were removed rather than kept and tested, because the simpler
condition is the exact one. The guardrail's early return was kept, but only for
its explanation: the fallback reaches the same verdict while reporting it as
"some posts were found", which is false and misdirects the reader. Its test now
asserts the reason string, not just the outcome.

## 8c. Confirmed working end to end

First real send: **2026-09-07**, week Thu 2026-08-27 → Wed 2026-09-02, delivered
to the CTO's DM (`ts=1788784986.507629`). Every number was cross-checked against
an independent pull of all 16 posts across the team before sending, and the
table matched the prediction exactly, including the one blog post inside the
window.

Two further defects were found by *doing* this rather than testing it:

6. **The Slack token type was never validated.** `check` reported
   "slack token OK (set)" for an `xapp-` app-level token, which cannot call
   `chat.postMessage` and fails at send time with a bare `invalid_auth`. Slack
   issues five token shapes with similar names and only `xoxb-` works here.
   `check` now names the exact mistake, and `--send` refuses rather than
   discovering it in front of the recipient.

7. **Idempotency collected before it skipped.** A re-run of an already-reported
   week triggered a full vendor snapshot, waited two minutes, and then decided
   not to send. Fetching is a side effect that costs credits, so a run whose
   only job is to do nothing must not pay for data first. The skip now
   short-circuits before any fetch (0.1s instead of ~150s) and `run()` returns
   `None` to say "nothing to report" rather than a report nobody should read.

8. **The poll timeout was a guess, and nearly cost a week.** Snapshot
   collection for six profiles was measured at 1m35s, 2m02s, 2m06s, 2m18s,
   2m19s — and then **9m39s**, a 6x spread with no visible cause. The ceiling
   was 10 minutes, so that run completed 21 seconds inside it; a marginally
   slower day would have discarded good data and reported the whole LinkedIn
   column as unknown. Raised to 30 minutes, because nothing waits on a weekly
   job and a tight ceiling silently loses a week's report. Long collections now
   log progress every 60s so a slow run looks slow rather than hung.

   Worth stating as a principle: the marker `—` is honest when we genuinely
   cannot get data, but it is *not* honest when we could have got the data by
   waiting. A timeout that fires on healthy-but-slow work manufactures the very
   uncertainty the design is built to report accurately.

**Known limitation, accepted:** one member's profile
(`balajinagarajkumar`) cannot be scraped — `crawl_error: Minimal layout
detected`, reproduced 6 times, with a colleague succeeding in the same request
as a control. Both URL spellings fail. The likely cause is his profile's EEA
region or a creator-mode layout variant; either way it is outside this
codebase. He renders `?` with the reason stated, which is the honest outcome.

## 8d. The MCP split, and what it is actually for

The two credential-holding boundaries moved into self-hosted MCP servers
(`kpi_mcp/`), each fetching its own secret from AWS Secrets Manager. The client
(`kpi_tracker/`) holds no credential and cannot reach AWS.

**Be precise about the benefit.** MCP exists so that *LLM agents* can discover
and call tools, and this system has zero LLM calls — so the protocol itself
buys it nothing today. What the split does buy is real:

* **Secret isolation.** The weekly job cannot read a token even if it wanted to.
* **A smaller blast radius.** Two secrets, two IAM statements, one ARN each.
  The LinkedIn server physically cannot read the Slack token.
* **Reuse.** Any other Moring agent can call these servers.

The isolation is *not* free, and this is the part most easily got wrong. Three
things are load-bearing, and without all three the split is decoration:

| Requirement | Why |
|---|---|
| **Streamable HTTP, not stdio** | stdio makes the client spawn and own the server as a child process. It cannot isolate a secret from its own parent. |
| **Separate Unix users** | Same-uid processes can read each other's memory and environment. |
| **An IMDS firewall rule** | EC2 instance metadata is *unauthenticated* — AWS's docs say any software on the instance can read it. Without a per-uid block on `169.254.169.254`, the client can fetch the same secret directly and the process split proves nothing. |

`deploy/block-imds-for-client.sh` does the third and ships with an acceptance
test, because it is the one that looks optional and is not.

### What was protected across the boundary

Adding a serialization boundary to a system whose whole contract is
"unknown ≠ zero" is the riskiest thing done to it so far. Four decisions guard it:

1. **`FetchResult` never crosses the wire.** The natural design — the server
   returns a list of posts — recreates §8b.4 exactly: a profile the vendor
   could not read simply has no posts, indistinguishable from a person who
   published nothing. Instead the server returns **one accounted-for entry per
   requested profile**, each with an explicit status, and a profile that is
   *absent* from the response is read by the client as failed. A dropped field
   degrades towards a dash, never towards a number.

2. **The URN decoder stayed client-side, and no dates cross the wire.** It is
   pinned by 16 tests against LinkedIn's own published values, and those tests
   do not run when a server is deployed. Moving it would let a `>> 23`
   regression — the exact bug its docstring warns about — ship unnoticed.

3. **`schema_version` is required and mismatches are refused**, rather than
   guessed at. Version skew between a restarted server and a running client is
   the normal state of something under active development.

4. **The async boundary is one file.** `mcp_call.py` catches `BaseException`,
   not `Exception`, because anyio raises `BaseExceptionGroup` and
   `asyncio.CancelledError` — neither of which is an `Exception` subclass. An
   escaping cancellation would have killed the blog column too, breaking the
   step independence in §2. It also flattens exception groups, because an
   unflattened one stringifies to "(1 sub-exception)".

### The Slack tool would have been an authorisation downgrade

Posting today requires reading a `chmod 600` file. A naive MCP port would mean
any process on the VM could `curl localhost` and message the CTO. So the
**destination is not a tool argument** — it is server-side configuration — and
the server enforces one report per week next to the token rather than trusting
the caller's history file. `post_alert` is deliberately separate and *not*
deduplicated: three failed weeks should be three messages.

### What the SDK actually does, versus what the internet says

Verified against `mcp==2.2.0` rather than taken from documentation:

* `FastMCP` was **renamed to `MCPServer`** and the old import path *removed*,
  not deprecated. Every 1.x-era tutorial fails immediately.
* A tool annotated `-> dict` publishes **no output schema** and returns
  `structured_content=None`. Only a pydantic (or TypedDict) return type gives
  the client a real dict — and gives `check` a schema to assert against.
* `ToolError` text **reaches** the client; a bare exception's text is
  **withheld** and only its traceback hits the server's stderr. So every
  expected failure is raised as `ToolError`, and a crash cannot leak a
  credential to the caller.
* The SDK's default read timeout is **300s** — already less than a measured
  9m39s run. It is overridden to 2100s, deliberately larger than the server's
  own 1800s vendor ceiling so the server times out first and returns a reason.

### Testing across the boundary

* **352 tests, still offline, still ~0.5s.** `mcp` is imported lazily inside
  one function, and `tests/test_import_boundary.py` fails if any client module
  imports `mcp`, `boto3` or `kpi_mcp` at module scope — checked both by AST and
  by importing the whole client in a subprocess and inspecting `sys.modules`.
* **A fake caller** covers every client semantic without a server.
* **Real in-process round trips** through actual `MCPServer` objects cover what
  a fake cannot: that the schema the server publishes contains the keys the
  client reads.
* **True end-to-end tests** with nothing stubbed but the vendor's HTTP —
  `Client()` accepts a server object, so the production `call_tool` itself is
  exercised in-process.
* The ~34 Bright Data tests moved to `test_mcp_brightdata.py` with their
  assertions intact, because every one of them records a real defect.

## 8e. The blog source was rewritten when the site was rebuilt

**2026-09-13.** moring.ai moved from Webflow to Astro. Every CSS class the
scraper depended on (`w-dyn-item`, `blog7_author-text`, `blog-meta-wrapper`)
disappeared, and it returned zero cards.

**It failed correctly.** The guard reported "we could not read this" — em
dashes with a stated reason — rather than a table of zeros, which is precisely
what its docstring predicted four days earlier:

> *"Webflow class names are generated by a design tool and change silently on a
> redesign. Zero cards means the selectors broke, which is a very different
> thing from a quiet week."*

That is the guard working. But the better lesson is that the guard was the
*second*-best answer: the best one was not to depend on design-tool class names
at all.

The rebuilt site publishes **`/rss.xml`**, which it never did before, and it is
strictly better as a source:

| | Old (HTML scraping) | New (RSS) |
|---|---|---|
| Requests per run | up to 12 | **1** |
| Enumeration | dedupe 26 cards → 10 posts, follow pagination | items, in order |
| Publish date | editorial date vs a CMS batch stamp that disagreed on 5 of 10 posts | `pubDate`, unambiguous |
| Fragility | generated CSS class names | a machine contract |
| HTML parsing | beautifulsoup4 | **stdlib** — the dependency is gone |

**What the rebuild cost us, and it is not nothing.** The old per-post JSON-LD
carried `author.sameAs` — the author's LinkedIn profile URL — which made
blog→person an *exact* join. The new site publishes only a display name (its
JSON-LD `author` is `{name, jobTitle}`; the only LinkedIn URL on the page is the
company's). So attribution now leans on name matching, which was previously the
fallback. All five current authors match, but a colleague who changes how they
are credited will start showing up as unattributed — reported under the table,
never silently dropped. A test documents the regression so that a future
redesign restoring an author URL fails loudly and the stronger join gets rewired.

One subtlety worth keeping: every `pubDate` in this feed is exactly
`00:00:00 GMT` — a *date* rendered as an instant. It is therefore taken as
written and **not** converted into the report timezone. That is the opposite of
what the old CMS timestamp needed (§8b.3), and deliberately so: that one was a
genuine instant. Converting this one the same way would move every post back a
day for any timezone behind UTC.

## 8f. The Lambda deployment, and what an adversarial review caught

A third deployment target (`deploy/lambda/`), chosen because this is a non-LLM
batch job and Lambda is cheap and needs no host to patch. The trade is stated
plainly: a Lambda is one process, so the MCP servers run in-process and the
function holds both tokens. The uid separation and IMDS block that made the EC2
isolation *structural* do not exist here.

Two Lambda facts drove most of the design:

* **The filesystem is read-only and thrown away.** `history.jsonl` and the
  Slack server's sent-weeks record must be in S3 or the collapse guardrail
  never gets a baseline and a retry posts the report twice. `storage.py` puts
  both behind one interface, and the handler refuses to start if either is a
  local path — checked up front, not after the vendor call.
* **The hard 900s timeout is below this project's 1800s poll ceiling.** So the
  ordering that makes a slow vendor produce a readable reason instead of an
  opaque kill inverts unless changed. Both ceilings now derive from one
  variable, `KPI_VENDOR_POLL_TIMEOUT`, so they cannot be set inconsistently.

### What the review found, after 12 other findings were refuted

| Defect | Why it mattered |
|---|---|
| `build.sh` stripped `*.dist-info` | `httpx2` reads its own version via `importlib.metadata` at import, so **every invocation** would fail with `PackageNotFoundError` — to save ~1 MB against a 50 MB limit. |
| IAM had no `s3:ListBucket` | S3 answers `GetObject` on a *missing* key with **403, not 404**, unless you hold `ListBucket`. `storage.read_text` deliberately refuses to treat AccessDenied as "missing", so the first run of a new deployment could never complete — and `PutObject` was never reached to create the object that would end it. |
| `pipeline.run` caught only `OSError` after a send | On S3 a write failure is a botocore `ClientError`. The report reaches the CTO, then the invocation fails, the alarm fires for a week that *was* reported, and the retry sends it **again**. |
| `KPI_SLACK_STATE_PATH` was unvalidated | `update-function-configuration --environment` replaces the whole environment, so flipping `AGENT_SEND` silently drops it — losing the only thing stopping a double post. |
| Two independent retry layers | EventBridge invokes Lambda *asynchronously*, so Lambda applies its own 2 retries on top of the schedule's — up to nine executions from one trigger. |
| `Path("s3://b/k")` normalises to `s3:/b/k` | pathlib collapses the double slash, so the URI was corrupted and failed only at write time. |
| `configure()` cleared root handlers | On Lambda that removes the runtime's own handler and costs the RequestId correlation. It now *adopts* handlers instead of replacing them. |

`build.sh` now refuses to ship a zip it cannot import: it resolves metadata for
every distribution (with a floor, because a stripped tree yields *zero* and a
loop over zero passes vacuously) and checks every `.so` is ARM aarch64.

### A test that passed for the wrong reason

Worth recording because it is easy to repeat. A mutation test showed the
history-path check could be deleted without failing anything. The assertion was
`match="history_path"` — and **pytest names `tmp_path` after the test
function**, so the *other* check's error message contained the string
`test_a_local_history_path_is_refused_on_lambda`, which contains
`history_path`. It matched the test's own name inside a temp directory. The
fix is an anchored pattern; the lesson is that a passing assertion is not
evidence until something has been broken to see it fail.

## 9. Vendor risk, recorded

LinkedIn has no usable official API for this: `r_member_social` is a closed
permission not accepting new requests, and the Member Data Portability route
requires each member to be in the EEA. That leaves third-party vendors, and
LinkedIn litigates against them — Proxycurl shut down in July 2025 after being
sued, and ProAPIs was sued in October 2025.

The mitigation is the `LinkedInSource` seam (`sources/base.py`). A vendor
disappearing is a new file and one config line, not a rewrite. `ManualCsvSource`
is the always-available floor: it needs no vendor at all, and it exists so the
report can still go out on a week when everything else is broken.
