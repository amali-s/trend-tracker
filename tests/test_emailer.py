"""Offline tests for the email renderer and the provider selection.

No SMTP, no network, no API key. The HTML is rendered from a fixture digest
and asserted against the five email constraints in PLAN §7, plus escaping,
the three delta states, the empty-week rule, and low-confidence surfacing.

Provider selection is exercised by monkeypatching os.environ and stubbing the
transport — no message ever leaves the process.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import emailer, sage  # noqa: E402
from src.models import BlogPost, Investment, SourceSummary  # noqa: E402
from src.trends import build_digest  # noqa: E402

WEEK = date(2026, 8, 8)


def inv(**kw) -> Investment:
    defaults = dict(
        company_name="Acme",
        company_description="Acme builds warehouse robots.",
        company_url="https://acme.com",
        sector="Robotics & Hardware",
        funding_amount_usd=30_000_000,
        funding_amount_raw="$30M",
        round_stage="Series B",
        vc_firms=["Greylock"],
        co_investors=["Sequoia"],
        confidence="high",
    )
    defaults.update(kw)
    inv_obj = Investment(**{k: v for k, v in defaults.items() if k != "url"})
    inv_obj.source_posts = [BlogPost(
        url="https://greylock.com/blog/acme", title="Acme", vc_firm="Greylock"
    )]
    return inv_obj


def digest(investments=None, history=None, sources=None):
    return build_digest(
        investments if investments is not None else [inv()],
        history or {},
        WEEK,
        source_summaries=sources or [SourceSummary(source="Greylock", posts_found=5, new_posts=1)],
    )


def html_of(*args, **kw) -> str:
    return emailer.render_digest(digest(*args, **kw))


# ---------------------------------------------------------------------------
# The five email constraints (PLAN §7)
# ---------------------------------------------------------------------------

class TestEmailSafety:
    def test_no_flexbox(self):
        assert "display:flex" not in html_of().replace(" ", "")

    def test_no_tailwind_or_css_classes(self):
        # No class attributes anywhere — everything is inline style.
        assert not re.search(r'\sclass\s*=', html_of())

    def test_no_external_stylesheets_or_font_imports(self):
        h = html_of()
        assert "<link" not in h
        assert "@font-face" not in h
        assert "@import" not in h

    def test_layout_is_table_based(self):
        h = html_of()
        assert 'role="presentation"' in h
        assert h.count("<table") >= 3

    def test_container_is_600px_not_the_component_width(self):
        h = html_of()
        assert f"width:{sage.CONTAINER_WIDTH}px" in h
        assert "248px" not in h

    def test_fallback_font_stacks_present(self):
        h = html_of()
        assert "Georgia" in h          # the serif fallback most clients have
        assert "sans-serif" in h

    def test_opts_out_of_dark_mode_inversion(self):
        h = html_of()
        assert 'name="color-scheme"' in h
        assert "light only" in h

    def test_every_link_carries_an_explicit_colour(self):
        h = html_of()
        for anchor in re.findall(r"<a\b[^>]*>", h):
            assert "color:" in anchor, f"link without explicit colour: {anchor}"


# ---------------------------------------------------------------------------
# Sage tokens actually reach the markup
# ---------------------------------------------------------------------------

class TestSageTokens:
    def test_palette_is_used(self):
        h = html_of()
        for token in (sage.LAYER_1, sage.ACCENT, sage.PALE_MUSTARD, sage.PRIMARY, sage.TEXT_PRIMARY):
            assert token in h, f"missing Sage token {token}"

    def test_accent_slot_is_a_table_cell_not_a_span(self):
        """Outlook drops padding on inline elements, so the amount slot must
        be a real table cell."""
        h = emailer._amount_slot(inv())
        assert "<table" in h
        assert sage.ACCENT in h


# ---------------------------------------------------------------------------
# Escaping and URL safety
# ---------------------------------------------------------------------------

class TestEscaping:
    def test_company_name_is_escaped(self):
        h = html_of([inv(company_name="Acme <script>alert(1)</script> & Co")])
        assert "<script>" not in h
        assert "&lt;script&gt;" in h
        assert "&amp; Co" in h

    def test_javascript_url_never_reaches_an_href(self):
        h = html_of([inv(company_url="javascript:alert(1)")])
        assert "javascript:" not in h
        assert "Visit site" not in h  # the action link is dropped entirely

    def test_data_url_is_rejected(self):
        assert emailer._safe_url("data:text/html,<script>") is None

    def test_plain_http_urls_pass(self):
        assert emailer._safe_url("https://acme.com") == "https://acme.com"
        assert emailer._safe_url("http://acme.com") == "http://acme.com"

    def test_empty_url_is_none(self):
        assert emailer._safe_url("") is None
        assert emailer._safe_url(None) is None


# ---------------------------------------------------------------------------
# Subject
# ---------------------------------------------------------------------------

class TestSubject:
    def test_matches_weekrange_and_carries_no_emoji(self):
        d = digest()
        assert d.subject == "Trend Tracker August 2-8 2026"
        assert d.subject.isascii()


# ---------------------------------------------------------------------------
# Deltas — the three render states
# ---------------------------------------------------------------------------

class TestDeltaStates:
    def _row(self, delta_usd, delta_pct):
        class _R:
            pass
        r = _R()
        r.delta_usd, r.delta_pct = delta_usd, delta_pct
        return r

    def test_percentage_when_there_is_a_prior_figure(self):
        cell = emailer._delta_cell(self._row(20_000_000, 200.0))
        assert "200%" in cell
        assert sage.DELTA_UP in cell

    def test_new_when_baseline_was_zero(self):
        cell = emailer._delta_cell(self._row(40_000_000, None))
        assert "new" in cell
        assert "%" not in cell

    def test_em_dash_when_no_delta_at_all(self):
        cell = emailer._delta_cell(self._row(None, None))
        assert "&mdash;" in cell

    def test_down_mover_uses_the_down_colour(self):
        cell = emailer._delta_cell(self._row(-30_000_000, -100.0))
        assert sage.DELTA_DOWN in cell

    def test_cold_start_table_shows_no_deltas(self):
        """No baseline → every delta cell is an em dash, no up/down colours."""
        rendered = emailer._sector_table(digest())  # empty history
        assert sage.DELTA_UP not in rendered
        assert sage.DELTA_DOWN not in rendered
        assert "&mdash;" in rendered


# ---------------------------------------------------------------------------
# Low confidence
# ---------------------------------------------------------------------------

class TestConfidence:
    def test_low_confidence_note_is_shown(self):
        h = html_of([inv(confidence="low", notes="Model reported $300M, which reads as a valuation.")])
        assert "valuation" in h
        assert sage.WARN_BG in h

    def test_high_confidence_shows_no_strip(self):
        h = html_of([inv(confidence="high")])
        assert sage.WARN_BG not in h


# ---------------------------------------------------------------------------
# Undisclosed footnote
# ---------------------------------------------------------------------------

class TestUndisclosed:
    def test_undisclosed_count_is_in_the_headline(self):
        h = html_of([inv(company_name="A"), inv(company_name="B", funding_amount_usd=None,
                                                 funding_amount_raw="Undisclosed")])
        assert "1 undisclosed" in h

    def test_undisclosed_marked_in_sector_row(self):
        rendered = emailer._sector_table(digest([
            inv(company_name="A", sector="Fintech", funding_amount_usd=None,
                funding_amount_raw="Undisclosed"),
        ]))
        assert "undisc" in rendered


# ---------------------------------------------------------------------------
# Empty week
# ---------------------------------------------------------------------------

class TestEmptyWeek:
    def test_renders_a_no_activity_email(self):
        h = html_of([], sources=[SourceSummary(source="Contrary", posts_found=0)])
        assert "No new investments this week" in h

    def test_source_table_survives_an_empty_week(self):
        h = html_of([], sources=[SourceSummary(source="Contrary", posts_found=0)])
        assert "Contrary" in h
        assert "0 posts" in h

    def test_silence_means_breakage_note_present(self):
        assert "silence means" in html_of().lower()


# ---------------------------------------------------------------------------
# Source footer health
# ---------------------------------------------------------------------------

class TestSourceFooter:
    def test_error_source_flagged_in_red(self):
        h = html_of(sources=[SourceSummary(source="NEA", error="timeout")])
        assert "error: timeout" in h
        assert sage.DELTA_DOWN in h

    def test_zero_post_source_flagged(self):
        h = html_of(sources=[SourceSummary(source="Battery Ventures", posts_found=0)])
        assert "0 posts" in h


# ---------------------------------------------------------------------------
# Plain-text alternative
# ---------------------------------------------------------------------------

class TestPlainText:
    def test_carries_company_names_and_amounts(self):
        text = emailer.render_text(digest([inv(company_name="Acme", funding_amount_raw="$30M")]))
        assert "Acme" in text
        assert "$30M" in text

    def test_low_confidence_flagged_in_text(self):
        text = emailer.render_text(digest([inv(confidence="low", notes="check the source")]))
        assert "check the source" in text

    def test_empty_week_in_text(self):
        text = emailer.render_text(digest([]))
        assert "No new investments this week" in text


# ---------------------------------------------------------------------------
# Provider selection
# ---------------------------------------------------------------------------

class TestSend:
    def test_sendgrid_wins_when_its_key_is_set(self, monkeypatch):
        calls = {}
        monkeypatch.setenv("EMAIL_TO", "me@example.com")
        monkeypatch.setenv("SENDGRID_API_KEY", "SG.x")
        monkeypatch.setenv("GMAIL_USER", "me@gmail.com")
        monkeypatch.setenv("GMAIL_APP_PASSWORD", "x")
        monkeypatch.setattr(emailer, "_send_sendgrid", lambda *a: calls.setdefault("sg", True) or True)
        monkeypatch.setattr(emailer, "_send_gmail", lambda *a: calls.setdefault("gm", True) or True)
        assert emailer.send("s", "<html>", "text") is True
        assert calls == {"sg": True}

    def test_falls_back_to_gmail(self, monkeypatch):
        calls = {}
        monkeypatch.delenv("SENDGRID_API_KEY", raising=False)
        monkeypatch.setenv("EMAIL_TO", "me@example.com")
        monkeypatch.setenv("GMAIL_USER", "me@gmail.com")
        monkeypatch.setenv("GMAIL_APP_PASSWORD", "x")
        monkeypatch.setattr(emailer, "_send_gmail", lambda *a: calls.setdefault("gm", True) or True)
        assert emailer.send("s", "<html>", "text") is True
        assert calls == {"gm": True}

    def test_no_provider_is_a_logged_failure(self, monkeypatch):
        for var in ("SENDGRID_API_KEY", "GMAIL_USER", "GMAIL_APP_PASSWORD"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("EMAIL_TO", "me@example.com")
        assert emailer.send("s", "<html>", "text") is False

    def test_missing_recipient_is_a_failure(self, monkeypatch):
        monkeypatch.delenv("EMAIL_TO", raising=False)
        monkeypatch.setenv("SENDGRID_API_KEY", "SG.x")
        assert emailer.send("s", "<html>", "text") is False


class TestDeliver:
    def test_dry_run_writes_preview_and_sends_nothing(self, tmp_path, monkeypatch):
        preview = tmp_path / "preview.html"
        monkeypatch.setattr(emailer, "PREVIEW_FILE", str(preview))
        sent = {}
        monkeypatch.setattr(emailer, "send", lambda *a: sent.setdefault("sent", True))
        result = emailer.deliver(digest(), dry_run=True)
        assert result is True
        assert preview.exists()
        assert sent == {}  # send() was never called
        assert "Acme" in preview.read_text()

    def test_real_run_calls_send(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(emailer, "send",
                            lambda subj, h, t: captured.update(subject=subj) or True)
        assert emailer.deliver(digest()) is True
        assert captured["subject"] == "Trend Tracker August 2-8 2026"
