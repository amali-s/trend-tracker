# Source Matrix

Per-site parsing intel for the 14 tracked blogs. This is a living document —
update it whenever `python -m src.probe` disagrees with what's written here.

**Probed 2026-08-05, re-probed 2026-08-06.** All fourteen are verified against
the live pages and covered by fixture tests. Latest full probe: 522 posts
across the fourteen, 400 of them carrying a real publication date.

Dates are still missing entirely on Greylock, a16z, General Catalyst, Index
Ventures and NEA, and on all but ~4 of Designer Fund's cards, because those
index pages simply do not print one. Those have to come from the detail page,
which is why the date is a secondary filter and never the "what's new" signal.

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

### Lightspeed — Tier B (WordPress REST)

| | |
|---|---|
| API | `https://lsvp.com/wp-json/wp/v2/posts` — 50 posts, all dated |
| Post URLs | `lsvp.com/stories/<slug>` |

Three paths, two of them traps. The HTML index matches 32 links but its post
anchors are **empty**, so titles fell back to the slug and lost punctuation —
`$3.5B` arrived as `3 5B`, which is actively harmful to the amount extractor.

**`/feed/` is a decoy and the dangerous kind:** 10 entries, all dated, parses
cleanly — but they are `/founder/<name>` and `/company/<slug>` CMS records, not
stories. A source pointed at it passes the probe and never surfaces a round.

### Designer Fund — Tier A (static HTML, Framer)

| | |
|---|---|
| Post URLs | `designerfund.com/blog/<slug>` |
| Dates | Plain text beside a "New Feature" badge — and **only on ~4 of 27 cards** |
| Feed | None. `/feed/`, `/rss/`, `/feed.xml` and WP REST all probed; none exists |

The healthiest of the inferred sources: pattern already right, titles already
clean because Framer puts a real `<h1>` in each card anchor. Note Framer emits
that `<h1>` twice per card (ssr-variant); harmless, since the first wins.

### Index Ventures — Tier A (static HTML)

| | |
|---|---|
| Post URLs | `indexventures.com/perspectives/<slug>` |
| Dates | None anywhere on the index, and no feed |

Not a client-rendered Vue app — that was the *jobs* side of the domain.

**Title comes from the slug, and must.** Every post anchor is a bare "Read more
Opens in a new window." with no heading, and the card describes the **founder**,
not the post: the card for "Simulating Society at Scale: Our Investment in
Similes' $200M Series B" reads "Joon Sung Park. Multidisciplinary artist. Agent
architect. Simulator of worlds." The Antler-style card walk is *wrong* here.

Slug titles lose punctuation (`$200M` → `200M`) — fine for the heuristics, not
fit for display. `fetch_post_detail` must overwrite them with the page `<h1>`.

### Accel — Tier A (static HTML)

| | |
|---|---|
| Post URLs | `accel.com/news/<slug>`; `/noteworthies/` matched nothing |
| Dates | Yes, in the card — 50/50 |

The category filter tabs are themselves `/news/<word>` URLs, so `/news/insights`
and `/news/podcasts` matched and arrived as posts titled "Insights" and
"Podcasts". Card anchors carry `<category> <title> <date>` as flat text with no
heading, so both ends are stripped. No `__NEXT_DATA__` on www.accel.com.

### NEA — Tier A (static HTML)

| | |
|---|---|
| Post URLs | `nea.com/blog/<slug>` |
| Dates | None on the index |

Cards stack several headings and **the first is often a label** — a content type
("Blog") or a newsletter series marker ("The Current #16") — with the real title
next. Taking the first heading produced posts titled "Blog" and "The Current #16".

`?topic=investment` does **not** restrict results to funding announcements; most
of what returns is essays and a recurring consumer-data newsletter. Do not read
this source's volume as deal flow.

Feed rejected on purpose: `statamic.nea.com` is their headless CMS backend, not
the published site, and it mixes `/team/` and `/portfolio/` records into 1430
entries. Host rewriting would work today and supply dates, but it means
depending on a backend origin and pulling the whole archive weekly.

### Bessemer — Tier A (static HTML)

| | |
|---|---|
| Post URLs | `bvp.com/(news\|atlas)/<slug>` |
| Dates | Yes — but stamped `7.29.26`, not `July 29, 2026` |
| Volume | ~207, the entire archive on one page |

The site nav links "Funds > Flagship" straight at a real post URL, *before* its
card. Since `parse_index` takes the first anchor per URL, that post arrived
titled "Flagship". `extract_title` returns `""` for a headingless nav-shaped
anchor, which skips it **without** marking the URL seen, so the real card
reclaims it. Expect a large first `--seed` from this source.

---

## Unverified

Empty. All fourteen are verified and fixture-tested as of 2026-08-06.

`UNVERIFIED_SOURCES` is kept in `firms.py` only so a newly added source has
somewhere to sit until it is probed.

This does not mean the parsers are permanently correct. They are marketing sites
that redesign without notice, and a fixture test proves the parser handles the
page *as it was on 2026-08-06*. `python -m src.probe` is the check that matters —
re-run it whenever a source starts returning zero.

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
