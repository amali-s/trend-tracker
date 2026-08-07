"""Offline tests for the classifier gate, the regex pass, and the amount guard.

No network and no API key: the Anthropic client is stubbed with canned
responses, and `Extractor.client` is only constructed on first use precisely so
this works.

The tests that matter most are in TestValuationTrap. A wrong funding number
looks exactly like a right one in a nice-looking email, so it has to be caught
here or not at all.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.extractor import (  # noqa: E402
    CLASSIFIER_SCHEMA,
    EXTRACTION_SCHEMA,
    ROUND_STAGES,
    Extractor,
    find_amounts,
    find_stage,
    parse_amount,
    reconcile_amount,
    round_amounts,
)
from src.models import BlogPost  # noqa: E402
from src.sectors import SECTORS, normalize_sector  # noqa: E402

# The canonical trap from PLAN §5.
TRAP = (
    "Acme raised $30M in a Series B at a $300M valuation, "
    "bringing its total funding to $52M."
)


# ---------------------------------------------------------------------------
# Stub client
# ---------------------------------------------------------------------------

class _TextBlock:
    type = "text"

    def __init__(self, text: str):
        self.text = text


class _ThinkingBlock:
    """Opus 5 has thinking on by default; its text is empty and must be skipped."""

    type = "thinking"
    thinking = ""


class _Usage:
    input_tokens = 100
    cache_read_input_tokens = 0
    output_tokens = 50


class _Response:
    def __init__(self, payload, stop_reason="end_turn", blocks=None):
        if blocks is not None:
            self.content = blocks
        else:
            self.content = [_ThinkingBlock(), _TextBlock(json.dumps(payload))]
        self.stop_reason = stop_reason
        self.usage = _Usage()


class _Messages:
    def __init__(self, responses):
        self.queued = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.queued:
            raise AssertionError("stub client ran out of queued responses")
        nxt = self.queued.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class StubClient:
    def __init__(self, *responses):
        self.messages = _Messages(responses)


def make_post(**kwargs) -> BlogPost:
    defaults = {
        "url": "https://greylock.com/blog/acme-series-b",
        "title": "Investing in Acme",
        "vc_firm": "Greylock",
        "body": TRAP,
    }
    defaults.update(kwargs)
    return BlogPost(**defaults)


def extractor(*responses, **kwargs) -> Extractor:
    kwargs.setdefault("use_cache", False)
    return Extractor(client=StubClient(*responses), **kwargs)


EXTRACTION_PAYLOAD = {
    "company_name": "Acme",
    "company_description": "Acme builds warehouse robots.",
    "company_url": "https://acme.com",
    "sector": "Robotics & Hardware",
    "sub_sector": "warehouse automation",
    "funding_amount_usd": 30_000_000,
    "funding_amount_raw": "$30M",
    "round_stage": "Series B",
    "co_investors": ["Sequoia", "Greylock"],
    "amount_quote": "Acme raised $30M in a Series B",
}


# ---------------------------------------------------------------------------
# The regex pass
# ---------------------------------------------------------------------------

class TestParseAmount:
    @pytest.mark.parametrize("number,mult,expected", [
        ("30", "M", 30_000_000),
        ("30", "million", 30_000_000),
        ("1.5", "billion", 1_500_000_000),
        ("1.5", "B", 1_500_000_000),
        ("2", "bn", 2_000_000_000),
        ("750", "K", 750_000),
        ("750,000", None, 750_000),
        ("12", "mm", 12_000_000),
    ])
    def test_normalizes_to_dollars(self, number, mult, expected):
        assert parse_amount(number, mult) == expected

    def test_rejects_garbage(self):
        assert parse_amount("not-a-number", "M") is None


class TestFindAmounts:
    def test_finds_every_figure_not_just_one(self):
        found = find_amounts(TRAP)
        assert [m.usd for m in found] == [30_000_000, 300_000_000, 52_000_000]

    def test_handles_unsuffixed_and_decimal_figures(self):
        found = find_amounts("a $750,000 pre-seed and a $1.5 billion round")
        assert [m.usd for m in found] == [750_000, 1_500_000_000]

    def test_keeps_the_raw_string_for_display(self):
        assert find_amounts("raised $30M today")[0].raw == "$30M"

    def test_empty_text_is_not_an_error(self):
        assert find_amounts("") == []
        assert find_amounts(None) == []


class TestAmountTagging:
    def test_tags_the_valuation_and_the_cumulative_total(self):
        by_value = {m.usd: m.kind for m in find_amounts(TRAP)}
        assert by_value[30_000_000] == "round"
        assert by_value[300_000_000] == "valuation"
        assert by_value[52_000_000] == "cumulative"

    def test_round_amounts_returns_only_the_round(self):
        assert [m.usd for m in round_amounts(TRAP)] == [30_000_000]

    def test_marker_claims_the_nearest_figure_not_every_nearby_one(self):
        """A ±N-char window around $30M would sweep up "valuation"."""
        assert round_amounts("raised $30M at a $300M valuation")[0].usd == 30_000_000

    def test_valuation_stated_before_the_figure(self):
        by_value = {m.usd: m.kind for m in find_amounts(
            "a Series A of $8M at a post-money valuation of $80M"
        )}
        assert by_value[8_000_000] == "round"
        assert by_value[80_000_000] == "valuation"

    def test_has_raised_is_not_treated_as_cumulative(self):
        """"Acme has raised $30M in a Series B" is the round itself."""
        assert round_amounts("Acme has raised $30M in a Series B")[0].usd == 30_000_000

    def test_distant_marker_does_not_claim_a_figure(self):
        text = "Acme raised $30M. " + ("Filler sentence about the team. " * 6) + "Valuation was not disclosed."
        assert round_amounts(text)[0].usd == 30_000_000

    # --- regressions, all found by probing real sentence shapes ------------

    def test_marker_phrase_running_toward_its_figure(self):
        """Proximity alone tagged the round here: only "valuing" was matched,
        so "the company at" counted as distance and the left-hand figure won."""
        by_value = {m.usd: m.kind for m in find_amounts(
            "We led the $12.5 million Series A, valuing the company at $120 million post-money."
        )}
        assert by_value[12_500_000] == "round"
        assert by_value[120_000_000] == "valuation"

    def test_multi_word_cumulative_phrase(self):
        """Same failure on the cumulative side: "brings total" ... "to $11M"."""
        by_value = {m.usd: m.kind for m in find_amounts(
            "The $8M seed brings total capital raised to $11M to date."
        )}
        assert by_value[8_000_000] == "round"
        assert by_value[11_000_000] == "cumulative"

    def test_marker_cannot_reach_across_a_sentence_boundary(self):
        assert round_amounts("The valuation was not disclosed. Acme raised $30M.")[0].usd == 30_000_000

    def test_valuation_in_a_following_sentence_still_binds_backward(self):
        by_value = {m.usd: m.kind for m in find_amounts(
            "Acme closed a $20M Series A. Its post-money valuation is $180M."
        )}
        assert by_value[20_000_000] == "round"
        assert by_value[180_000_000] == "valuation"

    def test_revenue_is_not_mistaken_for_the_round(self):
        by_value = {m.usd: m.kind for m in find_amounts(
            "The company reached $10M ARR before raising its $45M Series B."
        )}
        assert by_value[10_000_000] == "revenue"
        assert by_value[45_000_000] == "round"

    def test_an_unattached_marker_claims_nothing(self):
        """No nearest-figure fallback — guessing corrupts the headline number."""
        assert round_amounts("Acme raised $30M. Revenue growth was strong.")[0].usd == 30_000_000


class TestFindStage:
    @pytest.mark.parametrize("text,expected", [
        ("a Series B round", "Series B"),
        ("its Series A", "Series A"),
        ("pre-seed funding", "Pre-Seed"),
        ("preseed funding", "Pre-Seed"),
        ("a seed round", "Seed"),
        ("growth round", "Growth"),
        ("a bridge financing", "Bridge"),
        ("no stage mentioned here", "Unknown"),
    ])
    def test_normalizes_to_the_enum(self, text, expected):
        stage = find_stage(text)
        assert stage == expected
        assert stage in ROUND_STAGES


# ---------------------------------------------------------------------------
# The valuation guard
# ---------------------------------------------------------------------------

class TestValuationTrap:
    def test_correct_amount_is_corroborated_and_marked_high(self):
        usd, raw, confidence, notes = reconcile_amount(TRAP, 30_000_000, "$30M")
        assert usd == 30_000_000
        assert confidence == "high"
        assert notes == ""

    def test_a_returned_valuation_is_overridden_with_the_round(self):
        usd, raw, confidence, notes = reconcile_amount(TRAP, 300_000_000, "$300M")
        assert usd == 30_000_000, "the $300M valuation must not be reported as the round"
        assert raw == "$30M"
        assert confidence == "low"
        assert "valuation" in notes

    def test_a_returned_cumulative_total_is_overridden_too(self):
        usd, _, confidence, notes = reconcile_amount(TRAP, 52_000_000, "$52M")
        assert usd == 30_000_000
        assert confidence == "low"
        assert "cumulative" in notes

    def test_never_picks_the_largest_figure(self):
        """The whole failure mode in one assertion."""
        for claimed in (30_000_000, 300_000_000, 52_000_000):
            usd, _, _, _ = reconcile_amount(TRAP, claimed, "")
            assert usd == 30_000_000


class TestReconcileAmount:
    def test_undisclosed_with_no_figures_is_high_confidence(self):
        usd, raw, confidence, notes = reconcile_amount(
            "Acme raised a seed round.", None, "Undisclosed"
        )
        assert usd is None
        assert raw == "Undisclosed"
        assert confidence == "high"
        assert notes == ""

    def test_undisclosed_despite_a_figure_in_the_post_is_flagged(self):
        usd, _, confidence, notes = reconcile_amount(
            "Acme raised $30M in a seed round.", None, "Undisclosed"
        )
        assert usd is None
        assert confidence == "medium"
        assert "$30M" in notes

    def test_only_figure_is_a_valuation_reports_undisclosed(self):
        usd, raw, confidence, notes = reconcile_amount(
            "Acme is now valued at a $300M valuation.", 300_000_000, "$300M"
        )
        assert usd is None
        assert raw == "Undisclosed"
        assert confidence == "low"
        assert "valuation" in notes

    def test_figure_spelled_out_in_prose_is_medium(self):
        usd, _, confidence, _ = reconcile_amount(
            "Acme raised thirty million dollars.", 30_000_000, "$30M"
        )
        assert usd == 30_000_000
        assert confidence == "medium"

    def test_uncorroborated_figure_is_kept_but_flagged(self):
        usd, _, confidence, notes = reconcile_amount(
            "Acme raised $30M in a Series B.", 45_000_000, "$45M"
        )
        assert usd == 45_000_000
        assert confidence == "low"
        assert "could not corroborate" in notes

    def test_earliest_round_figure_wins_never_the_largest(self):
        text = "Acme raised $30M, more than the $40M valuation cap, at a $300M valuation."
        usd, _, _, _ = reconcile_amount(text, 300_000_000, "$300M")
        assert usd == 30_000_000


# ---------------------------------------------------------------------------
# The classifier gate
# ---------------------------------------------------------------------------

class TestClassify:
    def test_source_classification_short_circuits_the_llm(self):
        """a16z's /announcement/ path and Sequoia's tag beat any LLM call."""
        ex = extractor()  # no queued responses — a call would blow up
        keep, reason = ex.classify(make_post(likely_investment=True))
        assert keep is True
        assert ex.calls_made == 0
        assert ex.client.messages.calls == []

    def test_accepts_an_announcement(self):
        ex = extractor(_Response({
            "is_investment": True, "reason": "Acme raised a Series B", "company_name": "Acme",
        }))
        keep, reason = ex.classify(make_post())
        assert keep is True
        assert "Series B" in reason

    def test_rejects_the_firms_own_fundraise(self):
        ex = extractor(_Response({
            "is_investment": False,
            "reason": "Antler is raising its own fund, not a portfolio company",
            "company_name": None,
        }))
        keep, reason = ex.classify(make_post(
            vc_firm="Antler", title="Antler raises additional $510 million"
        ))
        assert keep is False
        assert "own fund" in reason

    def test_fails_closed_when_the_call_fails(self):
        """An essay in the digest is worse than a missed announcement."""
        ex = extractor(_Response(None, blocks=[]))
        keep, _ = ex.classify(make_post())
        assert keep is False

    def test_sends_labels_and_title_to_the_model(self):
        ex = extractor(_Response({
            "is_investment": True, "reason": "yes", "company_name": "Acme",
        }))
        ex.classify(make_post(labels=["Funding announcement"]))
        sent = ex.client.messages.calls[0]["messages"][0]["content"]
        assert "Funding announcement" in sent
        assert "Investing in Acme" in sent


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------

class TestRequestShape:
    def _call_kwargs(self) -> dict:
        ex = extractor(_Response({
            "is_investment": True, "reason": "yes", "company_name": "Acme",
        }))
        ex.classify(make_post())
        return ex.client.messages.calls[0]

    def test_uses_structured_outputs_not_a_json_instruction(self):
        fmt = self._call_kwargs()["output_config"]["format"]
        assert fmt["type"] == "json_schema"
        assert fmt["schema"] == CLASSIFIER_SCHEMA

    def test_caches_the_system_prompt(self):
        system = self._call_kwargs()["system"]
        assert system[0]["cache_control"] == {"type": "ephemeral"}

    def test_sets_no_sampling_parameters(self):
        """temperature / top_p / top_k are a 400 on this model."""
        kwargs = self._call_kwargs()
        assert not {"temperature", "top_p", "top_k"} & set(kwargs)

    def test_leaves_room_for_thinking_in_max_tokens(self):
        assert self._call_kwargs()["max_tokens"] >= 2000


class TestFailureHandling:
    def test_a_refusal_is_not_read_as_content(self):
        ex = extractor(_Response({}, stop_reason="refusal", blocks=[]))
        keep, _ = ex.classify(make_post())
        assert keep is False

    def test_malformed_json_does_not_raise(self):
        ex = extractor(_Response(None, blocks=[_TextBlock("{not json")]))
        keep, _ = ex.classify(make_post())
        assert keep is False

    def test_api_errors_are_caught(self):
        err = __import__("anthropic").APIConnectionError(request=None)
        ex = extractor(err)
        keep, _ = ex.classify(make_post())
        assert keep is False


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

class TestExtract:
    def test_builds_an_investment(self):
        ex = extractor(_Response(EXTRACTION_PAYLOAD))
        inv = ex.extract(make_post())
        assert inv is not None
        assert inv.company_name == "Acme"
        assert inv.funding_amount_usd == 30_000_000
        assert inv.round_stage == "Series B"
        assert inv.sector == "Robotics & Hardware"
        assert inv.vc_firms == ["Greylock"]
        assert inv.source_posts[0].url.endswith("acme-series-b")
        assert inv.confidence == "high"

    def test_valuation_trap_end_to_end(self):
        """The model returns the valuation; the guard has to catch it."""
        payload = dict(EXTRACTION_PAYLOAD, funding_amount_usd=300_000_000,
                       funding_amount_raw="$300M")
        inv = extractor(_Response(payload)).extract(make_post())
        assert inv.funding_amount_usd == 30_000_000
        assert inv.funding_amount_raw == "$30M"
        assert inv.confidence == "low"
        assert "valuation" in inv.notes

    def test_undisclosed_round_is_kept_not_dropped(self):
        payload = dict(EXTRACTION_PAYLOAD, funding_amount_usd=None,
                       funding_amount_raw="Undisclosed", amount_quote="")
        inv = extractor(_Response(payload)).extract(
            make_post(body="Acme raised a Series B. Terms were not disclosed.")
        )
        assert inv is not None, "undisclosed rounds still count as deal flow"
        assert inv.funding_amount_usd is None
        assert inv.funding_amount_raw == "Undisclosed"

    def test_publishing_firm_is_not_listed_as_a_co_investor(self):
        inv = extractor(_Response(EXTRACTION_PAYLOAD)).extract(make_post())
        assert "Greylock" not in inv.co_investors
        assert inv.co_investors == ["Sequoia"]

    def test_unknown_stage_falls_back_to_the_regex(self):
        payload = dict(EXTRACTION_PAYLOAD, round_stage="Unknown")
        inv = extractor(_Response(payload)).extract(make_post())
        assert inv.round_stage == "Series B"

    def test_off_enum_sector_becomes_other(self):
        payload = dict(EXTRACTION_PAYLOAD, sector="Vertical AI Agents")
        inv = extractor(_Response(payload)).extract(make_post())
        assert inv.sector == "Other"

    def test_a_nameless_extraction_is_discarded(self):
        payload = dict(EXTRACTION_PAYLOAD, company_name="  ")
        assert extractor(_Response(payload)).extract(make_post()) is None

    def test_amount_in_the_title_is_cross_checked(self):
        """Index Ventures and Accel put the round size in the title."""
        payload = dict(EXTRACTION_PAYLOAD, funding_amount_usd=12_000_000,
                       funding_amount_raw="$12M")
        inv = extractor(_Response(payload)).extract(
            make_post(title="Leland's $12M Series A", body="A short post with no figures.")
        )
        assert inv.funding_amount_usd == 12_000_000
        assert inv.confidence == "high"


# ---------------------------------------------------------------------------
# Batch behaviour and caching
# ---------------------------------------------------------------------------

class TestRun:
    def test_gate_then_extract(self, tmp_path):
        ex = extractor(
            _Response({"is_investment": True, "reason": "yes", "company_name": "Acme"}),
            _Response(EXTRACTION_PAYLOAD),
            _Response({"is_investment": False, "reason": "essay", "company_name": None}),
        )
        investments, rejected = ex.run([
            make_post(),
            make_post(url="https://greylock.com/blog/an-essay", title="On market structure",
                      body="A long essay about market structure."),
        ])
        assert len(investments) == 1
        assert len(rejected) == 1
        assert rejected[0][1] == "essay"

    def test_one_bad_post_does_not_sink_the_run(self):
        ex = extractor(
            RuntimeError("boom"),
            _Response({"is_investment": True, "reason": "yes", "company_name": "Acme"}),
            _Response(EXTRACTION_PAYLOAD),
        )
        # The first post's classify call raises inside _call's client, which is
        # not an anthropic error — run()'s guard has to catch it.
        investments, rejected = ex.run([
            make_post(url="https://greylock.com/blog/broken", body="broken"),
            make_post(),
        ])
        assert len(investments) == 1
        assert len(rejected) == 1


class TestCache:
    def test_second_run_reuses_the_cached_result(self, tmp_path):
        cache = str(tmp_path / "extraction_cache.json")
        post = make_post()

        first = Extractor(
            client=StubClient(
                _Response({"is_investment": True, "reason": "yes", "company_name": "Acme"}),
                _Response(EXTRACTION_PAYLOAD),
            ),
            cache_path=cache,
        )
        first.run([post])
        assert first.calls_made == 2
        assert os.path.exists(cache)

        # No queued responses at all: a second API call would raise.
        second = Extractor(client=StubClient(), cache_path=cache)
        investments, _ = second.run([post])
        assert len(investments) == 1
        assert second.calls_made == 0
        assert second.cache_hits == 2

    def test_an_unreadable_cache_is_discarded_not_fatal(self, tmp_path):
        cache = tmp_path / "extraction_cache.json"
        cache.write_text("{ this is not json")
        ex = Extractor(client=StubClient(), cache_path=str(cache))
        assert ex.cache == {}


# ---------------------------------------------------------------------------
# Schema sanity — structured outputs rejects a malformed schema at request time
# ---------------------------------------------------------------------------

class TestSchemas:
    @pytest.mark.parametrize("schema", [CLASSIFIER_SCHEMA, EXTRACTION_SCHEMA])
    def test_every_property_is_required(self, schema):
        assert set(schema["required"]) == set(schema["properties"])

    @pytest.mark.parametrize("schema", [CLASSIFIER_SCHEMA, EXTRACTION_SCHEMA])
    def test_additional_properties_disallowed(self, schema):
        assert schema["additionalProperties"] is False

    def test_nullable_fields_use_anyof_not_a_type_array(self):
        """Structured outputs documents anyOf; a bare type array is not listed."""
        assert EXTRACTION_SCHEMA["properties"]["funding_amount_usd"]["anyOf"] == [
            {"type": "integer"}, {"type": "null"},
        ]

    def test_sector_is_constrained_to_the_fixed_taxonomy(self):
        assert EXTRACTION_SCHEMA["properties"]["sector"]["enum"] == list(SECTORS)

    def test_stage_is_constrained_to_the_fixed_list(self):
        assert EXTRACTION_SCHEMA["properties"]["round_stage"]["enum"] == list(ROUND_STAGES)


class TestClassifierPromptCaches:
    def test_classifier_system_prompt_clears_the_512_token_cache_floor(self):
        """Opus 5 only caches a prefix of >=512 tokens. The cache breakpoint is
        on this system prompt, so if it drops below the floor it silently stops
        caching and every classifier call re-pays for it in full.

        Guarded by character count (no tokenizer needed): 2048 chars is 512
        tokens even at the densest classic ratio of 4 chars/token, so clearing
        it means the prompt caches under any tokenizer Opus 5 might use.
        """
        from src.extractor import CLASSIFIER_SYSTEM
        assert len(CLASSIFIER_SYSTEM) >= 2048, (
            f"classifier prompt is {len(CLASSIFIER_SYSTEM)} chars; below ~2048 "
            f"it may fall under Opus 5's 512-token cache floor"
        )


class TestNormalizeSector:
    @pytest.mark.parametrize("value,expected", [
        ("Fintech", "Fintech"),
        ("fintech", "Fintech"),
        ("  AI Infrastructure  ", "AI Infrastructure"),
        ("Vertical SaaS for dentists", "Other"),
        ("", "Other"),
        (None, "Other"),
    ])
    def test_maps_onto_the_enum(self, value, expected):
        assert normalize_sector(value) == expected
