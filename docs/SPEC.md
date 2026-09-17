# Problem and scope

The section-2 rubric (see [ARCHITECTURE.md](ARCHITECTURE.md)) cross-references a
section 1 that was never written for this project: "the users identified in
1.2", "ready to ship in 1.6", "the PII posture in 1.8". This document is that
section, written after the fact.

Writing it late is a real process failure and worth naming: several decisions
below were *discovered while building* rather than decided up front, and two of
them (the vendor constraint in §1.5, the unscrapeable profile in §1.7) would
have changed the plan had they been known on day one. Where that happened, it
says so.

---

## 1.1 The problem

Moring publishes thought-leadership content in two places: team members' own
LinkedIn posts, and the company blog at moring.ai. Nobody knew, week to week,
how much was actually being published or by whom.

Without that, the Friday standup conversation about content is anecdotal — the
loudest recent post stands in for the trend, and someone who has published
nothing for a month is indistinguishable from someone who published on Tuesday.

**The job:** once a week, tell the CTO how many LinkedIn posts and how many
blog posts each tracked person published in the preceding seven days.

Deliberately *not* the job: telling anyone whether the content was any good.

## 1.2 Users

| Who | Relationship | What they need |
|---|---|---|
| **Rajan** (CTO) | Reader | A table he can read in ten seconds on his phone on Thursday morning, and trust without checking. |
| **Ashvath** | Operator | To add or remove a tracked person by editing one file; to know when a run fails. |
| **The six tracked people** | **Subjects** | To not be misrepresented. A wrong number about their week is the failure that matters. |
| **Marketing** | Channel owner | The report lands in `#marketing-kpi`; this is a marketing measure, not a performance review. |

The third row drives most of the engineering. The system's readers are few and
forgiving; its *subjects* are named colleagues who never see the input data and
cannot correct it. That asymmetry is why "unknown" and "zero" are kept apart
everywhere (see ARCHITECTURE.md §2).

## 1.3 In scope

- Six named people, listed in [`data/members.json`](../data/members.json).
- **LinkedIn**: posts authored by that person. Reshares excluded.
- **Blog**: posts on moring.ai attributed to that person.
- A seven-day window, Thursday→Wednesday, in `Asia/Kolkata`.
- One Slack message per week to one configured destination.
- A stated target (3 LinkedIn posts/person/week) shown in the table header.

## 1.4 Explicitly out of scope

Each of these was considered and rejected, not merely forgotten:

| Not doing | Why |
|---|---|
| Engagement metrics (likes, comments, reach) | Measures the audience, not the output. Different question, different system. |
| Content quality or sentiment | Requires judgement; would need a model, an eval harness, and a much harder conversation about fairness to the subjects. |
| Platforms beyond LinkedIn and the Moring blog | No demand yet. |
| Historical backfill | Only "since we started measuring" is defensible; back-dated numbers invite arguments about incomplete data. |
| Per-post detail in the report | The table is a trend indicator. Detail is one CLI command away (`kpi-tracker blogs`). |
| Real-time or on-demand reporting | Weekly is the cadence of the decision it feeds. |
| Ranking or leaderboards | The table is in roster order on purpose. |

## 1.5 Constraints discovered while building

These were **not** known at the start. Each changed the design.

1. **There is no usable official LinkedIn API.** `r_member_social` is closed to
   new applicants, and the Member Data Portability route requires each member
   to be in the EEA. Every route is a third-party vendor, and LinkedIn
   litigates those — Proxycurl shut down in July 2025 after being sued. The
   mitigation is a swappable source seam plus a manual-CSV fallback that needs
   no vendor at all.

2. **Vendor collection time varies 6×** — measured 1m35s to 9m39s for six
   profiles. This set the timeout ceilings and killed any idea of a synchronous
   or interactive report.

3. **The vendor's own date filter is lossy.** It silently discarded 11 of 17
   posts. All date handling is therefore done locally from the post permalink.

4. **Blog publish dates are ambiguous.** The site's JSON-LD `datePublished` is
   a CMS batch stamp — three posts share one millisecond. The human-typed
   editorial date is authoritative.

## 1.6 Ready to ship

The gate, as met today:

- [x] 367 code tests passing, offline, under a second.
- [x] Every guardrail has a case proving it fires and one proving it doesn't.
- [x] Mutation tested: deliberate breakages introduced and confirmed caught.
- [x] Numbers cross-checked by hand against an independent data pull.
- [x] One real message rendered by Slack and read on a real client.
- [x] Secret isolation verified: the client holds no credential and `check` fails if it finds one.
- [ ] **Two consecutive weeks where the table matches a manual count.** Not yet done.
- [ ] **The collapse guardrail armed.** Needs 2–3 weeks of history; until then it notes rather than blocks.
- [ ] **AwsSecrets exercised against real Secrets Manager.** Only the env-var backend has run.

The last three are the honest reasons this is "deployable" rather than "proven".

## 1.7 Known limitations

- **One profile cannot be read.** `balajinagarajkumar` returns
  `crawl_error: Minimal layout detected`, reproduced seven times with
  colleagues succeeding in the same request. He renders `—` with the reason
  stated. Vendor-side; not fixable here.
- **A quiet week and a broken week look similar to a casual reader.** Mitigated
  by naming the unknowns under the table and by the collapse guardrail once it
  has history.
- **Single vendor.** Documented in ARCHITECTURE.md §9, mitigated by the source seam.

## 1.8 PII posture

**What is stored:** each tracked person's display name, public LinkedIn profile
URL, and blog byline — in `data/members.json`, committed to the repo. Plus
per-week counts and post URLs in `data/history.jsonl`, which is **not**
committed.

**What is not stored:** post text, engagement figures, follower counts,
anything from a private profile, and anything about anyone not on the roster.
Blog posts by non-roster authors are counted as unattributed and named once in
the message, never recorded.

**Who can see it:** members of `#marketing-kpi`. Per-person counts are visible
to the other people in the table — a deliberate choice, since a colleague
should be able to see that their own profile is not being read.

**Credentials:** never on the client, never in the repo, never in a log. Held
by the MCP servers, fetched from AWS Secrets Manager, and stripped from every
log line and every error string by [`redaction.py`](../kpi_tracker/redaction.py).

**Retention:** `history.jsonl` grows forever and has no eviction policy. Fine at
~50 lines a year; it would need a decision before this pattern is reused at
larger scale.

**Not addressed:** the tracked people have not been asked for consent, and
there is no process for someone to opt out. For an internal marketing measure
of already-public output this is probably proportionate, but it is a decision
that was never explicitly taken, and it is the one item on this page most
worth revisiting.
