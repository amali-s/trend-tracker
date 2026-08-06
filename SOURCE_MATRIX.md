# Source Matrix

Per-site parsing intel for the 14 tracked blogs. This is a living document —
update it whenever `python -m src.probe` disagrees with what's written here.

**Probed 2026-08-05.** Four sites were checked against the live pages. The other
ten carry inferred patterns and are marked UNVERIFIED. Run the probe before
relying on them.

---

## Verified

### Greylock — Tier A (static HTML)

| | |
|---|---|
| Index | `https://greylock.com/blog/portfolio-news/` |
| Platform | Next.js front end over Sanity CMS (`cdn.sanity.io` assets) |
| Post URLs | `greylock.com/blog/<slug>/` |
| Pattern | `greylock\.com/blog/[a-z0-9][a-z0-9-]+$` |
| Pagination | `/blog/portfolio-news/page/N/` — index reported **8 pages** |
| Dates on index | No — must come from the detail page |
| Feed | Not found via my fetcher; re-probe locally |

The category filter nav (`/blog/portfolio-news/`, `/blog/greymatter/`,
`/blog/firm-news/`) shares the `/blog/` prefix with real posts, so those four
paths are excluded explicitly. So is `/blog/portfolio-news/opengraph-image-*`,
which is a generated social image, not a post.

Titles follow an "Introducing X: <positioning>" convention, which makes the
company name easy to extract and flags the post as an announcement without an
LLM call. Note that not every such post is a new round — "Congrats,
Chronosphere and Palo Alto Networks!" is an acquisition. The classifier still
has to run.

### a16z — Tier A (static HTML, WordPress)

| | |
|---|---|
| Index | `https://a16z.com/news-content/` |
| Platform | WordPress (`wp-content`, CloudFront asset CDN) |
| Post URLs | Investments: `a16z.com/announcement/<slug>/` |
| Pattern | `a16z\.com/announcement/[a-z0-9][a-z0-9-]+$` |
| Pagination | JS "load more" button, no URL form — first batch only (~45 items) |
| Dates on index | No |
| Feed | `/feed/` and `/wp-json/wp/v2/posts` both returned empty through my fetcher — **worth re-probing locally**, this may have been the fetcher rather than the site |

**The useful finding here.** a16z segregates content by URL path: investment
announcements live at `/announcement/`, podcasts at `/podcast/`, essays at the
root. Observed announcements: `investing-in-volta`, `investing-in-neo`,
`investing-in-runta`, `investing-in-netris`, `investing-in-mirendil`.

That path is a near-perfect classifier gate, so discovery is scoped to it and
the LLM classifier is skipped for these posts. The trade-off is deliberate:
this ignores any investment news a16z publishes outside `/announcement/`. Given
how much of this blog is podcasts and essays, precision is worth more than
recall. Widen `post_url_pattern` if you'd rather pay for the classifier.

The index also exposes a focus-area label per card (`Infra`, `Consumer`,
`Enterprise`, `Fintech`, `American Dynamism`, `Bio + Health`, `Crypto`,
`Growth`, `Perennial`, `Speedrun`, `General`). Those are passed through as
sector hints. There's also a "Content Type: Investment News" filter in the UI —
if it maps to a URL parameter, that would be an even cleaner scope than the path.

### Contrary — Tier A (static HTML)

| | |
|---|---|
| Index | `https://contrary.com/blog` |
| Post URLs | `contrary.com/blog/<slug>` (no trailing slash) |
| Pattern | `contrary\.com/blog/[a-z0-9][a-z0-9-]+$` |
| Pagination | JS "Load more", no URL form — first ~9 posts only |
| Dates on index | **Yes** — plain text, `"October 8, 2025"` format |
| Feed | Not found; re-probe |

Two parsing quirks handled in `ContrarySource`:

1. The date is plain text, not a `<time>` element, so the generic date lookup
   finds nothing. A month-name regex handles it.
2. The entire card is a single anchor, so harvested text reads
   `"October 8, 2025 Investing in Base Power Announcing our investment in Base
   Power Read more"`. The parser strips the leading date, a `Featured` prefix,
   and the trailing `Read more`.

**Low volume.** At probe time the newest post was `class-of-2026` dated
2025-11-06, and the newest *investment* post was 2025-10-08. Expect many empty
weeks from this source and don't read zero as breakage.

### General Catalyst — Tier A (static HTML, Webflow)

| | |
|---|---|
| Index | `https://www.generalcatalyst.com/stories` |
| Platform | Webflow (`meta-generator: Webflow`) |
| Post URLs | `generalcatalyst.com/stories/<slug>` |
| Pattern | `generalcatalyst\.com/stories/[a-z0-9][a-z0-9-]+$` |
| Pagination | `?c3e7011e_page=2` — **build-hashed, do not hardcode** |
| Dates on index | No |
| Feed | Not found; re-probe |

Three things the probe surfaced:

- The page carries a long **"NEWS" section of external press links**
  (techcrunch, forbes, wsj, bloomberg, ft). The same-host check in
  `BaseSource.is_post_url` filters these out.
- The pagination parameter contains a Webflow build hash that changes on
  redeploy, so it isn't hardcoded. Page 1 only, which is ~25 stories.
- **Post anchors are empty** — the card renders image, category, title, and
  tags as sibling divs, with the href on a trailing empty `<a>`. There's no
  anchor text to harvest, so the title falls back to the slug
  (`our-investment-in-arca` → "Our Investment In Arca"). Acceptable, but the
  detail-page `<h1>` is better and `fetch_post_detail` will pick it up.

Category is `Insights` / `Community` / `News` / `Case Studies` / `Resources`;
investment posts are `Community` with titles "Our Investment in X", "Seeding
the Future with X", "Doubling Down on X". Topic tags map unusually well onto our
sector enum: `Applied AI`, `Artificial Intelligence`, `Healthcare`, `Fintech`,
`Enterprise`, `Consumer`, `Space`, `Defense & Government`,
`Energy & Infrastructure`, `Industrials & Manufacturing`, `Global Resilience`.

---

## Unverified

Patterns inferred from the index URL. **Run `python -m src.probe` before
trusting any of these.** The probe prints the path prefixes actually present on
each page, so a wrong pattern is obvious from its output.

| Firm | Index | Inferred pattern | What to watch for |
|---|---|---|---|
| Sequoia | `sequoiacap.com/stories/?_story-category=news` | `/(article\|stories)/<slug>` | Is `_story-category` applied server-side? If it's a client-side filter you'll get all stories, not just news. |
| Index Ventures | `indexventures.com/perspectives/` | `/perspectives/<slug>` | vc-job-agent found this domain to be a client-rendered Vue app on the jobs side. May need Playwright. Mostly essays, so expect heavy classifier rejection. |
| Kleiner Perkins | `kleinerperkins.com/perspectives/category/announcements` | `/perspectives/<slug>` | Index is already scoped to announcements — if confirmed, set `investment_url_pattern` and skip the classifier like a16z. |
| Accel | `accel.com/news/portfolio` | `/noteworthies/<slug>` or `/news/<slug>` | vc-job-agent found `jobs.accel.com` on old Getro/Next.js serving `__NEXT_DATA__`. Check for that blob — parsing it beats the DOM. |
| Battery Ventures | `battery.com/news/` | `/(news\|blog)/<slug>` | `/news/` pages often mix first-party posts with outbound press links. |
| NEA | `nea.com/blog?type=Read&topic=investment` | `/blog/<slug>` | Keep the existing filters; walk `&page=N`. Confirm the page param actually changes the HTML — if not, it's an SPA. |
| Antler | `antler.co/newsroom` | `/(blog\|newsroom)/<slug>` | Very high volume of small pre-seed deals across many geographies. Will dominate deal *count* while contributing little to deal *dollars*. |
| Lightspeed | `lsvp.com/stories/` | `/(stories\|blog)/<slug>` | — |
| Bessemer | `bvp.com/news` | `/(news\|atlas)/<slug>` | Publishes "atlases" and memos alongside announcements; classifier matters. |
| Designer Fund | `designerfund.com/blog` | `/(blog\|stories)/<slug>` | Only firm not in vc-job-agent. Low volume, writes about design practice more than rounds. Most likely of the 14 to have a real feed — probe for one first. |

---

## How to fix a source

1. `python -m src.probe <name> --save-fixtures`
2. Read the "path prefixes present on the page" output — that's the real URL shape.
3. Correct `post_url_pattern` / `exclude_url_patterns` in `src/sources/firms.py`.
4. Re-run the probe until `Pattern matches` is non-zero and the sample URLs look like posts.
5. Add a fixture-based test in `tests/test_parsing.py` so the fix stays fixed.
6. Move the entry from `UNVERIFIED_SOURCES` to `VERIFIED_SOURCES` and document it above.

If the probe reports **Tier C**, there were almost no same-host links in the
static HTML. Before reaching for Playwright, open DevTools → Network on the page
and look for an XHR returning JSON — calling that endpoint directly is faster
and far more stable than driving a browser. That's how vc-job-agent's nine
Consider-platform scrapers were eventually fixed.
