# data/members.json

The only file you edit to change who is tracked. One object per person:

```json
{
  "name": "Ashvath Narayan",
  "linkedin_url": "https://www.linkedin.com/in/ashvath-narayanan/",
  "blog_author": "Ashvath Narayan"
}
```

| Field | Required | What it is |
|---|---|---|
| `name` | yes | The name shown in the Slack table. |
| `linkedin_url` | yes | Paste the profile URL straight from the browser. Trailing slash, `www`, and `?trk=...` parameters are all fine — it gets normalised on load. |
| `blog_author` | no | How this person is credited on moring.ai. Accepts either the byline (`"Ashvath Narayan"`) or the author-page slug (`"/authors/ashvath-narayan"`). Only needed when the byline differs from `name`. |

## How a blog post gets attributed

In this order, first match wins — all exact, no guessing:

1. **LinkedIn URL** from the post's JSON-LD `author.sameAs`, compared against `linkedin_url`. This is the strongest key and it works today for every post on the site.
2. **`blog_author`**, compared against the post's byline *and* its author-page slug. `"Balaji Nagaraj"` and `"/authors/balaji-nagaraj"` both reduce to `balaji-nagaraj`, so either form works.
3. **`name`**, same comparison, as the last resort.

A post that matches nobody is **not** silently dropped — it is counted as
unattributed and named in the Slack message, so you can add the missing person
rather than wonder why a number looks low.

## Adding someone

Add the object, run `uv run kpi-tracker check` to validate the file, then
`uv run kpi-tracker run` to see them in the table. Duplicate LinkedIn profiles
are rejected on load.
