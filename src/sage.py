"""Sage design tokens, as email-safe constants.

Every hex code, font stack, size, and tracking value the email uses lives here
as a named constant, so a token change is one edit rather than a search across
f-strings in emailer.py. The composed style strings near the bottom exist for
the same reason — emailer.py should never concatenate a colour into markup.

Mapping (PLAN §7): design-system Card.tsx `variant="card"` onto an investment.

    label        VC firm(s)          12px Spectral 300, #827A64, tracking -0.72px
    heading      Company name        20px Rethink Sans 300, #1B2323, tracking -0.4px
    slot         $30M · Series B     bg brand.accent #E8DDA2, text #1B2323
    body         What it does        14px Spectral 300, line-height 1.5, #59554b
    tag          Sector              bg data.paleMustard #D9D059, 8px, #59554b
    action       "Visit site →"      primary #1AAED8
    container    —                   bg layer1 #FFF8F0, border 0.5px #ADABA5, radius 8px

Two honest limitations, both inherent to HTML email rather than shortcuts:

  - font-weight 300 is faithful to Sage but only renders light where Rethink
    Sans / Spectral actually load. Gmail strips webfonts, and Georgia and the
    system sans have no 300 weight, so most readers see 400. The palette
    carries the identity; the weights are best-effort.
  - PAGE_BG is the one value NOT in PLAN §7's table. The Card component sat on
    whatever page hosted it and never defined a page background; a full-width
    email needs one. It's an inferred warm neutral consistent with layer1, not
    a published Sage token — labelled as such so nobody treats it as canonical.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Palette — these are the published Sage tokens
# ---------------------------------------------------------------------------

LAYER_1 = "#FFF8F0"       # card background
BORDER = "#ADABA5"        # card border
TEXT_PRIMARY = "#1B2323"  # headings, accent-slot text
TEXT_MUTED = "#59554b"    # body, tag text
LABEL = "#827A64"         # the small firm label above the heading
ACCENT = "#E8DDA2"        # brand.accent — the amount·stage slot
PALE_MUSTARD = "#D9D059"  # data.paleMustard — the sector chip
PRIMARY = "#1AAED8"       # links / the "Visit site" action

# Inferred, not a published token — see the module docstring.
PAGE_BG = "#F4EFE6"

# A hairline that survives clients which round 0.5px up to 1px.
BORDER_STYLE = f"0.5px solid {BORDER}"

# ---------------------------------------------------------------------------
# Type
# ---------------------------------------------------------------------------

# Rethink Sans and Spectral are declared first and will be used by the rare
# client that has them; everyone else falls through the stack. Georgia is the
# serif nearly every mail client has, which is why it leads the fallbacks.
FONT_SANS = (
    "'Rethink Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', "
    "Roboto, Helvetica, Arial, sans-serif"
)
FONT_SERIF = "Spectral, Georgia, 'Times New Roman', Times, serif"

# Weights. See the docstring — 300 degrades to 400 for most readers.
WEIGHT_LIGHT = "300"
WEIGHT_NORMAL = "400"
WEIGHT_MEDIUM = "500"

# Sizes (px), from the Card mapping.
SIZE_LABEL = "12px"
SIZE_HEADING = "20px"
SIZE_BODY = "14px"
SIZE_SLOT = "14px"
SIZE_TAG = "12px"
SIZE_SMALL = "12px"
SIZE_HERO = "28px"

TRACKING_LABEL = "-0.72px"
TRACKING_HEADING = "-0.4px"
LINE_BODY = "1.5"

# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

CONTAINER_WIDTH = 600  # px — a digest width, not Card.tsx's 248px component grid
RADIUS = "8px"         # ignored by Outlook's Word engine; cards render square there

SPACE_XS = "4px"
SPACE_SM = "8px"
SPACE_MD = "16px"
SPACE_LG = "24px"
SPACE_XL = "32px"

# Colour of a positive vs negative sector delta.
DELTA_UP = "#2E7D5B"
DELTA_DOWN = "#B0453C"
DELTA_NEUTRAL = TEXT_MUTED

# The low-confidence strip on a card whose amount the guard could not
# corroborate. Warm amber rather than an alarming red — it's a "check this",
# not an error.
WARN_BG = "#FBF3D9"
WARN_TEXT = "#7A5C1E"
WARN_BORDER = "#E4C97A"

# ---------------------------------------------------------------------------
# Composed style strings — the only place colours meet type
# ---------------------------------------------------------------------------

CARD_LABEL = (
    f"font-family:{FONT_SERIF};font-size:{SIZE_LABEL};font-weight:{WEIGHT_LIGHT};"
    f"color:{LABEL};letter-spacing:{TRACKING_LABEL};text-transform:uppercase;"
    f"margin:0;padding:0;"
)

CARD_HEADING = (
    f"font-family:{FONT_SANS};font-size:{SIZE_HEADING};font-weight:{WEIGHT_LIGHT};"
    f"color:{TEXT_PRIMARY};letter-spacing:{TRACKING_HEADING};"
    f"margin:0;padding:0;line-height:1.25;"
)

CARD_BODY = (
    f"font-family:{FONT_SERIF};font-size:{SIZE_BODY};font-weight:{WEIGHT_LIGHT};"
    f"color:{TEXT_MUTED};line-height:{LINE_BODY};margin:0;padding:0;"
)

CARD_SLOT = (
    f"font-family:{FONT_SANS};font-size:{SIZE_SLOT};font-weight:{WEIGHT_MEDIUM};"
    f"color:{TEXT_PRIMARY};margin:0;padding:0;white-space:nowrap;"
)

CARD_TAG = (
    f"font-family:{FONT_SANS};font-size:{SIZE_TAG};font-weight:{WEIGHT_NORMAL};"
    f"color:{TEXT_MUTED};margin:0;padding:0;"
)

CARD_ACTION = (
    f"font-family:{FONT_SANS};font-size:{SIZE_BODY};font-weight:{WEIGHT_MEDIUM};"
    f"color:{PRIMARY};text-decoration:none;"
)

BODY_TEXT = (
    f"font-family:{FONT_SERIF};font-size:{SIZE_BODY};font-weight:{WEIGHT_LIGHT};"
    f"color:{TEXT_MUTED};line-height:{LINE_BODY};"
)
