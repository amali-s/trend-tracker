# Verification — PLAN §11

Status of each §11 correctness check. Split into what was verified offline
(no API key, no network) and what needs the live API / real email accounts.

Test suite at time of writing: **372 passed** (`python -m pytest`).

---

## Verified offline

| # | §11 check | Result | Evidence |
|---|---|---|---|
| 1 | **Dedup** — run twice, second finds zero new | ✅ PASS | Run 1 sent 2 posts to extraction; run 2 sent 0 and produced an empty digest. `tests/test_main.py::TestDedup::test_second_run_finds_zero_new_posts` |
| 2 | **Syndicate collapse** — one card, several firms | ✅ PASS | Two posts announcing Ramp's Series D collapse to **one** investment with `vc_firms = {Sequoia, Founders Fund}`. `tests/test_main.py::TestSyndicateCollapse` |
| 3 | **Amount accuracy** — hand-verify ~15 incl. a valuation trap | ✅ 15/15 (offline proxy) | 15 realistic announcement phrasings — valuation traps, cumulative totals, revenue figures, undisclosed, spelled-out, title-embedded — all resolved to the correct round size; the two guard overrides came back `confidence=low`. See below. **Live half pending** (§ Pending). |
| 4 | **Sector math** — per-sector $ sums == headline; undisclosed excluded from sums, counted in deals | ✅ PASS | Headline $245M == Σ sector totals; 4 deals == Σ sector deals; the 1 undisclosed round is in the deal count and contributes $0. `tests/test_trends.py::TestSectorRows::test_sector_totals_equal_the_headline` |
| 7 | **Source health** — a 0-post or errored source is flagged, not silently dropped | ✅ PASS | `unhealthy_sources` returns both the 0-post and the errored source; the email footer renders them in red. `tests/test_emailer.py::TestSourceFooter` |
| 6a | **Email rendering** — `--dry-run` renders | ✅ PASS | Rendered a rich digest and inspected it in a browser (Phase 6): all three delta states, the low-confidence amber strip, undisclosed card, and the red-flagged source footer render correctly in table layout. `tests/test_emailer.py::TestEmailSafety` asserts no flexbox, no CSS classes, no webfont imports, 600px container, explicit colour on every link. |

### The 15 hand-verified amounts (check 3)

Each row is a realistic sentence from these blogs, the figure a model might
return, and the guard's final answer. **The guard never takes the largest
figure.**

| Sentence shape | Model returned | Guard → | Conf |
|---|---|---|---|
| `$540M Series E at a $6B valuation` | $540M | **$540M** | high |
| `$200M Series B at a $1.2B post-money valuation` | $1.2B (trap) | **$200M** | low |
| `$12.5M Series A, valuing the company at $120M` | $12.5M | **$12.5M** | high |
| `$8M seed brings total funding to $19M to date` | $8M | **$8M** | high |
| `$40M Series B` (clean) | $40M | **$40M** | high |
| `$30M round, more than its $40M valuation cap` | $30M | **$30M** | high |
| `reached $10M ARR before its $45M Series B` | $45M | **$45M** | high |
| `$750,000 pre-seed` | $750K | **$750K** | high |
| `$1.5 billion round` | $1.5B | **$1.5B** | high |
| `terms were not disclosed` | None | **Undisclosed** | high |
| `now valued at $300M following the raise` | $300M (only fig is a valuation) | **Undisclosed** | low |
| `raised twelve million dollars in seed funding` | $12M (spelled out, no regex figure) | **$12M** | medium |
| `$25M Series A. Its post-money valuation is $180M` | $25M | **$25M** | high |
| `Antler raises additional $510M for its fund` | $510M | **$510M** | high |
| `Leland raised a $12M Series A` (title-style) | $12M | **$12M** | high |

Confidence is calibrated: the two overrides land at `low` (they'd show the
amber "check the source" strip in the email); the spelled-out figure with no
regex corroboration is `medium`; corroborated figures are `high`.

---

## Pending — need the live API / real accounts

These cannot run without `ANTHROPIC_API_KEY` (and, for email, a real inbox).
They are the checks that exercise the model and the network, not the code.

### 3b. Amount accuracy against real fetched posts

The offline proxy above verifies the *guard logic* against realistic inputs.
The remaining half is verifying the *model's* extraction against the actual
source posts, including a real post that carries both a round size and a
valuation.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python -m src.main --dry-run -v      # full pipeline, no send; writes preview.html
```

Then open `preview.html` and spot-check ~15 amounts against the linked source
posts. Any card with the amber strip (`confidence: low`) is one the guard
already flagged — check those first.

### 5. Classifier precision (false-positive rate)

Purely a model check — hand-label ~30 real posts and measure how many
non-announcements slip through. An essay in the digest is worse than a missed
announcement, so precision matters more than recall here. Watch specifically
for the firms' **own** fundraises (Antler, Bessemer), acquisitions, and the
essay-heavy blogs (Contrary, Index Ventures, Designer Fund).

The pipeline logs every drop at INFO (`Dropped [firm] title — reason`), so:

```bash
python -m src.main --dry-run -v 2>classifier.log
grep "Dropped" classifier.log      # what the gate rejected, and why
```

Cross-check the rejections against the source pages and count any real
announcement that was dropped (false negative) or any non-announcement that
reached `preview.html` (false positive — the one to minimise).

### 6b. Email rendering in real clients

`--dry-run` proves the HTML renders in a browser. It does **not** prove Gmail,
Gmail iOS, and Apple Mail dark mode render it — those strip webfonts, ignore
flexbox (we use neither), and Apple Mail inverts colours in dark mode. Send a
real digest to yourself and check all three:

```bash
python -m src.main            # a real run; sends to EMAIL_TO
```

Confirm: cards don't collapse, the cream palette survives dark-mode inversion
(every text node has an explicit colour, so it should), and the fallback fonts
(Georgia + system sans) look acceptable where Rethink Sans / Spectral don't
load — which is most clients.

---

## First-run reminder (from PLAN §4)

The committed `data/seen_posts.json` is `{}`. If the **first scheduled run**
fires against that empty baseline, email #1 is every post on all 14 blogs. So
the first action after wiring the four repo secrets must be a seed — either a
manual `workflow_dispatch` with **seed = true**, or locally:

```bash
python -m src.main --seed     # records current posts, sends nothing
```

Then the first real run has a clean baseline.
