# Trend Tracker

A weekly email digest of new startup investments announced by 14 venture capital
firms. Scans each firm's blog, detects posts that weren't there last week, reads
each new post, and extracts what company raised money, what it does, how much it
raised, and where to find it.

Delivered Sunday mornings as `Trend Tracker August 2-8 2026`, styled with the
Sage design system.

---

## What it does

Three questions, answered every week:

**What new companies are worth knowing about?** Each investment gets a card with
the company name, a plain description of what it does, the amount and stage, and a
link to its site.

**Where is money going?** A sector rollup sits above the cards — total capital
deployed, deal count, and a per-sector breakdown compared against the trailing
four-week average, so a genuine shift is distinguishable from a noisy week.

**What are all 14 firms doing?** One email, every firm, no tab-hopping.

And one constraint that shapes the whole design: **nothing repeats.** A company
that appeared last week does not appear again.

---

## How it works

```
14 VC blogs ──▶ new-post diff ──▶ read each post ──▶ extract ──▶ dedup ──▶ trends ──▶ email
                (vs. last week)     (follow link)    (Claude)   (3 layers)
```

### Detecting what's new

The agent keeps a memory of every post URL it has ever seen in
`data/seen_posts.json`. Each run scrapes the 14 index pages, compares against that
memory, and only the URLs it has never seen continue down the pipeline.

Publication dates are deliberately *not* the primary signal. Many of these blogs
show no date on the index page, some show relative dates, and posts get backdated
or quietly edited. The URL diff is reliable where dates aren't. A parsed date, when
available, only serves as a sanity filter to reject posts older than ~60 days.

Three layers of deduplication run in sequence:

| Layer | Key | Catches |
|---|---|---|
| 1 | Canonical post URL | The normal case |
| 2 | Content hash of title + body | Re-slugged or re-published posts |
| 3 | Company name + round stage | The *same round* announced by several firms in the syndicate |

Layer 3 matters more than it sounds. When a company closes a Series B, every firm
in the round tends to blog about it. Without layer 3 you'd see the same company
three times in one email and its dollars counted three times in the sector totals.
Instead the entries merge into one card listing every firm involved.

### Reading each post

New posts are fetched and parsed for their full body, then passed through a
classifier: *is this actually announcing a specific company raising a specific
round?* Most posts on these blogs aren't — they're essays, market maps, podcast
episodes, partner announcements. Those are dropped before extraction.

What survives goes through a regex pass for the amount and stage, then a Claude
call for the company description, canonical URL, sector, and co-investors. The two
passes cross-check each other; disagreement lowers the confidence flag rather than
silently picking one.

The extraction prompt specifically guards against the valuation trap — a post
reading "raised $30M Series B at a $300M valuation" must report $30M, not $300M.

### Sector trends

Every investment is assigned exactly one sector from a **fixed** list. This is the
whole reason the trend view works: a free-form label would drift between weeks and
nothing would be comparable.

```
AI Infrastructure · AI Applications · Developer Tools · Fintech ·
Healthcare & Bio · Enterprise SaaS · Security · Consumer · Robotics & Hardware ·
Climate & Energy · Defense & Gov · Commerce & Retail · Data & Analytics ·
Crypto · Other
```

Twelve weeks of history live in `data/weekly_history.json`, which is what makes
the four-week comparison possible.

### The email

Built on the Sage `Card` component (`variant="card"`), translated to email-safe
HTML:

| Card slot | Content |
|---|---|
| Label | VC firm(s) |
| Heading | Company name |
| Accent slot | Amount · round stage |
| Body | What the company does |
| Tag | Sector |
| Action | Visit site → |

Sage's palette carries over intact. Its typefaces — Rethink Sans and Spectral —
cannot: Gmail blocks webfonts, so the email declares fallback stacks and most
readers will see Georgia and a system sans instead. This is a hard limitation of
HTML email, not a shortcut.

If a week has no new investments, you still get an email saying so. Silence should
always mean something broke.

---

## Sources

| Firm | Blog |
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

These are editorial blogs on each firm's main site — different properties from the
portfolio *job boards* that `vc-job-agent` scrapes. Each runs its own CMS, so each
has its own parser. `SOURCE_MATRIX.md` records what platform each site turned out
to be and how it's being read.

---

## Setup

```bash
git clone <your-repo> trend-tracker
cd trend-tracker
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium          # only needed for JS-rendered sources
cp .env.example .env                 # then fill it in
```

### Environment

```bash
ANTHROPIC_API_KEY=sk-ant-...

# Email — Gmail (simplest) or SendGrid
GMAIL_USER=your.email@example.com
GMAIL_APP_PASSWORD=your_16_char_app_password
EMAIL_TO=your.email@example.com

# SENDGRID_API_KEY=...
# EMAIL_FROM=verified@yourdomain.com
```

`GMAIL_APP_PASSWORD` is a Google App Password, not your account password — it
requires 2FA to be enabled on the account. Generate it in your Google Account
security settings.

### First run

Seed the memory before sending anything. Otherwise every post on all 14 blogs
looks new and email #1 is hundreds of entries.

```bash
python -m src.main --seed        # records current posts, sends nothing
python -m src.main --dry-run     # renders to stdout + writes preview.html
python -m src.main               # scrape, extract, send
```

### Commands

| Flag | Effect |
|---|---|
| `--seed` | Record what exists now; send no email. Run once at setup. |
| `--dry-run` | Full pipeline, no send. Writes `preview.html`. |
| `--source NAME` | Run one source only. Use while writing a parser. |
| `--no-extract` | Skip Claude calls. Fast structural test of the scrapers. |
| `--week YYYY-MM-DD` | Override the week-ending date. |
| `-v` | Debug logging. |

---

## Scheduling

GitHub Actions runs it Sundays at 13:00 UTC (8am CDT / 7am CST — GitHub cron has
no DST awareness).

Add these repository secrets: `ANTHROPIC_API_KEY`, `GMAIL_USER`,
`GMAIL_APP_PASSWORD`, `EMAIL_TO`.

**State persistence deserves attention.** `data/seen_posts.json` is what makes
"nothing repeats" work, and losing it means the next email resends everything. The
workflow commits it back to the repo after each successful run rather than relying
on `actions/cache`, because cache entries are evicted after a period of inactivity
that may be as short as the schedule interval itself. Worth confirming GitHub's
current eviction policy in their Actions cache docs if you change this.

---

## Project layout

```
src/
├── main.py          Orchestrator — parallel scrape, pipeline, summary table
├── models.py        BlogPost, Investment, SourceSummary, WeeklyDigest
├── state.py         The three dedup layers + weekly history
├── extractor.py     Classifier gate + regex + Claude structured extraction
├── sectors.py       The fixed sector list
├── trends.py        Sector rollup and four-week deltas
├── weekrange.py     Sunday–Saturday windows and the subject line
├── sage.py          Sage tokens as email-safe constants
├── emailer.py       Sage card renderer + Gmail/SendGrid delivery
└── sources/         base.py + 14 per-firm parsers

data/
├── seen_posts.json        Dedup layers 1 and 2
├── seen_investments.json  Dedup layer 3
└── weekly_history.json    12 weeks, for trend deltas
```

---

## Troubleshooting

**A source returns 0 posts.** Almost always a site redesign; these are marketing
pages and they change without notice. Run `--source NAME -v`, check whether the
HTML still contains post links, and re-tier the source in `SOURCE_MATRIX.md`. The
email footer flags any source that comes back empty so this surfaces on its own.

**The same company appears twice.** Layer 3 normalization missed a name variant —
"Acme Inc." vs "Acme". Extend the normalizer in `state.py`.

**A funding number looks wrong.** Check the source post for multiple dollar figures.
If it lists a valuation or a cumulative total alongside the round, that's the
valuation trap; the extraction prompt needs tightening. Low-confidence extractions
are marked in the email precisely so these are catchable.

**Email arrives unstyled or cards look broken.** Expected in some clients. Gmail
strips webfonts and Outlook ignores flexbox — the renderer uses table layout and
inline styles, but if you edit it, keep both constraints in mind.

**Nothing arrived at all.** Check the Actions run log. A successful run always
sends something, even an empty week.

---

## Relationship to vc-job-agent

Trend Tracker reuses vc-job-agent's architecture — the URL-memory approach in
`history.py`, the follow-the-link detail scraping in `BaseScraper.fetch_job_detail()`,
the parallel scrape-and-summarize loop in `main.py`, and the dual-provider emailer.

It does not reuse the scrapers. Those target portfolio job boards on separate
subdomains, most of them sharing one backend API. These are 14 independent
editorial CMSs, so the parsing is new work.
