# Trend Tracker — Build Plan

A weekly email digest of new investments announced by 14 VC firms, built on the
architecture of `vc-job-agent`, styled with the Sage design system Card component.

> **Note on location:** this project was originally scaffolded inside `vc-job-agent/`
> and has since been moved out to its own standalone repository. References to
> vc-job-agent below are to it as a *reference implementation* — a sibling checkout
> that this repo borrows architecture from but does not depend on at runtime.

---

## 0. Read this first: what actually carries over from vc-job-agent

I audited the repo before writing this plan. One finding changes the shape of the work:

**The URLs in this project are different sites from the ones vc-job-agent scrapes.**

vc-job-agent targets *job boards* on separate subdomains — `jobs.greylock.com`,
`portfoliojobs.a16z.com`, `careers.nea.com` — and 9 of its 14 scrapers are thin
subclasses of `ConsiderScraper`, which POSTs to a single shared
`/api-boards/search-jobs` endpoint. Those scrapers are ~8 lines each because the
platform does the work.

Trend Tracker targets *editorial blog pages* on the firms' main marketing sites —
`greylock.com/blog/portfolio-news`, `a16z.com/news-content`, `nea.com/blog`. These
are 14 unrelated CMSs with no shared API. **No per-site selector logic transfers.**
Expect 14 genuinely custom parsers, not 14 one-line subclasses.

What *does* transfer is the architecture, and it transfers well:

| vc-job-agent | Trend Tracker | What it gives us |
|---|---|---|
| `src/history.py` | `src/state.py` | URL-keyed JSON memory with `first_seen`/`last_seen` + pruning. **This is the "what's new since last week" engine.** |
| `BaseScraper.fetch_job_detail()` | `BaseSource.fetch_post_detail()` | The "follow a link to a second page and scrape its content" logic you asked to reuse — selector cascade, `<script>`/`<nav>` stripping, section extraction between headings |
| `BaseScraper` session setup | `BaseSource` | Browser UA headers, `fetch_page`, `fetch_json`, `extract_embedded_json`, `clean_text`, rate-limit delay |
| `main.py` fan-out | `main.py` | `ThreadPoolExecutor` parallel scrape + per-source results table + graceful per-source failure |
| `matcher.py` | `extractor.py` | Pattern for a structured Claude call: big prompt → strict JSON → parse into a dataclass |
| `emailer.py` | `emailer.py` | Dual Gmail SMTP / SendGrid provider with env-var detection |
| `.github/workflows/daily-job-scan.yml` | `weekly-trend-scan.yml` | Cron + pip cache + Playwright install + secrets wiring |

Two things in the existing repo are worth fixing rather than copying — see
[§9 State persistence](#9-state-persistence--fix-this-dont-copy-it) and
[§7 Email constraints](#7-the-sage-card-in-email).

---

## 1. Architecture

```
                    ┌──────────────────────────┐
                    │  14 VC blog index pages  │
                    └────────────┬─────────────┘
                                 │  parallel fan-out (ThreadPoolExecutor)
                                 ▼
                    ┌──────────────────────────┐
                    │  sources/*.py            │  → list[BlogPost]  (url, title, date?)
                    │  tier A/B/C/D per site   │
                    └────────────┬─────────────┘
                                 ▼
                    ┌──────────────────────────┐
                    │  state.py  NEW-POST DIFF │  ← data/seen_posts.json
                    │  layer 1: URL            │
                    │  layer 2: content hash   │
                    └────────────┬─────────────┘
                                 │  only net-new posts continue
                                 ▼
                    ┌──────────────────────────┐
                    │  fetch_post_detail()     │  → full post body
                    └────────────┬─────────────┘
                                 ▼
                    ┌──────────────────────────┐
                    │  extractor.py            │  1. is this an investment? (gate)
                    │  regex pass → Claude pass│  2. company/$/round/sector/url
                    └────────────┬─────────────┘
                                 ▼
                    ┌──────────────────────────┐
                    │  state.py  DEDUP layer 3 │  ← data/seen_investments.json
                    │  same round, 2 firms     │     merge, don't duplicate
                    └────────────┬─────────────┘
                                 ▼
                    ┌──────────────────────────┐
                    │  trends.py               │  sector rollup + 4-week deltas
                    └────────────┬─────────────┘
                                 ▼
                    ┌──────────────────────────┐
                    │  emailer.py (Sage cards) │  → Gmail SMTP / SendGrid
                    └──────────────────────────┘
```

### Repo layout

```
trend-tracker/
├── README.md
├── PLAN.md
├── SOURCE_MATRIX.md          # output of Phase 1 — living doc
├── requirements.txt
├── .env.example
├── .github/workflows/weekly-trend-scan.yml
├── data/
│   ├── seen_posts.json       # dedup layer 1 + 2
│   ├── seen_investments.json # dedup layer 3
│   └── weekly_history.json   # for 4-week trend deltas
└── src/
    ├── main.py
    ├── models.py             # BlogPost, Investment, SourceSummary, WeeklyDigest
    ├── state.py              # ported from history.py
    ├── extractor.py          # regex + Claude structured extraction
    ├── sectors.py            # FIXED sector enum — see §6
    ├── trends.py             # rollup + week-over-week deltas
    ├── weekrange.py          # week window + subject line
    ├── emailer.py
    ├── sage.py               # Sage design tokens as email-safe constants
    └── sources/
        ├── base.py           # BaseSource
        ├── rss_base.py       # tier B shared
        ├── a16z.py  sequoia.py  general_catalyst.py  index_ventures.py
        ├── greylock.py  kleiner_perkins.py  accel.py  contrary.py
        ├── battery.py  nea.py  antler.py  lsvp.py  bessemer.py
        └── designer_fund.py
```

---

## 2. Data model

```python
@dataclass
class BlogPost:
    url: str                      # canonical, query-stripped — the dedup key
    title: str
    vc_firm: str
    published_date: datetime | None   # often unavailable; see §4
    body: str = ""                    # filled by fetch_post_detail()
    content_hash: str = ""            # sha256 of normalized title+body

@dataclass
class Investment:
    company_name: str
    company_url: str | None           # requirement: link to the company's site
    company_description: str          # requirement: what the company does
    sector: str                       # from sectors.SECTORS — fixed enum
    funding_amount_usd: int | None    # normalized to dollars for math
    funding_amount_raw: str           # "$30M" as written, for display
    round_stage: str                  # "Seed", "Series A", ... "Unknown"
    co_investors: list[str]
    vc_firms: list[str]               # plural — a round can be announced by several
    source_post: BlogPost
    confidence: str                   # "high" | "medium" | "low"
```

`funding_amount_usd` must be a normalized integer, not a string. Without it the
sector totals in §6 can't be summed and the whole trend requirement fails.

---

## 3. Source strategy — probe, don't guess

Do **not** write 14 parsers up front. vc-job-agent's `SCRAPER_DIAGNOSIS.md` is a
post-mortem of exactly that mistake: five extraction strategies were written
against an assumed platform, and all five failed because nobody checked what the
pages actually served first.

Phase 1 is a recon pass that classifies each of the 14 sites into a tier and
records the finding in `SOURCE_MATRIX.md`:

| Tier | Signature | Approach | Cost |
|---|---|---|---|
| **B** | Has RSS/Atom/JSON feed | `feedparser` on `/feed`, `/rss`, `/feed.xml`, or WordPress `/wp-json/wp/v2/posts` | Cheapest, most reliable, gives real dates. **Probe for this first on every site.** |
| **A** | Post links present in initial HTML | `requests` + BeautifulSoup | Cheap |
| **D** | Empty container div + XHR to a JSON endpoint | Call the JSON endpoint directly | Cheap once found; find it in DevTools → Network |
| **C** | Client-rendered, no usable endpoint | Playwright chromium, `wait_for_selector` | Slow, brittle — last resort |

Recon checklist per site: does the raw HTML contain a post title? Is there a
`<link rel="alternate" type="application/rss+xml">`? Does `sitemap.xml` list posts
with `<lastmod>`? Is there a `__NEXT_DATA__` or `__NUXT__` blob? What XHR fires on
load?

Note: `playwright` is already in vc-job-agent's `requirements.txt` and its workflow
runs `playwright install chromium --with-deps`, but **no scraper actually imports
it** — so tier C is unproven in this codebase. Budget time for it.

The 14 sources:

| Firm | Index URL |
|---|---|
| a16z | https://a16z.com/news-content/ |
| Sequoia | https://sequoiacap.com/stories/?_story-category=news |
| General Catalyst | https://www.generalcatalyst.com/stories |
| Index Ventures | https://www.indexventures.com/perspectives/ |
| Greylock | https://greylock.com/blog/portfolio-news/ |
| Kleiner Perkins | https://www.kleinerperkins.com/perspectives/category/announcements |
| Accel | https://www.accel.com/news/portfolio |
| Contrary | https://contrary.com/blog |
| Battery Ventures | https://www.battery.com/news/ |
| NEA | https://www.nea.com/blog?type=Read&page=1&topic=investment |
| Antler | https://www.antler.co/newsroom |
| LSVP | https://lsvp.com/stories/ |
| Bessemer | https://www.bvp.com/news |
| Designer Fund | https://designerfund.com/blog |

Two notes on specific URLs. NEA's already carries `?topic=investment&page=1` —
keep the topic filter, and treat `page` as a pagination parameter to walk (stop
after ~3 pages or when a page yields only already-seen URLs). Sequoia's
`?_story-category=news` is a CMS filter; verify it survives a plain fetch rather
than being applied client-side.

Every source subclasses `BaseSource` and returns `list[BlogPost]`. A source that
throws must not kill the run — copy `main.py`'s per-future try/except and log the
failure into the summary table so a silently-broken source is visible in the email
footer rather than just vanishing.

---

## 4. "New since last week" — three dedup layers

This is the core requirement and it needs more than one mechanism.

**Layer 1 — post URL.** Direct port of `history.py`'s `load_scraped_jobs()` /
`update_scraped_jobs_memory()`. `data/seen_posts.json` maps canonical URL →
`{first_seen, last_seen, vc_firm, title}`. A URL already in the map is dropped
before any detail fetch, which is also the main cost saver. Canonicalize first:
strip `utm_*` and other query params, strip trailing slash, lowercase the host.

**Layer 2 — content hash.** Some CMSs change slugs or serve the same post at two
paths. Hash normalized `title + body`, store alongside. Catches re-slugged reposts
that layer 1 misses.

**Layer 3 — investment identity.** The one layer vc-job-agent has no analogue for,
and the one that matters most for a syndicate-heavy dataset. When a company raises
a Series B, *several* firms in the round each blog about it. Layer 1 sees three
distinct URLs and lets all three through — you'd get the same round three times in
one email, and triple-count its dollars in the sector totals.

Key on `(normalized_company_name, round_stage)` in `data/seen_investments.json`.
Normalize the name: lowercase, strip `Inc.`/`Ltd.`/`Corp.`, collapse whitespace.
Within a single run, **merge** matches into one `Investment` with all firms in
`vc_firms` — the card then reads "Sequoia · Index Ventures". Across runs, drop it.

**Why dates are a secondary filter, not the primary one.** Many VC index pages
show no date at all, some show relative dates ("2 weeks ago"), and posts get
backdated or edited after publication. Driving "new" off parsed dates would be
unreliable in both directions. So: the URL/hash diff decides what's new; a parsed
date, *when present*, is only used to reject posts older than ~60 days (a genuinely
old post surfacing for the first time is probably an archive reshuffle, not news).
`BaseScraper.extract_posted_date()` already handles a dozen date formats with a
`dateutil` fallback — port it as-is.

**Cold start.** On the first run every post looks new — potentially hundreds. Ship
a `--seed` flag that scrapes, writes `seen_posts.json`, and exits without emailing.
Then the first real run has a clean baseline. Without this, email #1 is unusable.

---

## 5. Extraction — gate first, then parse

**Step 1: the classifier gate.** Most posts on these blogs are *not* funding
announcements. They're essays, market maps, podcast episodes, partner hires,
portfolio-company product launches. `contrary.com/blog` and
`indexventures.com/perspectives` are mostly long-form writing. Without a gate the
digest fills with noise and the trend math is meaningless.

Ask Claude for a boolean plus a reason: is this post announcing a specific,
named company raising a specific round? Everything else is dropped and logged.

**Step 2: regex pass (cheap, deterministic).**

- Amount: `\$\s?([\d,.]+)\s?(million|billion|M\b|B\b|bn\b)` → normalize to int USD
- Stage: `pre-seed | seed | series [A-J] | growth | bridge | extension`

**Step 3: Claude pass (structured JSON)** for what regex can't do — company
description, canonical company URL, sector assignment, co-investors, and
disambiguating the amount.

**The valuation trap — call this out explicitly in the prompt.** Announcements
routinely contain several dollar figures: `"raised $30M Series B at a $300M
valuation, bringing total funding to $52M"`. Naive regex grabs the largest number
and reports a $300M round. The prompt must require *the amount raised in this
round*, and the code should prefer a Claude-returned amount that appears adjacent
to a round keyword over the raw regex maximum. Set `confidence: "low"` when the
figures disagree, and surface low-confidence rows visibly in the email rather than
silently trusting them.

Also handle undisclosed rounds — plenty of posts name no figure.
`funding_amount_usd = None`, `funding_amount_raw = "Undisclosed"`. Do not drop
these; they still count as deal-flow signal in the deal-count column.

Follow `matcher.py`'s conventions: prompt constant at class level, strict
"return only JSON" instruction, `json.loads` wrapped in try/except with a logged
fallback, and truncate post bodies (~8k chars) before sending.

---

## 6. Sector trends — fixed taxonomy

Requirement two is understanding money-by-sector over time. That only works if the
sector label is **stable across weeks**. If Claude free-forms the sector, week 1
says "AI infrastructure", week 2 says "ML platform tooling", and nothing is
comparable.

Define a closed enum in `sectors.py` and instruct Claude to pick exactly one,
falling back to `Other`:

```
AI Infrastructure · AI Applications · Developer Tools · Fintech ·
Healthcare & Bio · Enterprise SaaS · Security · Consumer · Robotics & Hardware ·
Climate & Energy · Defense & Gov · Commerce & Retail · Data & Analytics ·
Crypto · Other
```

Optionally keep a free-text `sub_sector` alongside for color — but never trend on it.

`trends.py` produces, from this week plus `weekly_history.json`:

- **Headline:** total capital deployed, deal count, firms active
- **Sector table:** deals, total $, % of week's capital, Δ vs 4-week average
- **Stage mix:** how much went to Seed vs Series A vs later
- **Most active firm** this week
- **Movers:** sectors up/down most vs the 4-week average

For deltas, reuse the pattern in `history.py`'s `apply_rank_deltas()` — load the
prior period, diff, and store the current period. Keep 12 weeks of history and
prune past that (`save_scraped_jobs()` already demonstrates time-based pruning).

Rendering an `Undisclosed` round: count it in deals, exclude it from dollar sums,
and footnote the count so the totals aren't silently understated.

---

## 7. The Sage Card in email

Mapping `design-system/.storybook/src/components/Card/Card.tsx` (`variant="card"`)
onto an investment:

| Card slot | Content | Sage token |
|---|---|---|
| `label` | VC firm(s) | 12px Spectral 300, `#827A64`, tracking `-0.72px` |
| `heading` | Company name | 20px Rethink Sans 300, `#1B2323`, tracking `-0.4px` |
| slot block | `$30M · Series B` | bg `brand.accent` `#E8DDA2`, text `#1B2323` |
| `body` | What the company does | 14px Spectral 300, line-height 1.5, `#59554b` |
| tag chip | Sector | bg `data.paleMustard` `#D9D059`, 8px, `#59554b` |
| action link | "Visit site →" | `primary` `#1AAED8` |
| container | — | bg `layer1` `#FFF8F0`, border `0.5px solid #ADABA5`, radius 8px |

Extract these into `src/sage.py` as named constants rather than scattering hex
codes through f-strings, so a token change is one edit.

**Five email constraints that will break a direct port of Card.tsx:**

1. **Tailwind classes do not work in email.** `Card.tsx` is entirely utility
   classes and there's no build step in this pipeline. Every class must be
   hand-translated to an inline `style` attribute.
2. **Flexbox is unreliable in Outlook.** `Card.tsx` uses `flex` throughout. Use
   nested `<table>` layout instead. vc-job-agent's `emailer.py` already builds cards
   as inline-styled divs — extend that approach rather than the React one.
3. **Webfonts are blocked by Gmail.** Rethink Sans and Spectral won't load. Declare
   fallback stacks — `'Rethink Sans', system-ui, sans-serif` and `Spectral, Georgia,
   serif` — and accept that most readers see Georgia + system sans. The Sage
   *palette* survives, which carries most of the identity.
4. **`max-w-[248px]` is a component-grid width,** far too narrow for a digest.
   Use a 600px container with full-width stacked cards.
5. **Gradients degrade in Outlook.** The `profile` variant's radial gradient won't
   render; stick to the `card` variant's solid `#FFF8F0`.

Also: dark-mode inversion in Apple Mail can wreck a warm cream palette. Set
explicit `color` on every text node rather than relying on inheritance, and test
in dark mode.

### Subject line

Requirement: `Trend Tracker August 2-8 2026`.

August 2, 2026 is a Sunday and August 8 is a Saturday, so the week window is
**Sunday–Saturday**. `weekrange.py`:

```python
def week_label(end: date) -> str:
    start = end - timedelta(days=6)
    if start.month == end.month:
        return f"{start:%B} {start.day}-{end.day} {end.year}"               # August 2-8 2026
    if start.year == end.year:
        return f"{start:%B} {start.day} - {end:%B} {end.day} {end.year}"    # August 30 - September 5 2026
    return f"{start:%B} {start.day} {start.year} - {end:%B} {end.day} {end.year}"
```

Subject = `f"Trend Tracker {week_label(end)}"`. No emoji — vc-job-agent's
`emailer.py` opens its subject with an emoji; drop that here to match the spec exactly.

**Empty-week behavior is a real decision.** vc-job-agent's `send_daily_digest()`
returns early and sends nothing when there are no matches, which is
indistinguishable from a crashed workflow. Send a short "no new activity this week"
email including the per-source summary table instead, so silence always means
breakage.

---

## 8. Scheduling

```yaml
on:
  schedule:
    - cron: '0 13 * * 0'   # Sunday 13:00 UTC = 8am CDT
  workflow_dispatch:
```

Runs Sunday morning covering the Sunday–Saturday window that just closed. Note
that GitHub cron is UTC-only with no DST handling, so this drifts to 7am CT in
winter — harmless for a weekly digest, but don't be surprised by it.

---

## 9. State persistence — fix this, don't copy it

vc-job-agent persists `data/*.json` between runs with `actions/cache`:

```yaml
key: agent-data-${{ github.run_number }}
restore-keys: agent-data-
```

That's workable for a *daily* job. For a *weekly* one it's fragile: GitHub evicts
cache entries that haven't been accessed in a set window — my understanding is
7 days, which lands right on this schedule's boundary, but **verify the current
policy in GitHub's Actions cache documentation**, since it has changed before and I
can't confirm today's value. A single eviction wipes `seen_posts.json`, and the next
run treats every post as new — the exact failure mode requirement four rules out.

Pick a durable store instead. In rough order of preference:

1. **Commit state back to the repo.** Add a step that commits `data/*.json` after a
   successful run. Auditable, diffable, free, and you can see week-over-week changes
   in git history. Needs `permissions: contents: write`.
2. **A dedicated branch or a gist** if you'd rather keep data out of the main branch.
3. **`actions/upload-artifact`** with explicit `retention-days: 90` — but artifacts
   are clumsier to read back than a committed file.

Keep `actions/cache` for pip and Playwright browsers, where eviction is harmless.

---

## 10. Phases

| # | Phase | Output | Notes |
|---|---|---|---|
| 0 | Scaffold | Repo, `requirements.txt`, `.env.example`, models | Copy `history.py`, `base.py`, `emailer.py`, workflow from vc-job-agent as starting points |
| 1 | **Source recon** | `SOURCE_MATRIX.md` — tier + selector + feed URL + date availability per site | **Highest-risk phase. Do it first, before any parser.** |
| 2 | Core | `BaseSource`, `state.py`, `weekrange.py` | Port + adapt |
| 3 | Sources | 14 parsers, tier B → A → D → C | Each returns `list[BlogPost]`; failures isolated |
| 4 | Extraction | `fetch_post_detail()`, classifier gate, `extractor.py` | Valuation trap, undisclosed rounds |
| 5 | Trends | `sectors.py`, `trends.py`, `weekly_history.json` | Fixed enum; 4-week deltas |
| 6 | Email | `sage.py`, Sage card renderer, subject line | Table layout, inline styles |
| 7 | Dedup hardening | Layer 2 + 3, `--seed` run | Verify a syndicated round collapses to one card |
| 8 | Ship | Workflow, secrets, durable state | Sunday cron |
| 9 | Verify | See below | |

---

## 11. Verification

Correctness here is hard to eyeball — a wrong funding number looks exactly like a
right one in a nice-looking email. Concrete checks:

- **Dedup:** run twice back to back. The second run must find zero new posts.
- **Syndicate collapse:** find a real round two of the 14 firms both blogged. Assert one card, two firms in `vc_firms`.
- **Amount accuracy:** hand-verify ~15 extracted amounts against the source posts. Specifically seek out a post containing both a round size and a valuation.
- **Sector math:** assert per-sector dollar sums equal the headline total, and that `Undisclosed` rounds are excluded from sums but present in deal counts.
- **Classifier precision:** hand-label ~30 posts, measure false positives. An essay in the digest is worse than a missed announcement.
- **Email rendering:** send to Gmail web, Gmail iOS, and Apple Mail dark mode. Confirm fallback fonts and that cards don't collapse.
- **Source health:** a source returning 0 posts two weeks running should be flagged in the email footer, not silently ignored — that's how vc-job-agent ended up with 13 broken scrapers before anyone noticed.

---

## 12. Open decisions

- **Repo location:** standalone `trend-tracker/` vs. a second entry point inside `vc-job-agent`. Standalone keeps state files and cron separate; sharing means one place to fix `BaseSource` bugs.
- **Cap per email:** unbounded, or top N by amount with the rest in a compact list? A busy week across 14 firms could plausibly be 40+ rounds.
- **Sector taxonomy:** the 15 above are a proposal — worth one editing pass before Phase 5, since changing it later invalidates trend history.
