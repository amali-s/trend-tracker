# Source Matrix

Per-site parsing intel for the 14 tracked blogs. This is a living document —
update it whenever `python -m src.probe` disagrees with what's written here.

**Probed 2026-08-05, re-probed 2026-08-06.** Eight sites are verified against
the live pages and covered by fixture tests. The other six return posts but
have not been fixture-tested yet, and all six have known defects recorded
below. Run the probe before relying on them.

Three findings from the 2026-08-06 pass that generalise:

1. **A feed existing on a host proves nothing.** `probe.py` used to report a
   source OK whenever `/feed/` responded, before checking whether the pattern
   matched anything — Sequoia and Battery were both reported OK while
   discovering zero posts. The pass condition is now `source.scrape()`.
2. **Not every feed is the blog.** Lightspeed's `/feed/` returns `/founder/`
   and `/company/` CMS records, not stories. NEA's is served from
   `statamic.nea.com`, a different host, and mixes `/team/` and `/portfolio/`
   in with `/blog/`. Both are useless despite parsing cleanly.
3. **Matching a pattern is not the same as parsing a post.** Antler matched 12
   URLs and titled every one of them "Read more". Check titles and dates, not
   just the match count.

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

### Sequoia — Tier B (RSS)

| | |
|---|---|
| Index | `https://www.sequoiacap.com/stories/?_story-category=news` (unusable) |
| Feed | `https://www.sequoiacap.com/feed/` — 10 items, real dates |
| Post URLs | `sequoiacap.com/article/<slug>/` — **not** `/stories/<slug>` |
| Dates | Yes, from the feed |

The HTML index is client-rendered: five same-host links, all nav. The
`_story-category` question is therefore moot. Feed `<link>` elements use the
bare host (`sequoiacap.com`) while the site is served from `www.` — this is why
`same_site()` ignores a leading `www.` label.

Sequoia tags funding posts **`Funding announcement`**, a firm-authored gate as
good as a16z's `/announcement/` path. Titles follow "Partnering with X: ...",
which means every naturally-occurring tagged post *also* matches the title
patterns — so the tag-gate test uses a constructed fixture item whose title
matches nothing. Without it, deleting the tag gate leaves the tests green.

### Antler — Tier A (static HTML)

| | |
|---|---|
| Index | `https://www.antler.co/newsroom` |
| Post URLs | `antler.co/press-releases/<slug>` — **not** `/newsroom/` or `/blog/` |
| Pagination | `?7eb107ba_page=2` and `?f4c053cc_page=2` — build-hashed, two listings, not hardcoded |
| Dates | Yes — plain text in the card, no `<time>` element |

The anchor is a bare "Read more"; the title is a sibling several levels up.
`extract_title` walks to the card container and takes its longest text node
rather than indexing positionally, since the wrappers are Webflow-generated.

The index carries 18 `/location/<country>` links with exactly the two-segment
shape a looser pattern eats. **Trap for the classifier:** "Antler Raises
additional $510 Million" matches the investment title patterns but is the firm
raising, not a portfolio company.

### Battery Ventures — Tier B (WordPress REST)

| | |
|---|---|
| Index | `https://www.battery.com/blog` |
| API | `https://www.battery.com/wp-json/wp/v2/posts` — 50 posts, all dated |
| Post URLs | `battery.com/blog/<slug>/` |
| Feed | `/feed/` exists but caps at 4 items — too shallow |

**`/news/` was the wrong page and no pattern over it could have worked.** It is
not a mix of first-party posts and press links — every article link on it points
at businesswire.com, cfo.com or forterro.com. The same-host check drops all of
them, which is exactly why the source returned zero.

Expect few investments: this blog is research and commentary, and Battery
appears to route portfolio funding news to the outbound coverage on `/news/`.
Do not read a zero here as breakage without re-probing.

### Kleiner Perkins — Tier B (RSS)

| | |
|---|---|
| Index | `https://www.kleinerperkins.com/perspectives/category/announcements` (works, but dateless) |
| Feed | `https://www.kleinerperkins.com/feed/` — 10 items, all dated |
| Post URLs | `kleinerperkins.com/perspectives/<slug>` |

The inferred pattern was right and the index returns 6 posts, but with no dates.
The feed is used instead and the `/category/announcements` scoping is knowingly
given up — more essays reach the classifier, but every post gets a real date.

**Republishes under two slugs.** "K2 Space: Building Bigger" appears at both
`/k2-space-building-bigger/` and `/k2-space-building-bigger-2/` with different
tags. Layer 1 passes both; this is a live test case for dedup layer 2.

---

## Unverified

These six return posts, so the probe reports them OK, but none is fixture-tested
and every one has a known defect found on 2026-08-06. **Do not trust their
output yet.**

| Firm | scrape() | Known defect |
|---|---|---|
| Index Ventures | 31 posts, 0 dated | Every title is `"Read more Opens in a new window."` — same bare-CTA anchor problem Antler had. Not a Vue SPA after all; static HTML works. |
| Accel | 52 posts, 0 dated | Nav leaking in as posts (`Insights`, `Podcasts`), and card chrome in titles: `"Portfolio News Cyera + Oasis: ... July 2..."`. Pattern is too broad and the title needs the card treatment. Dates are present in the card text. |
| NEA | 14 posts, 0 dated | Nav leaking (`Blog`); much of the output is `The Current #N`, a newsletter series rather than announcements. Feed is on `statamic.nea.com` (different host) and mixes `/team/` + `/portfolio/`, so it is unusable without host rewriting. `?topic=investment` filtering appears not to be applied. |
| Lightspeed | 32 posts, 0 dated | Titles are slug-derived and lose punctuation: `"Lightspeed Announces Lead Investment In Anthropics 3 5B Series E Finan"`. **The feed is a decoy** — `/feed/` returns `/founder/` and `/company/` records, not stories. Fix the HTML title extraction. |
| Bessemer | 207 posts, 0 dated | 207 is the whole archive plus nav (`Flagship`). Needs scoping and a date source. |
| Designer Fund | 27 posts, 0 dated | Titles are clean — the healthiest of the six. Needs a date source and a fixture test. No feed found. |

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
