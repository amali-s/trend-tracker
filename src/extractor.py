"""Turning a blog post into an Investment — gate first, then parse.

Three stages, in cost order:

  1. **Classifier gate.** Most posts on these blogs are not funding
     announcements. They are essays, market maps, podcast episodes, partner
     hires, product launches, acquisitions, and the firms' own fundraises.
     Without a gate the digest fills with noise and the trend math is
     meaningless. The gate runs on a short excerpt at low effort, and is
     skipped entirely when the source already knows (a16z publishes
     investments under /announcement/, Sequoia tags them "Funding
     announcement").

  2. **Regex pass.** Deterministic, free, and — importantly — it finds *every*
     dollar figure in the post with its position, not just one. That is what
     makes stage 3's cross-check possible.

  3. **Claude pass.** Structured JSON for what regex cannot do: the company
     description, its canonical URL, the sector, co-investors, and
     disambiguating which of several dollar figures is the round.

The two passes cross-check each other rather than one silently winning. See
`reconcile_amount` for the valuation-trap guard, which is the single most
important piece of correctness in this file.

Note on the schema
------------------
PLAN §5 describes vc-job-agent's convention: a strict "return only JSON"
instruction in the prompt plus `json.loads` in a try/except. This uses the
Messages API's structured outputs instead (`output_config.format` with a JSON
Schema), so the shape is *enforced by the API* rather than requested in prose
and malformed JSON stops being a failure mode. The try/except is kept as a
fallback and logs loudly if it ever fires.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Optional

import anthropic

from .models import BlogPost, Investment
from .sectors import DEFAULT_SECTOR, SECTORS, normalize_sector

logger = logging.getLogger(__name__)

MODEL = "claude-opus-5"

# Thinking is on by default on this model and counts against max_tokens, so
# both budgets carry headroom well beyond the size of the JSON itself. A tight
# budget truncates mid-answer rather than erroring.
CLASSIFY_MAX_TOKENS = 4000
EXTRACT_MAX_TOKENS = 8000

# The gate is a short binary judgement; extraction is the one that has to
# reason about several dollar figures at once.
CLASSIFY_EFFORT = "low"
EXTRACT_EFFORT = "medium"

# How much of the post each stage sees. The gate only needs the opening — an
# announcement says what it is in the first paragraph.
CLASSIFY_BODY_CHARS = 2000
EXTRACT_BODY_CHARS = 8000

# Bump when a prompt or schema changes, so cached rows written under the old
# wording are not reused. v2: expanded the classifier prompt (more negative
# examples; also crosses Opus 5's 512-token cache floor).
PROMPT_VERSION = 2

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
EXTRACTION_CACHE_FILE = os.path.join(DATA_DIR, "extraction_cache.json")

ROUND_STAGES: tuple[str, ...] = (
    "Pre-Seed",
    "Seed",
    "Series A", "Series B", "Series C", "Series D", "Series E",
    "Series F", "Series G", "Series H", "Series I", "Series J",
    "Growth",
    "Bridge",
    "Extension",
    "Unknown",
)


# ---------------------------------------------------------------------------
# Stage 2 — the regex pass
# ---------------------------------------------------------------------------

# The multiplier is optional so "$750,000" is caught alongside "$30M".
# Alternation order matters: longer spellings must precede their abbreviations
# or "billion" would match as "b" followed by a failed word boundary.
AMOUNT_RE = re.compile(
    r"\$\s?(\d[\d,]*(?:\.\d+)?)\s*(billion|million|thousand|bn|mm|b|m|k)?\b",
    re.IGNORECASE,
)

_MULTIPLIERS = {
    "billion": 1_000_000_000, "bn": 1_000_000_000, "b": 1_000_000_000,
    "million": 1_000_000, "mm": 1_000_000, "m": 1_000_000,
    "thousand": 1_000, "k": 1_000,
}

STAGE_RE = re.compile(
    r"\b(pre-?seed|seed|series\s+([A-J])\b|growth|bridge|extension)\b",
    re.IGNORECASE,
)

# Phrases that mean a nearby dollar figure is what the company is *worth*,
# not what it raised. The compound forms come first so "post-money valuation"
# is one marker rather than two — as two, "post-money" would claim the figure
# to its left (the round) and "valuation" the one to its right.
VALUATION_MARKER = re.compile(
    r"\b((?:pre-?money|post-?money)\s+valuations?|valuations?|valuing|valued|"
    r"post-?money|pre-?money)\b",
    re.IGNORECASE,
)

# Phrases that mean a nearby figure is money raised *across all rounds*.
# Deliberately excludes bare "has raised" — "Acme has raised $30M in a Series
# B" is the round itself, and tagging it would throw away the right answer.
CUMULATIVE_MARKER = re.compile(
    r"\b(total\s+(funding|raised|capital|investment)|totall?ing|to\s+date|"
    r"bringing\s+(its|their|the|our)?\s*total|brings\s+(its|their|the|our)?\s*total|"
    r"cumulative|in\s+total|raised\s+a\s+total|lifetime\s+funding)\b",
    re.IGNORECASE,
)

# Figures that are the company's revenue rather than its raise. "reached $10M
# ARR before raising its $45M Series B" is a real sentence shape.
REVENUE_MARKER = re.compile(
    r"\b(ARR|MRR|annual\s+recurring\s+revenue|revenue|run[-\s]?rate|bookings)\b",
    re.IGNORECASE,
)

# A marker only claims a figure ahead of it within this distance. Beyond it the
# two are probably unrelated clauses.
MARKER_RANGE = 90

# Only whitespace, punctuation, and a short preposition. Used for the *backward*
# claim, where English writes the compact appositive: "$300M valuation",
# "$52M in total", "$6B post-money".
#
# Deliberately excludes articles. Adding "a"/"the" here would make " at a "
# connective, and "raised $30M at a $300M valuation" would then hand the round
# to the valuation marker — the exact bug this guard exists to prevent.
CONNECTIVE_ONLY = re.compile(
    r"^[\s,:;-]*(?:of|at|to|around|near|nearly|approximately|about|roughly|"
    r"reaching|topping|over|above)?[\s,:;-]*$",
    re.IGNORECASE,
)

# A marker cannot reach forward across the end of its sentence. Without this,
# "The valuation was not disclosed. Acme raised $30M." would tag the round.
SENTENCE_BREAK = re.compile(r"[.!?\n]")


@dataclass
class AmountMatch:
    """One dollar figure found in the text, and what it appears to mean."""

    usd: int
    raw: str
    start: int
    end: int
    kind: str = "round"  # round | valuation | cumulative


def parse_amount(number: str, multiplier: Optional[str]) -> Optional[int]:
    """Normalize a matched figure to whole US dollars."""
    try:
        value = float(number.replace(",", ""))
    except ValueError:
        return None
    scale = _MULTIPLIERS.get((multiplier or "").lower(), 1)
    return int(round(value * scale))


def find_amounts(text: str) -> list[AmountMatch]:
    """Every dollar figure in the text, tagged round / valuation / cumulative.

    Tagging works by proximity rather than by a window around each figure.
    Each marker phrase claims the single *nearest* figure, which is what makes
    the canonical trap come out right:

        "raised $30M Series B at a $300M valuation, bringing total funding to $52M"

    "valuation" is nearest to $300M and "total funding" is nearest to $52M, so
    $30M is the only figure left untagged. A fixed ±N-character window around
    $30M would have swept up the word "valuation" and mislabelled the round.
    """
    if not text:
        return []

    matches: list[AmountMatch] = []
    for m in AMOUNT_RE.finditer(text):
        usd = parse_amount(m.group(1), m.group(2))
        if usd is None:
            continue
        matches.append(AmountMatch(
            usd=usd,
            raw=m.group(0).strip(),
            start=m.start(),
            end=m.end(),
        ))

    if not matches:
        return []

    for marker, kind in (
        (VALUATION_MARKER, "valuation"),
        (CUMULATIVE_MARKER, "cumulative"),
        (REVENUE_MARKER, "revenue"),
    ):
        for hit in marker.finditer(text):
            claimed = _claim(text, matches, hit.start(), hit.end())
            if claimed is not None:
                claimed.kind = kind

    return matches


def _claim(
    text: str, matches: list[AmountMatch], marker_start: int, marker_end: int
) -> Optional[AmountMatch]:
    """Which unclaimed figure does the marker at [start, end) refer to?

    Direction is decided by grammar, not by raw proximity — proximity is what
    got this wrong. A marker refers backward only in the tight appositive
    ("$300M valuation"); otherwise it refers forward, however many words of
    its own phrase sit in between ("valuing the company at $120 million",
    "brings total capital raised to $11M"). Measuring distance instead let
    those trailing words push the marker onto the round to its left.

    A marker that matches neither shape claims nothing. There is no
    nearest-figure fallback: guessing wrong here corrupts the headline number.
    """
    unclaimed = [m for m in matches if m.kind == "round"]
    if not unclaimed:
        return None

    # Backward — the compact appositive, nothing but a connective between.
    preceding = [m for m in unclaimed if m.end <= marker_start]
    if preceding:
        previous = max(preceding, key=lambda m: m.end)
        if CONNECTIVE_ONLY.match(text[previous.end:marker_start]):
            return previous

    # Forward — the next figure in the same sentence.
    following = [m for m in unclaimed if m.start >= marker_end]
    if following:
        nxt = min(following, key=lambda m: m.start)
        between = text[marker_end:nxt.start]
        if len(between) <= MARKER_RANGE and not SENTENCE_BREAK.search(between):
            return nxt

    return None


def round_amounts(text: str) -> list[AmountMatch]:
    """Only the figures that look like the size of this round."""
    return [m for m in find_amounts(text) if m.kind == "round"]


def find_stage(text: str) -> str:
    """First round stage named in the text, normalized to the enum."""
    match = STAGE_RE.search(text or "")
    if not match:
        return "Unknown"
    token = match.group(1).lower().replace("-", "").replace(" ", "")
    if token.startswith("preseed"):
        return "Pre-Seed"
    if token == "seed":
        return "Seed"
    if match.group(2):
        return f"Series {match.group(2).upper()}"
    return match.group(1).title()


def reconcile_amount(
    text: str, claude_usd: Optional[int], claude_raw: str
) -> tuple[Optional[int], str, str, str]:
    """Cross-check Claude's amount against the figures actually in the post.

    This is the valuation-trap guard from PLAN §5. Returns
    `(usd, raw, confidence, notes)`.

    The rule that matters: **nothing here ever takes the largest figure.** A
    naive `max()` is exactly what reports a $30M round as $300M. Where a
    choice among round-tagged figures is unavoidable it takes the *earliest*,
    because announcements lead with the round size and mention history later.
    """
    matches = find_amounts(text)
    rounds = [m for m in matches if m.kind == "round"]
    flagged = [m for m in matches if m.kind != "round"]

    # --- Claude reported no figure -----------------------------------------
    if claude_usd is None:
        raw = claude_raw or "Undisclosed"
        if rounds:
            # The post does contain a round-shaped figure the model didn't
            # report. Keep the model's call — it read the whole post — but say
            # so, because this is the shape of a miss.
            return None, raw, "medium", (
                f"Reported undisclosed, but the post contains "
                f"{rounds[0].raw}; verify against the source."
            )
        return None, raw, "high", ""

    # --- Claude's figure corroborated as a round size -----------------------
    if any(m.usd == claude_usd for m in rounds):
        return claude_usd, claude_raw or f"${claude_usd:,}", "high", ""

    # --- Claude picked up a valuation or a cumulative total -----------------
    trap = next((m for m in flagged if m.usd == claude_usd), None)
    if trap is not None:
        if rounds:
            best = min(rounds, key=lambda m: m.start)
            return best.usd, best.raw, "low", (
                f"Model reported {claude_raw or trap.raw}, which reads as a "
                f"{trap.kind} figure in the post; using {best.raw} instead."
            )
        return None, "Undisclosed", "low", (
            f"The only figure in the post ({trap.raw}) is a {trap.kind}, "
            f"not a round size."
        )

    # --- Uncorroborated -----------------------------------------------------
    if not matches:
        # Nothing to check against — the figure may be spelled out in prose
        # ("thirty million"), which the regex cannot see.
        return claude_usd, claude_raw or f"${claude_usd:,}", "medium", ""

    seen = ", ".join(m.raw for m in matches[:5])
    return claude_usd, claude_raw or f"${claude_usd:,}", "low", (
        f"Model reported {claude_raw or claude_usd} but the post's figures are "
        f"{seen}; could not corroborate."
    )


# ---------------------------------------------------------------------------
# Prompts and schemas
# ---------------------------------------------------------------------------

def _nullable(kind: str) -> dict:
    """A nullable field.

    Structured outputs document `anyOf` as supported; a bare `type` array is
    not listed, so this uses the form that is explicitly blessed.
    """
    return {"anyOf": [{"type": kind}, {"type": "null"}]}


# Kept deliberately thorough. Beyond precision, the length matters: the cache
# breakpoint sits on this system prompt, and Opus 5 only caches a prefix of at
# least 512 tokens. A terser version of this prompt measured ~429 tokens and
# silently never cached — every classifier call re-paid for it in full. The
# extra negative examples both improve the gate and keep the prompt safely
# above the threshold. If you trim it, re-check it still clears 512 tokens
# (see tests/test_extractor.py::TestClassifierPromptCaches).
CLASSIFIER_SYSTEM = """\
You are a filter on a venture-capital news pipeline. You decide one thing: is \
this blog post announcing that a SPECIFIC, NAMED company has raised a SPECIFIC \
funding round?

Answer true ONLY when both halves hold — a named portfolio company, and a \
round it has raised or is raising. A post may be an announcement even if no \
amount is stated; an undisclosed round still counts as true when a named \
company and a round are both present.

Answer false for everything else. The common false positives, all of which \
superficially resemble an announcement:

- The VC firm announcing its OWN new fund. "Antler raises additional $510 \
million", "$3.85 billion for early-stage investments", "announcing our third \
fund" — the firm is raising, not a portfolio company. This is the single most \
common false positive; when the entity raising is the publisher itself, \
answer false.
- An acquisition, merger, or majority buyout. "Cognition acquires TierZero" \
is not a round.
- An IPO, direct listing, SPAC, tender offer, or secondary share sale.
- Venture debt, credit facilities, grants, or other non-dilutive awards — \
these are not equity rounds. If the post is unclear whether it is debt or \
equity, lean false.
- Essays, market maps, theses, annual letters, and "state of X" reports, even \
when they cite funding amounts in passing.
- Podcast episodes, interviews, webinars, and video transcripts.
- Firm news: partner hires, promotions, office openings, fund anniversaries.
- Portfolio-company news that is not a raise: product launches, customer \
wins, hiring pushes, awards, research results, revenue milestones.
- Roundups or newsletters listing many companies without announcing one \
specific company's round.

Edge cases that ARE true: an extension or a bridge to an existing round, when \
a named company and a round label or amount are present; a single round \
announced jointly by several firms — still one company, still true; and a \
growth or later-stage round (Series D and beyond), which counts exactly the \
same as an early one. When a post is genuinely ambiguous between an \
announcement and commentary, weigh whether a specific company is named as \
having just raised — if so, true.

Give a one-sentence reason for the decision, and the company name when true.\
"""

CLASSIFIER_SCHEMA = {
    "type": "object",
    "properties": {
        "is_investment": {"type": "boolean"},
        "reason": {"type": "string"},
        "company_name": _nullable("string"),
    },
    "required": ["is_investment", "reason", "company_name"],
    "additionalProperties": False,
}

EXTRACTION_SYSTEM = f"""\
Extract the funding round announced in this post as structured data.

THE AMOUNT IS THE THING MOST OFTEN GOT WRONG. Read carefully.

`funding_amount_usd` must be THE AMOUNT RAISED IN THIS ROUND, normalized to \
whole US dollars. Announcements routinely carry several dollar figures, and \
only one of them is the round:

    "raised $30M in a Series B at a $300M valuation, bringing its total
     funding to $52M"

Here the round is $30,000,000. NOT $300,000,000 (that is what the company is \
worth) and NOT $52,000,000 (that is every round it has ever raised, added up). \
Never report a valuation, a post-money or pre-money figure, a cumulative or \
to-date total, a fund size, an acquisition price, or a revenue or ARR number \
as the round. Do not simply take the largest figure in the post — the largest \
figure is usually the valuation.

If the post does not state what was raised, set `funding_amount_usd` to null \
and `funding_amount_raw` to "Undisclosed". Do not guess, and do not \
substitute a valuation or a total. An undisclosed round is a valid, expected \
answer.

`amount_quote` must be the sentence you took the amount from, copied \
verbatim from the post — or an empty string when the amount is undisclosed. \
This is cross-checked against the post, so quote rather than paraphrase.

The other fields:

- `company_name`: the company that raised. Not the VC firm.
- `company_description`: one or two plain sentences on what the company \
actually does, written for someone who has never heard of it. No marketing \
language, no "revolutionary" or "leading".
- `company_url`: the company's own website if the post links or names it, \
otherwise null. Never the VC firm's site and never the post's own URL.
- `sector`: exactly one value from the fixed list. Use "{DEFAULT_SECTOR}" if \
none fits — do not stretch a label to fit.
- `sub_sector`: a short free-text refinement for colour, or an empty string.
- `round_stage`: exactly one value from the fixed list, "Unknown" if unstated.
- `co_investors`: other investors named as participating in THIS round. \
Exclude the firm publishing the post. Empty list if none are named.\
"""

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "company_name": {"type": "string"},
        "company_description": {"type": "string"},
        "company_url": _nullable("string"),
        "sector": {"type": "string", "enum": list(SECTORS)},
        "sub_sector": {"type": "string"},
        "funding_amount_usd": _nullable("integer"),
        "funding_amount_raw": {"type": "string"},
        "round_stage": {"type": "string", "enum": list(ROUND_STAGES)},
        "co_investors": {"type": "array", "items": {"type": "string"}},
        "amount_quote": {"type": "string"},
    },
    "required": [
        "company_name", "company_description", "company_url", "sector",
        "sub_sector", "funding_amount_usd", "funding_amount_raw",
        "round_stage", "co_investors", "amount_quote",
    ],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# The extractor
# ---------------------------------------------------------------------------

def _load_cache(path: str) -> dict:
    """Load the extraction cache, treating any problem as an empty cache.

    Deliberately unlike `state._load`, which raises on a corrupt file. State
    is the "nothing repeats" guarantee and silently forgetting it would resend
    everything; this cache is derived and disposable, so the safe failure is
    to re-derive it.
    """
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError, TypeError) as e:
        logger.warning(f"Discarding unreadable extraction cache at {path}: {e}")
        return {}


def _save_cache(path: str, data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError as e:  # a cache that won't persist is not worth failing over
        logger.warning(f"Could not write extraction cache to {path}: {e}")


class Extractor:
    """Runs the classifier gate and the structured extraction pass."""

    def __init__(
        self,
        client: Optional[Any] = None,
        cache_path: str = EXTRACTION_CACHE_FILE,
        use_cache: bool = True,
    ):
        self._client = client
        self.cache_path = cache_path
        self.use_cache = use_cache
        self.cache: dict = _load_cache(cache_path) if use_cache else {}
        self.calls_made = 0
        self.cache_hits = 0

    @property
    def client(self):
        """Constructed on first use so tests can run without an API key."""
        if self._client is None:
            self._client = anthropic.Anthropic()
        return self._client

    # ------------------------------------------------------------------
    # The Claude call
    # ------------------------------------------------------------------

    def _call(
        self, *, system: str, user: str, schema: dict, effort: str,
        max_tokens: int, label: str,
    ) -> Optional[dict]:
        """One structured-JSON request. Returns None on any failure.

        A failure here must never sink the run — one unreadable post is worth
        far less than the other thirteen sources.
        """
        try:
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=max_tokens,
                # The system prompt is a stable constant and the post body is
                # volatile, so the cache breakpoint goes on system: render
                # order is tools -> system -> messages, and every post in the
                # run reuses this prefix.
                system=[{
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }],
                output_config={
                    "effort": effort,
                    "format": {"type": "json_schema", "schema": schema},
                },
                messages=[{"role": "user", "content": user}],
            )
        except anthropic.NotFoundError as e:
            logger.error(f"[{label}] Model {MODEL} not available: {e}")
            return None
        except anthropic.RateLimitError as e:
            logger.error(f"[{label}] Rate limited: {e}")
            return None
        except anthropic.APIStatusError as e:
            logger.error(f"[{label}] API error {e.status_code}: {e}")
            return None
        except anthropic.APIConnectionError as e:
            logger.error(f"[{label}] Connection failed: {e}")
            return None

        self.calls_made += 1

        # A refusal is a successful HTTP 200 with empty or partial content, so
        # this has to be checked before content is touched.
        if response.stop_reason == "refusal":
            logger.warning(f"[{label}] Request declined by safety classifiers; skipping")
            return None
        if response.stop_reason == "max_tokens":
            logger.warning(f"[{label}] Response hit max_tokens; JSON is likely truncated")

        usage = getattr(response, "usage", None)
        if usage is not None:
            logger.debug(
                f"[{label}] tokens in={getattr(usage, 'input_tokens', '?')} "
                f"cached={getattr(usage, 'cache_read_input_tokens', '?')} "
                f"out={getattr(usage, 'output_tokens', '?')}"
            )

        text = next((b.text for b in response.content if b.type == "text"), "")
        if not text:
            logger.warning(f"[{label}] Response carried no text block")
            return None

        # The schema is enforced API-side, so this should not fail. If it ever
        # does, that is worth knowing about rather than swallowing.
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            logger.error(
                f"[{label}] Schema-constrained response did not parse as JSON "
                f"({e}); first 200 chars: {text[:200]!r}"
            )
            return None

        return parsed if isinstance(parsed, dict) else None

    def _cached(self, namespace: str, post: BlogPost) -> Optional[dict]:
        if not self.use_cache:
            return None
        hit = self.cache.get(f"{namespace}:{PROMPT_VERSION}:{post.content_hash}")
        if hit is not None:
            self.cache_hits += 1
        return hit

    def _store(self, namespace: str, post: BlogPost, value: dict) -> None:
        if self.use_cache:
            self.cache[f"{namespace}:{PROMPT_VERSION}:{post.content_hash}"] = value

    def save_cache(self) -> None:
        if self.use_cache:
            _save_cache(self.cache_path, self.cache)

    # ------------------------------------------------------------------
    # Stage 1 — the gate
    # ------------------------------------------------------------------

    def classify(self, post: BlogPost) -> tuple[bool, str]:
        """Is this post announcing a specific company raising a specific round?"""
        # The source already knows, from the URL path or the firm's own tag.
        # That beats any LLM judgement and costs nothing.
        if post.likely_investment is True:
            return True, "source classified it (URL path or firm tag)"

        cached = self._cached("classify", post)
        if cached is not None:
            return bool(cached.get("is_investment")), cached.get("reason", "") + " (cached)"

        labels = f"Labels: {', '.join(post.labels)}\n" if post.labels else ""
        user = (
            f"Firm: {post.vc_firm}\n"
            f"Title: {post.title}\n"
            f"{labels}"
            f"URL: {post.url}\n\n"
            f"{post.body[:CLASSIFY_BODY_CHARS]}"
        )

        result = self._call(
            system=CLASSIFIER_SYSTEM,
            user=user,
            schema=CLASSIFIER_SCHEMA,
            effort=CLASSIFY_EFFORT,
            max_tokens=CLASSIFY_MAX_TOKENS,
            label=f"classify {post.vc_firm}",
        )
        if result is None:
            # Fail closed. A post we could not read is not evidence of a round,
            # and an essay in the digest is worse than a missed announcement.
            return False, "classifier call failed"

        self._store("classify", post, result)
        return bool(result.get("is_investment")), str(result.get("reason", ""))

    # ------------------------------------------------------------------
    # Stage 3 — structured extraction
    # ------------------------------------------------------------------

    def extract(self, post: BlogPost) -> Optional[Investment]:
        """Pull the round out of a post that has passed the gate."""
        result = self._cached("extract", post)
        if result is None:
            user = (
                f"Firm publishing this post: {post.vc_firm}\n"
                f"Title: {post.title}\n"
                f"URL: {post.url}\n\n"
                f"{post.body[:EXTRACT_BODY_CHARS]}"
            )
            result = self._call(
                system=EXTRACTION_SYSTEM,
                user=user,
                schema=EXTRACTION_SCHEMA,
                effort=EXTRACT_EFFORT,
                max_tokens=EXTRACT_MAX_TOKENS,
                label=f"extract {post.vc_firm}",
            )
            if result is None:
                return None
            self._store("extract", post, result)

        company = (result.get("company_name") or "").strip()
        if not company:
            logger.warning(f"Extraction returned no company name for {post.url}")
            return None

        # The title often carries the amount ("Leland's $12M Series A") while
        # the body does not, so both are cross-checked.
        haystack = f"{post.title}\n{post.body[:EXTRACT_BODY_CHARS]}"

        claude_usd = result.get("funding_amount_usd")
        if isinstance(claude_usd, bool) or not isinstance(claude_usd, (int, float)):
            claude_usd = None
        else:
            claude_usd = int(claude_usd)

        usd, raw, confidence, notes = reconcile_amount(
            haystack, claude_usd, str(result.get("funding_amount_raw") or "")
        )

        stage = str(result.get("round_stage") or "Unknown")
        if stage not in ROUND_STAGES:
            stage = "Unknown"
        if stage == "Unknown":
            stage = find_stage(haystack)

        co_investors = [
            str(x).strip() for x in (result.get("co_investors") or [])
            if str(x).strip() and str(x).strip().lower() != post.vc_firm.lower()
        ]

        return Investment(
            company_name=company,
            company_description=str(result.get("company_description") or "").strip(),
            company_url=(result.get("company_url") or None),
            sector=normalize_sector(result.get("sector")),
            sub_sector=str(result.get("sub_sector") or "").strip(),
            funding_amount_usd=usd,
            funding_amount_raw=raw or "Undisclosed",
            round_stage=stage,
            co_investors=co_investors,
            vc_firms=[post.vc_firm],
            source_posts=[post],
            confidence=confidence,
            notes=notes,
        )

    # ------------------------------------------------------------------

    def run(self, posts: list[BlogPost]) -> tuple[list[Investment], list[tuple[BlogPost, str]]]:
        """Gate then extract a batch. Returns (investments, rejections)."""
        investments: list[Investment] = []
        rejected: list[tuple[BlogPost, str]] = []

        for post in posts:
            try:
                keep, reason = self.classify(post)
                if not keep:
                    logger.info(f"Dropped [{post.vc_firm}] {post.title[:60]} — {reason}")
                    rejected.append((post, reason))
                    continue

                investment = self.extract(post)
                if investment is None:
                    rejected.append((post, "extraction failed"))
                    continue

                investments.append(investment)
            except Exception as e:  # noqa: BLE001 — one bad post must not sink the run
                logger.error(f"Extraction blew up on {post.url}: {e}")
                rejected.append((post, f"error: {e}"))

        self.save_cache()
        logger.info(
            f"Extracted {len(investments)} investments from {len(posts)} posts "
            f"({len(rejected)} rejected, {self.calls_made} API calls, "
            f"{self.cache_hits} cache hits)"
        )
        return investments, rejected
