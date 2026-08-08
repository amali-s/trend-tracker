"""Render the weekly digest as email-safe HTML, and send it.

Two jobs, kept apart: the `render_*` functions turn a `WeeklyDigest` into HTML
and a plain-text alternative, and `send()` delivers it via whichever provider
the environment is configured for.

Five constraints from PLAN §7 shape every line of the HTML, because a direct
port of the React Card would break in real mail clients:

  1. No Tailwind — there's no build step, so every class becomes an inline
     `style` attribute.
  2. No flexbox — Outlook ignores it; layout is nested `<table>` throughout.
  3. No webfonts — Gmail strips them; sage.py declares fallback stacks and
     accepts that most readers see Georgia + a system sans.
  4. 600px container, not Card.tsx's 248px component-grid width.
  5. No gradients — the profile variant's radial gradient won't render; this
     uses the card variant's solid #FFF8F0.

And a sixth, from the same section: Apple Mail's dark-mode inversion wrecks a
warm cream palette, so every text node carries an explicit `color` and the
document opts out of scheme adaptation.

Degradation knowingly accepted: Outlook's Word rendering engine ignores
`border-radius`, so cards are square there. Rounding them needs VML, which
isn't worth it for a digest.
"""

from __future__ import annotations

import html
import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional
from urllib.parse import urlparse

from . import sage
from .models import Investment
from .trends import WeeklyDigest, format_usd

logger = logging.getLogger(__name__)

PREVIEW_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "preview.html"
)


# ---------------------------------------------------------------------------
# Escaping helpers — everything here is scraped or model-derived, so nothing
# reaches the HTML without being escaped first.
# ---------------------------------------------------------------------------

def _esc(text: Optional[str]) -> str:
    return html.escape(text or "", quote=True)


def _safe_url(url: Optional[str]) -> Optional[str]:
    """Return the URL only if it's a plain http(s) link, else None.

    company_url comes out of a marketing page or the model, so a
    `javascript:` or `data:` scheme must never reach an href. Anything that
    isn't http(s) is dropped and the caller renders plain text instead.
    """
    if not url:
        return None
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return url.strip()
    return None


def _table(inner: str, style: str = "", attrs: str = "") -> str:
    """A presentational table — role set so screen readers skip the layout."""
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" '
        f'border="0" {attrs} style="{style}">{inner}</table>'
    )


# ---------------------------------------------------------------------------
# Card
# ---------------------------------------------------------------------------

def _amount_slot(inv: Investment) -> str:
    """The $30M · Series B accent block.

    A single-cell table, not a styled <span>: Outlook drops padding on inline
    elements, which would jam the text against the accent edge.
    """
    amount = _esc(inv.funding_amount_raw or "Undisclosed")
    stage = _esc(inv.round_stage) if inv.round_stage and inv.round_stage != "Unknown" else ""
    text = f"{amount} &middot; {stage}" if stage else amount
    cell = (
        f'<td style="background-color:{sage.ACCENT};padding:{sage.SPACE_XS} '
        f'{sage.SPACE_SM};border-radius:4px;{sage.CARD_SLOT}">{text}</td>'
    )
    return _table(f"<tr>{cell}</tr>")


def _sector_chip(inv: Investment) -> str:
    """The sector tag chip — also a single-cell table, same reasoning."""
    cell = (
        f'<td style="background-color:{sage.PALE_MUSTARD};padding:2px '
        f'{sage.SPACE_SM};border-radius:4px;{sage.CARD_TAG}">{_esc(inv.sector)}</td>'
    )
    return _table(f"<tr>{cell}</tr>")


def _confidence_strip(inv: Investment) -> str:
    """A visible marker on a low-confidence extraction.

    PLAN §5 requires surfacing these rather than silently trusting them —
    without this strip the valuation guard's work is invisible in the email.
    """
    if inv.confidence != "low":
        return ""
    note = _esc(inv.notes) or "Amount could not be automatically verified — check the source."
    return (
        f'<tr><td style="padding-top:{sage.SPACE_SM};">'
        f'<div style="background-color:{sage.WARN_BG};border:1px solid '
        f'{sage.WARN_BORDER};border-radius:4px;padding:{sage.SPACE_SM};'
        f'font-family:{sage.FONT_SANS};font-size:{sage.SIZE_SMALL};'
        f'color:{sage.WARN_TEXT};line-height:1.4;">&#9888; {note}</div>'
        f"</td></tr>"
    )


def _card(inv: Investment) -> str:
    firms = _esc(" &middot; ".join(inv.vc_firms) if inv.vc_firms else "")
    heading = _esc(inv.company_name)
    body = _esc(inv.company_description)

    url = _safe_url(inv.company_url)
    action = (
        f'<tr><td style="padding-top:{sage.SPACE_MD};">'
        f'<a href="{_esc(url)}" style="{sage.CARD_ACTION}">Visit site &rarr;</a>'
        f"</td></tr>"
        if url else ""
    )

    co = ""
    if inv.co_investors:
        co_text = _esc(", ".join(inv.co_investors))
        co = (
            f'<tr><td style="padding-top:{sage.SPACE_SM};'
            f'font-family:{sage.FONT_SANS};font-size:{sage.SIZE_SMALL};'
            f'color:{sage.LABEL};">with {co_text}</td></tr>'
        )

    rows = (
        f'<tr><td style="{sage.CARD_LABEL}padding-bottom:{sage.SPACE_XS};">{firms}</td></tr>'
        f'<tr><td style="{sage.CARD_HEADING}padding-bottom:{sage.SPACE_SM};">{heading}</td></tr>'
        f'<tr><td style="padding-bottom:{sage.SPACE_MD};">{_amount_slot(inv)}</td></tr>'
        f'<tr><td style="{sage.CARD_BODY}padding-bottom:{sage.SPACE_MD};">{body}</td></tr>'
        f"<tr><td>{_sector_chip(inv)}</td></tr>"
        f"{co}"
        f"{_confidence_strip(inv)}"
        f"{action}"
    )
    inner = _table(rows, style="width:100%;")

    return (
        f'<tr><td style="padding-bottom:{sage.SPACE_MD};">'
        f'<div style="background-color:{sage.LAYER_1};border:{sage.BORDER_STYLE};'
        f'border-radius:{sage.RADIUS};padding:{sage.SPACE_LG};">{inner}</div>'
        f"</td></tr>"
    )


# ---------------------------------------------------------------------------
# Trend blocks
# ---------------------------------------------------------------------------

def _delta_cell(row) -> str:
    """One sector's delta, in one of three render states.

    A percentage when there's a real prior figure to compare against; a bare
    dollar delta marked "new" when the sector has a baseline of $0 (percentage
    change from zero is undefined); and an em dash when there's no baseline at
    all. Collapsing these to one state was the trap flagged at the end of
    Phase 5 — a `None` percentage rendered naively reads as a real 0%.
    """
    if row.delta_usd is None:
        return f'<span style="color:{sage.DELTA_NEUTRAL};">&mdash;</span>'

    up = row.delta_usd > 0
    colour = sage.DELTA_UP if up else sage.DELTA_DOWN
    arrow = "&uarr;" if up else "&darr;"

    if row.delta_pct is None:
        # Baseline was $0 for this sector — report the dollar move as "new".
        label = f"{format_usd(abs(row.delta_usd))} new"
    else:
        label = f"{abs(row.delta_pct):.0f}%"

    return f'<span style="color:{colour};">{arrow} {label}</span>'


def _sector_table(digest: WeeklyDigest) -> str:
    if not digest.sectors:
        return ""

    head_cell = (
        f"font-family:{sage.FONT_SANS};font-size:{sage.SIZE_SMALL};"
        f"font-weight:{sage.WEIGHT_MEDIUM};color:{sage.LABEL};"
        f"text-transform:uppercase;letter-spacing:0.5px;"
        f"padding:{sage.SPACE_SM} {sage.SPACE_SM};"
        f"border-bottom:{sage.BORDER_STYLE};text-align:"
    )
    header = (
        f"<tr>"
        f'<td style="{head_cell}left;">Sector</td>'
        f'<td style="{head_cell}right;">Deals</td>'
        f'<td style="{head_cell}right;">Capital</td>'
        f'<td style="{head_cell}right;">vs 4-wk</td>'
        f"</tr>"
    )

    body_cell = (
        f"font-family:{sage.FONT_SANS};font-size:{sage.SIZE_BODY};"
        f"color:{sage.TEXT_PRIMARY};padding:{sage.SPACE_SM};"
        f"border-bottom:1px solid #EDE6DA;text-align:"
    )
    rows = []
    for r in digest.sectors:
        cap = format_usd(r.total_usd) if r.total_usd else "&mdash;"
        undis = (
            f' <span style="color:{sage.LABEL};font-size:{sage.SIZE_SMALL};">'
            f"(+{r.undisclosed} undisc.)</span>"
            if r.undisclosed else ""
        )
        delta = _delta_cell(r) if digest.has_baseline else (
            f'<span style="color:{sage.DELTA_NEUTRAL};">&mdash;</span>'
        )
        rows.append(
            f"<tr>"
            f'<td style="{body_cell}left;">{_esc(r.sector)}{undis}</td>'
            f'<td style="{body_cell}right;">{r.deals}</td>'
            f'<td style="{body_cell}right;">{cap}</td>'
            f'<td style="{body_cell}right;font-size:{sage.SIZE_SMALL};">{delta}</td>'
            f"</tr>"
        )

    return _section(
        "Where the money went",
        _table(header + "".join(rows), style="width:100%;border-collapse:collapse;"),
    )


def _stage_mix(digest: WeeklyDigest) -> str:
    if not digest.stages:
        return ""
    cell = f"font-family:{sage.FONT_SANS};font-size:{sage.SIZE_BODY};color:{sage.TEXT_MUTED};padding:{sage.SPACE_XS} 0;"
    rows = "".join(
        f"<tr>"
        f'<td style="{cell}">{_esc(s.stage)}</td>'
        f'<td style="{cell}text-align:right;color:{sage.TEXT_PRIMARY};">'
        f"{s.deals} &middot; {format_usd(s.total_usd) if s.total_usd else '&mdash;'} "
        f'<span style="color:{sage.LABEL};">({s.share_pct:.0f}%)</span></td>'
        f"</tr>"
        for s in digest.stages
    )
    return _section("Stage mix", _table(rows, style="width:100%;"))


def _movers(digest: WeeklyDigest) -> str:
    if not digest.has_baseline or (not digest.movers_up and not digest.movers_down):
        return ""

    def line(m, up):
        colour = sage.DELTA_UP if up else sage.DELTA_DOWN
        arrow = "&uarr;" if up else "&darr;"
        pct = f" ({abs(m.delta_pct):.0f}%)" if m.delta_pct is not None else ""
        return (
            f'<div style="font-family:{sage.FONT_SANS};font-size:{sage.SIZE_BODY};'
            f'color:{sage.TEXT_MUTED};padding:{sage.SPACE_XS} 0;">'
            f'<span style="color:{colour};">{arrow} {_esc(m.sector)}</span> '
            f"{format_usd(abs(m.delta_usd))}{pct} vs 4-wk avg</div>"
        )

    parts = [line(m, True) for m in digest.movers_up]
    parts += [line(m, False) for m in digest.movers_down]
    return _section("Movers", "".join(parts))


def _source_footer(digest: WeeklyDigest) -> str:
    """Per-source table. Unhealthy sources are called out so a silently broken
    scraper is visible here rather than just vanishing (PLAN §11)."""
    if not digest.source_summaries:
        return ""

    cell = f"font-family:{sage.FONT_SANS};font-size:{sage.SIZE_SMALL};padding:2px {sage.SPACE_SM};"
    rows = []
    for s in sorted(digest.source_summaries, key=lambda x: x.source.lower()):
        if s.error:
            status, colour = f"error: {_esc(s.error)}", sage.DELTA_DOWN
        elif s.posts_found == 0:
            status, colour = "0 posts", sage.DELTA_DOWN
        else:
            status, colour = f"{s.posts_found} posts, {s.new_posts} new", sage.LABEL
        rows.append(
            f"<tr>"
            f'<td style="{cell}color:{sage.TEXT_MUTED};">{_esc(s.source)}</td>'
            f'<td style="{cell}color:{colour};text-align:right;">{status}</td>'
            f"</tr>"
        )

    return _section(
        "Sources",
        _table("".join(rows), style="width:100%;"),
        muted=True,
    )


# ---------------------------------------------------------------------------
# Page scaffolding
# ---------------------------------------------------------------------------

def _section(title: str, inner: str, muted: bool = False) -> str:
    colour = sage.LABEL if muted else sage.TEXT_PRIMARY
    heading = (
        f'<div style="font-family:{sage.FONT_SANS};font-size:{sage.SIZE_BODY};'
        f"font-weight:{sage.WEIGHT_MEDIUM};color:{colour};text-transform:uppercase;"
        f'letter-spacing:0.5px;padding-bottom:{sage.SPACE_SM};">{title}</div>'
    )
    return (
        f'<tr><td style="padding:{sage.SPACE_MD} 0;">{heading}{inner}</td></tr>'
    )


def _scan_summary(digest: WeeklyDigest) -> str:
    """A one-line 'what was scanned' phrase for the empty state.

    An empty week caused by broken scrapers means something different from a
    genuinely quiet one, so the count distinguishes 'all clear' from 'some
    sources could not be reached'.
    """
    total = len(digest.source_summaries)
    healthy = total - len(digest.unhealthy_sources)
    if total == 0:
        return "The scan completed"
    noun = "source" if total == 1 else "sources"
    if healthy == total:
        return f"All {total} {noun} scanned cleanly"
    return f"{healthy} of {total} {noun} scanned"


def _empty_state(digest: WeeklyDigest) -> str:
    """The empty-week hero: a calm all-clear, not an error.

    A week with no new rounds is a normal outcome. The email is still sent —
    that is the whole point, since silence would mean the pipeline broke — so
    this block reads as proof-of-life: an accent badge, the plain statement
    that nothing new landed, and how many sources were scanned. If any source
    could not be reached, a warning strip says so, because that changes what an
    "empty" week means.
    """
    hero_style = (
        f"font-family:{sage.FONT_SANS};font-size:{sage.SIZE_HERO};"
        f"font-weight:{sage.WEIGHT_LIGHT};color:{sage.TEXT_PRIMARY};"
        f"letter-spacing:{sage.TRACKING_HEADING};line-height:1.25;"
        f"margin:0;text-align:center;"
    )
    sub_style = (
        f"font-family:{sage.FONT_SERIF};font-size:{sage.SIZE_BODY};"
        f"font-weight:{sage.WEIGHT_LIGHT};color:{sage.TEXT_MUTED};"
        f"line-height:{sage.LINE_BODY};margin:0;text-align:center;"
    )

    # The accent badge — a single-cell table so Outlook keeps the fill and
    # padding (it drops both on inline elements). border-radius is squared by
    # Outlook's Word engine, same accepted degradation as the cards.
    badge = _table(
        f'<tr><td align="center" valign="middle" width="56" height="56" '
        f'style="width:56px;height:56px;background-color:{sage.ACCENT};'
        f'border-radius:28px;font-family:{sage.FONT_SANS};font-size:26px;'
        f'line-height:56px;color:{sage.TEXT_PRIMARY};text-align:center;">'
        f"&#10003;</td></tr>",
        attrs='align="center"',
    )

    warn = ""
    unhealthy = len(digest.unhealthy_sources)
    if unhealthy:
        noun = "source" if unhealthy == 1 else "sources"
        warn = (
            f'<tr><td style="padding-top:{sage.SPACE_MD};">'
            f'<div style="background-color:{sage.WARN_BG};border:1px solid '
            f"{sage.WARN_BORDER};border-radius:4px;padding:{sage.SPACE_SM} "
            f"{sage.SPACE_MD};font-family:{sage.FONT_SANS};"
            f"font-size:{sage.SIZE_SMALL};color:{sage.WARN_TEXT};"
            f'line-height:1.4;text-align:center;">&#9888; {unhealthy} {noun} '
            f"could not be scanned cleanly &mdash; a quiet week may be partly "
            f"that. See Sources below.</div></td></tr>"
        )

    inner = _table(
        f'<tr><td align="center" style="padding-bottom:{sage.SPACE_MD};">{badge}</td></tr>'
        f'<tr><td style="{hero_style}padding-bottom:{sage.SPACE_SM};">'
        f"No new investments this week</td></tr>"
        f'<tr><td style="{sub_style}">{_scan_summary(digest)} &mdash; nothing '
        f"new cleared the classifier.</td></tr>"
        f"{warn}",
        style="width:100%;",
    )

    return (
        f'<tr><td style="padding:{sage.SPACE_XL} 0;">'
        f'<div style="background-color:{sage.LAYER_1};border:{sage.BORDER_STYLE};'
        f"border-radius:{sage.RADIUS};padding:{sage.SPACE_XL} {sage.SPACE_LG};"
        f'text-align:center;">{inner}</div></td></tr>'
    )


def _headline(digest: WeeklyDigest) -> str:
    # The empty week is rendered by _empty_state; render_digest branches before
    # this is ever reached with no investments.
    h = digest.headline
    undis = f" &middot; {h.undisclosed_count} undisclosed" if h.undisclosed_count else ""
    hero = f"{format_usd(h.total_usd)} across {h.deal_count} deals"
    sub = f"{h.firms_active} firms active{undis}"

    hero_style = (
        f"font-family:{sage.FONT_SANS};font-size:{sage.SIZE_HERO};"
        f"font-weight:{sage.WEIGHT_LIGHT};color:{sage.TEXT_PRIMARY};"
        f"letter-spacing:{sage.TRACKING_HEADING};line-height:1.2;margin:0;"
    )
    sub_style = (
        f"font-family:{sage.FONT_SERIF};font-size:{sage.SIZE_BODY};"
        f"color:{sage.TEXT_MUTED};padding-top:{sage.SPACE_SM};margin:0;"
    )
    active = ""
    if digest.most_active_firm:
        f = digest.most_active_firm
        active = (
            f'<div style="{sub_style}">Most active: {_esc(f.firm)} '
            f"({f.deals} {'deal' if f.deals == 1 else 'deals'})</div>"
        )

    return (
        f'<tr><td style="padding-bottom:{sage.SPACE_LG};">'
        f'<div style="{hero_style}">{hero}</div>'
        f'<div style="{sub_style}">{sub}</div>{active}'
        f"</td></tr>"
    )


def render_digest(digest: WeeklyDigest) -> str:
    """The full HTML email."""
    if digest.is_empty:
        # A quiet week: the all-clear hero, then the source table (which is the
        # proof of what ran) and the footer. The sector/stage/mover blocks are
        # all "where this week's money went" — nothing went anywhere, so they
        # are dropped rather than shown as a page of zeros.
        body = _empty_state(digest) + _source_footer(digest) + _footer_note()
        preheader = "No new investments this week"
    else:
        cards = "".join(_card(i) for i in _sorted_investments(digest.investments))
        cards_block = (
            _section("New investments", _table(cards, style="width:100%;"))
            if cards else ""
        )
        body = (
            _headline(digest)
            + _sector_table(digest)
            + _stage_mix(digest)
            + _movers(digest)
            + cards_block
            + _source_footer(digest)
            + _footer_note()
        )
        preheader = (
            f"{format_usd(digest.headline.total_usd)} across "
            f"{digest.headline.deal_count} deals"
        )

    preheader = _esc(preheader)

    inner = _table(
        body,
        style=f"width:{sage.CONTAINER_WIDTH}px;max-width:100%;",
        attrs=f'width="{sage.CONTAINER_WIDTH}"',
    )

    return (
        "<!DOCTYPE html>"
        '<html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light only">'
        '<meta name="supported-color-schemes" content="light only">'
        f"<title>{_esc(digest.subject)}</title>"
        "</head>"
        f'<body style="margin:0;padding:0;background-color:{sage.PAGE_BG};">'
        # Hidden preheader — controls the inbox preview line.
        f'<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{preheader}</div>'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="100%" style="background-color:{sage.PAGE_BG};">'
        f'<tr><td align="center" style="padding:{sage.SPACE_XL} {sage.SPACE_MD};">'
        f"{inner}"
        f"</td></tr></table>"
        "</body></html>"
    )


def _footer_note() -> str:
    return (
        f'<tr><td style="padding-top:{sage.SPACE_LG};border-top:{sage.BORDER_STYLE};">'
        f'<div style="font-family:{sage.FONT_SANS};font-size:{sage.SIZE_SMALL};'
        f'color:{sage.LABEL};line-height:1.5;">Trend Tracker scans 14 VC blogs '
        f"weekly. A successful run always sends this email, even an empty week &mdash; "
        f"so silence means something broke.</div></td></tr>"
    )


def _sorted_investments(investments: list[Investment]) -> list[Investment]:
    """Largest disclosed round first, undisclosed last."""
    return sorted(
        investments,
        key=lambda i: (i.funding_amount_usd is None, -(i.funding_amount_usd or 0)),
    )


# ---------------------------------------------------------------------------
# Plain-text alternative
# ---------------------------------------------------------------------------

def render_text(digest: WeeklyDigest) -> str:
    lines = [digest.subject, "=" * len(digest.subject), ""]
    h = digest.headline
    if digest.is_empty:
        lines += [
            "No new investments this week.",
            f"{_scan_summary(digest)} — nothing new cleared the classifier.",
            "",
        ]
    else:
        lines.append(f"{format_usd(h.total_usd)} across {h.deal_count} deals")
        extra = f", {h.undisclosed_count} undisclosed" if h.undisclosed_count else ""
        lines.append(f"{h.firms_active} firms active{extra}")
        if digest.most_active_firm:
            lines.append(f"Most active: {digest.most_active_firm.firm} "
                         f"({digest.most_active_firm.deals} deals)")
        lines.append("")

    if digest.sectors:
        lines += ["WHERE THE MONEY WENT", "-" * 20]
        for r in digest.sectors:
            cap = format_usd(r.total_usd) if r.total_usd else "-"
            lines.append(f"  {r.sector}: {r.deals} deals, {cap}")
        lines.append("")

    for inv in _sorted_investments(digest.investments):
        firms = " / ".join(inv.vc_firms)
        stage = f" {inv.round_stage}" if inv.round_stage != "Unknown" else ""
        lines.append(f"* {inv.company_name} — {inv.funding_amount_raw}{stage} [{inv.sector}]")
        lines.append(f"  {firms}")
        if inv.company_description:
            lines.append(f"  {inv.company_description}")
        if inv.confidence == "low" and inv.notes:
            lines.append(f"  [!] {inv.notes}")
        url = _safe_url(inv.company_url)
        if url:
            lines.append(f"  {url}")
        lines.append("")

    if digest.source_summaries:
        lines += ["SOURCES", "-" * 20]
        for s in sorted(digest.source_summaries, key=lambda x: x.source.lower()):
            if s.error:
                status = f"error: {s.error}"
            elif s.posts_found == 0:
                status = "0 posts"
            else:
                status = f"{s.posts_found} posts, {s.new_posts} new"
            lines.append(f"  {s.source}: {status}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def write_preview(html_body: str, path: Optional[str] = None) -> str:
    # Resolve the path at call time, not at definition time, so a test (or a
    # caller) that overrides PREVIEW_FILE is actually honoured.
    path = path or PREVIEW_FILE
    with open(path, "w") as f:
        f.write(html_body)
    logger.info(f"Wrote preview to {path}")
    return path


def send(subject: str, html_body: str, text_body: str) -> bool:
    """Deliver via SendGrid if configured, else Gmail SMTP, else fail loudly.

    Recipient is EMAIL_TO and only EMAIL_TO — nothing is hardcoded. Returns
    True on a successful send.
    """
    to_addr = os.environ.get("EMAIL_TO")
    if not to_addr:
        logger.error("EMAIL_TO is not set; cannot send.")
        return False

    if os.environ.get("SENDGRID_API_KEY"):
        return _send_sendgrid(subject, html_body, text_body, to_addr)
    if os.environ.get("GMAIL_USER") and os.environ.get("GMAIL_APP_PASSWORD"):
        return _send_gmail(subject, html_body, text_body, to_addr)

    logger.error(
        "No email provider configured. Set SENDGRID_API_KEY, or "
        "GMAIL_USER + GMAIL_APP_PASSWORD."
    )
    return False


def _send_gmail(subject: str, html_body: str, text_body: str, to_addr: str) -> bool:
    user = os.environ["GMAIL_USER"]
    password = os.environ["GMAIL_APP_PASSWORD"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
            server.starttls()
            server.login(user, password)
            server.send_message(msg)
        logger.info(f"Sent digest to {to_addr} via Gmail SMTP.")
        return True
    except (smtplib.SMTPException, OSError) as e:
        logger.error(f"Gmail send failed: {e}")
        return False


def _send_sendgrid(subject: str, html_body: str, text_body: str, to_addr: str) -> bool:
    # Imported inside the branch: sendgrid is in requirements.txt but need not
    # be installed for the Gmail path or the tests to work.
    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Content, Mail
    except ImportError:
        logger.error("SENDGRID_API_KEY is set but the sendgrid package isn't installed.")
        return False

    from_addr = os.environ.get("EMAIL_FROM")
    if not from_addr:
        logger.error("SendGrid needs EMAIL_FROM (a verified sender).")
        return False

    message = Mail(from_email=from_addr, to_emails=to_addr, subject=subject)
    message.add_content(Content("text/plain", text_body))
    message.add_content(Content("text/html", html_body))

    try:
        client = SendGridAPIClient(os.environ["SENDGRID_API_KEY"])
        response = client.send(message)
        if 200 <= response.status_code < 300:
            logger.info(f"Sent digest to {to_addr} via SendGrid ({response.status_code}).")
            return True
        logger.error(f"SendGrid returned {response.status_code}.")
        return False
    except Exception as e:  # noqa: BLE001 — sendgrid raises its own exception types
        logger.error(f"SendGrid send failed: {e}")
        return False


def deliver(digest: WeeklyDigest, dry_run: bool = False) -> bool:
    """Render and send (or, in dry-run, render and write preview.html)."""
    html_body = render_digest(digest)
    text_body = render_text(digest)

    if dry_run:
        write_preview(html_body)
        print(text_body)
        return True

    return send(digest.subject, html_body, text_body)
