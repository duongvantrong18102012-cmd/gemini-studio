"""Design tokens and the global stylesheet.

RED HACKER terminal identity: black metal, crimson signal, bone-white text.
One palette, one stylesheet. Every screen is a view inside a single
`ContentSwitcher`; nothing remounts when navigating, which is what keeps the
UI from flashing.

The exported colour names are retained from the previous blue theme so every
widget keeps working; only their values changed. New names (`CRIMSON`,
`CRIMSON_DIM`, `METAL`, `BONE`, `PLUM`) describe the identity directly.
"""

from __future__ import annotations

# ── Palette ───────────────────────────────────────────────────────────
BG = "#050406"
BG_DEEP = "#020203"
PANEL = "#0b090b"
PANEL_ALT = "#110d10"
ELEVATED = "#1a1114"
BORDER = "#3a1b22"
BORDER_SOFT = "#241317"

FG = "#f2f2f3"
FG_DIM = "#c4b8bc"
MUTED = "#907f85"
FAINT = "#5c4c51"

CRIMSON = "#ff2e4d"
CRIMSON_DIM = "#8a0f22"
CRIMSON_DEEP = "#4d0813"
METAL = "#9aa0a6"
METAL_DIM = "#5f666d"
BONE = "#f2f2f3"
PLUM = "#b97583"

# Legacy aliases used across the widgets (same names, new identity).
CYAN = CRIMSON
CYAN_DIM = CRIMSON_DIM
VIOLET = PLUM
MAGENTA = "#ff5470"
GREEN = "#8fd6b0"
AMBER = "#e8b87a"
RED = "#ff4d5e"
BLUE = METAL

GRADIENT = (CRIMSON, MAGENTA, PLUM, METAL)

CSS = f"""
/* ══════════════════════════════════════════════════════════════════
   Base
   ══════════════════════════════════════════════════════════════════ */
Screen {{
    layers: base overlay;
    background: {BG};
    color: {FG};
    scrollbar-background: {BG_DEEP};
    scrollbar-color: {BORDER};
    scrollbar-color-hover: {CRIMSON_DIM};
    scrollbar-color-active: {CRIMSON};
    scrollbar-size-vertical: 1;
    scrollbar-size-horizontal: 1;
}}

* {{
    scrollbar-size-vertical: 1;
}}

#body {{
    height: 1fr;
}}

/* ══════════════════════════════════════════════════════════════════
   Header
   ══════════════════════════════════════════════════════════════════ */
#app-header {{
    dock: top;
    height: 4;
    background: {BG_DEEP};
    border-bottom: solid {BORDER};
    padding: 0 1;
}}

#app-header.-compact {{
    height: 3;
}}

#header-inner {{
    height: 3;
}}

#header-inner.-compact {{
    height: 2;
}}

#brand {{
    width: 1fr;
    content-align: left middle;
    padding: 0 1;
}}

#header-right {{
    width: auto;
    height: 3;
    align: right middle;
    content-align: right middle;
}}

#scanline {{
    height: 1;
    background: {BG_DEEP};
    color: {BORDER};
}}

/* ══════════════════════════════════════════════════════════════════
   Sidebar
   ══════════════════════════════════════════════════════════════════ */
#sidebar {{
    width: 30;
    background: {BG_DEEP};
    border-right: solid {BORDER};
    padding: 0;
}}

#sidebar.-compact {{
    width: 24;
}}

#sidebar.-narrow {{
    width: 20;
}}

#sidebar.-hidden {{
    display: none;
}}

#body.-stacked #team-body {{
    layout: vertical;
}}

#body.-stacked #team-left {{
    width: 1fr;
    height: 1fr;
    margin-right: 0;
}}

#body.-stacked #team-right {{
    width: 1fr;
    height: 1fr;
}}

#body.-stacked #cowork-body,
#body.-stacked #computer-body {{
    layout: vertical;
}}

#body.-short #team-summary {{
    max-height: 4;
}}

#body.-short #team-log {{
    height: 6;
}}

#body.-short #cowork-side {{
    height: auto;
    max-height: 10;
}}

#sidebar-scroll {{
    height: 1fr;
    padding: 0;
    scrollbar-size-vertical: 0;
}}

.side-label {{
    color: {FAINT};
    padding: 0 2;
    text-style: bold;
}}

NavItem {{
    height: 2;
    padding: 0 1 0 1;
    color: {FG_DIM};
    background: transparent;
}}

NavItem:hover {{
    color: {FG};
    background: {PANEL};
}}

NavItem.-active {{
    color: {CRIMSON};
    background: {ELEVATED};
    text-style: bold;
}}

#session-card {{
    height: auto;
    margin: 0 1 1 1;
    padding: 0 1;
    background: {PANEL};
    border: round {BORDER};
}}

#session-card-title {{
    color: {PLUM};
    text-style: bold;
    height: 1;
}}

.mini-key {{
    color: {FAINT};
    height: 1;
}}

.mini-val {{
    color: {FG_DIM};
    height: 1;
    text-align: right;
}}

/* ══════════════════════════════════════════════════════════════════
   Main pane / views
   ══════════════════════════════════════════════════════════════════ */
#main-pane {{
    width: 1fr;
    height: 1fr;
    background: {BG};
}}

#views {{
    height: 1fr;
    padding: 0;
}}

.view {{
    height: 1fr;
    padding: 0 1;
}}

.view-head {{
    height: 2;
    padding: 0 1;
    content-align: left middle;
}}

.view-title {{
    color: {FG};
    text-style: bold;
}}

.view-sub {{
    color: {MUTED};
}}

/* ══════════════════════════════════════════════════════════════════
   Chat
   ══════════════════════════════════════════════════════════════════ */
#chat-view {{
    padding: 0;
}}

#chat-head {{
    height: 2;
    padding: 0 2;
    content-align: left middle;
}}

#transcript {{
    height: 1fr;
    padding: 0 2;
    scrollbar-gutter: stable;
}}

UserMessage {{
    width: 100%;
    margin: 1 0 0 0;
    padding: 0 1;
    background: {PANEL_ALT};
    border-left: thick {CRIMSON};
    color: {FG};
    height: auto;
}}

AssistantMessage {{
    width: 100%;
    height: auto;
    margin: 1 0 0 0;
    padding: 0 1;
    border-left: thick {CRIMSON_DIM};
    background: {PANEL};
}}

AssistantMessage Markdown {{
    margin: 0;
    padding: 0 1;
    background: transparent;
    color: {FG};
}}

AssistantMessage .msg-meta {{
    color: {FAINT};
    height: 1;
    padding: 0 1;
}}

AssistantMessage .msg-meta.-copyable {{
    color: {CRIMSON};
}}

AssistantMessage .msg-thought {{
    color: {MUTED};
    background: {BG_DEEP};
    border: round {BORDER_SOFT};
    padding: 0 1;
    margin: 0 1;
    height: auto;
    display: none;
}}

AssistantMessage .msg-thought.-visible {{
    display: block;
}}

ToolCard {{
    height: auto;
    margin: 1 0 0 0;
    padding: 0 1;
    background: {BG_DEEP};
    border: round {BORDER};
}}

ToolCard.-running {{
    border: round {AMBER};
}}

ToolCard.-ok {{
    border: round {GREEN};
}}

ToolCard.-error {{
    border: round {RED};
}}

ToolCard.-wait {{
    border: round {CRIMSON};
}}

Notice {{
    height: auto;
    margin: 1 0 0 0;
    padding: 0 1;
    background: {PANEL};
    border-left: thick {AMBER};
    color: {FG_DIM};
}}

#composer-wrap {{
    height: auto;
    margin: 1 2 0 2;
    padding: 0;
    background: {PANEL};
    border: round {BORDER};
}}

#composer-wrap:focus-within {{
    border: round {CRIMSON};
    background: {PANEL_ALT};
}}

#composer {{
    height: auto;
    min-height: 3;
    max-height: 10;
    background: transparent;
    border: none;
    padding: 0 1;
    color: {FG};
}}

#composer:focus {{
    background: transparent;
    border: none;
}}

#composer .text-area--cursor {{
    background: {CRIMSON};
    color: {BG_DEEP};
}}

#composer .text-area--selection {{
    background: {CRIMSON_DIM} 55%;
}}

#composer .text-area--cursor-line {{
    background: {ELEVATED} 70%;
}}

#composer .text-area--gutter {{
    background: transparent;
    color: {FAINT};
}}

#composer-hints {{
    height: 1;
    padding: 0 1;
    color: {FAINT};
    background: transparent;
}}

#slash-hints {{
    height: auto;
    max-height: 6;
    margin: 0 2;
    padding: 0 1;
    background: {BG_DEEP};
    border: round {BORDER_SOFT};
    display: none;
}}

#slash-hints.-visible {{
    display: block;
}}

/* ══════════════════════════════════════════════════════════════════
   Tables / generic panels
   ══════════════════════════════════════════════════════════════════ */
DataTable {{
    height: 1fr;
    background: transparent;
    border: round {BORDER};
}}

DataTable > .datatable--header {{
    background: {PANEL_ALT};
    color: {CRIMSON};
    text-style: bold;
}}

DataTable > .datatable--cursor {{
    background: {ELEVATED};
    color: {FG};
}}

DataTable > .datatable--hover {{
    background: {PANEL};
}}

DataTable > .datatable--even-row {{
    background: {BG};
}}

.panel {{
    background: {PANEL};
    border: round {BORDER};
    padding: 0 1;
}}

.panel-title {{
    color: {CRIMSON};
    text-style: bold;
    height: 1;
}}

.dim {{
    color: {MUTED};
}}

.mono {{
    color: {FG_DIM};
}}

Input {{
    background: {PANEL};
    border: round {BORDER};
    color: {FG};
    padding: 0 1;
}}

Input:focus {{
    border: round {CRIMSON};
    background: {PANEL_ALT};
}}

Input > .input--placeholder {{
    color: {FAINT};
}}

Select {{
    background: {PANEL};
    border: round {BORDER};
    color: {FG};
}}

Select:focus {{
    border: round {CRIMSON};
}}

SelectOverlay {{
    background: {PANEL_ALT};
    border: round {BORDER};
}}

Button {{
    background: {PANEL_ALT};
    border: round {BORDER};
    color: {FG_DIM};
    min-width: 8;
    height: 3;
    padding: 0 1;
}}

Button:hover {{
    border: round {CRIMSON};
    color: {CRIMSON};
}}

Button.-primary {{
    background: {CRIMSON_DIM};
    color: {BONE};
    border: round {CRIMSON};
    text-style: bold;
}}

#detail {{
    height: auto;
    min-height: 6;
    margin: 1 0 0 0;
    padding: 0 1;
    background: {BG_DEEP};
    border: round {BORDER_SOFT};
}}

.filter-row {{
    height: 3;
    margin: 1 0;
}}

.filter-row Input {{
    width: 1fr;
}}

.filter-row Button {{
    width: auto;
    margin-left: 1;
}}

#skills-detail {{
    height: 14;
    margin-top: 1;
    padding: 0 1;
}}

#skills-detail-body {{
    height: auto;
}}

/* ══════════════════════════════════════════════════════════════════
   Dashboard
   ══════════════════════════════════════════════════════════════════ */
#dash-grid {{
    layout: grid;
    grid-size: 2 2;
    grid-columns: 1fr 1fr;
    grid-rows: 1fr 1fr;
    grid-gutter: 1;
    height: 1fr;
}}

.dash-card {{
    background: {PANEL};
    border: round {BORDER};
    padding: 0 1;
    height: 1fr;
}}

.dash-card .panel-title {{
    color: {PLUM};
}}

.meter-row {{
    height: 3;
}}

.meter-label {{
    width: 7;
    color: {FG_DIM};
}}

.meter-value {{
    width: 22;
    text-align: right;
    color: {FG};
}}

ProgressBar {{
    width: 1fr;
}}

ProgressBar > .bar--bar {{
    color: {CRIMSON};
    background: {BORDER_SOFT};
}}

ProgressBar > .bar--complete {{
    color: {GREEN};
}}

ProgressBar > .bar--indeterminate {{
    color: {PLUM};
    background: {BORDER_SOFT};
}}

Sparkline {{
    height: 1;
    width: 1fr;
}}

/* ══════════════════════════════════════════════════════════════════
   Cowork / Computer
   ══════════════════════════════════════════════════════════════════ */
#cowork-toolbar, #computer-toolbar, #team-toolbar {{
    height: 3;
    margin: 1 0;
}}

#cowork-toolbar Input, #computer-toolbar Input, #team-toolbar Input {{
    width: 1fr;
}}

#cowork-toolbar Button, #computer-toolbar Button, #team-toolbar Button {{
    width: auto;
    margin-left: 1;
}}

#cowork-body, #computer-body {{
    height: 1fr;
}}

#cowork-side {{
    width: 40;
    height: auto;
    margin-right: 1;
}}

#cowork-summary {{
    height: auto;
}}

#cowork-steps {{
    width: 1fr;
    height: 1fr;
}}

#cowork-stream {{
    height: 5;
    margin-top: 1;
    background: {BG_DEEP};
    border: round {BORDER_SOFT};
}}

#cowork-log {{
    height: 10;
    background: {BG_DEEP};
    border: round {BORDER_SOFT};
}}

#computer-state {{
    height: auto;
    max-height: 13;
    margin-bottom: 1;
}}

#computer-windows {{
    width: 1fr;
    height: 1fr;
    margin-right: 1;
}}

#computer-log {{
    width: 56;
    height: 1fr;
    background: {BG_DEEP};
    border: round {BORDER_SOFT};
}}

/* ══════════════════════════════════════════════════════════════════
   Agent Command Center (team)
   ══════════════════════════════════════════════════════════════════ */
#team-body {{
    height: 1fr;
}}

#team-left {{
    width: 2fr;
    height: 1fr;
    margin-right: 1;
}}

#team-right {{
    width: 1fr;
    height: 1fr;
}}

#team-summary {{
    height: auto;
    max-height: 7;
    margin-bottom: 1;
}}

#team-progress {{
    height: 1;
    margin: 0 0 1 0;
}}

#team-tasks {{
    height: 1fr;
}}

#team-agents {{
    height: 1fr;
}}

#team-log {{
    height: 12;
    margin-top: 1;
    background: {BG_DEEP};
    border: round {BORDER_SOFT};
}}

/* ══════════════════════════════════════════════════════════════════
   Status bar
   ══════════════════════════════════════════════════════════════════ */
#status-bar {{
    dock: bottom;
    height: 1;
    background: {BG_DEEP};
    color: {MUTED};
    padding: 0 1;
}}

/* ══════════════════════════════════════════════════════════════════
   Overlays (modal screens: palette, help, approval, splash)
   ══════════════════════════════════════════════════════════════════ */
PanelScreen, PaletteScreen, HelpScreen, ApprovalScreen, SplashScreen {{
    align: center middle;
    background: {BG_DEEP} 78%;
}}

.overlay {{
    width: 84;
    max-width: 96%;
    height: auto;
    max-height: 84%;
    background: {PANEL};
    border: round {CRIMSON};
    padding: 1 2;
}}

.overlay-title {{
    color: {CRIMSON};
    text-style: bold;
    height: 1;
}}

.overlay-sub {{
    color: {MUTED};
    height: 1;
    margin-bottom: 1;
}}

#palette-input {{
    margin-bottom: 1;
}}

#palette-list {{
    height: auto;
    max-height: 18;
    background: transparent;
    border: none;
}}

#palette-list > .option-list--option {{
    padding: 0 1;
}}

#palette-list > .option-list--option-highlighted {{
    background: {ELEVATED};
    color: {CRIMSON};
}}

#help-scroll {{
    height: auto;
    max-height: 34;
    scrollbar-size-vertical: 1;
}}

#help-body {{
    height: auto;
}}

#approval-body {{
    height: auto;
    max-height: 24;
    padding: 0 1;
}}

#approval-actions {{
    height: 3;
    align: right middle;
    margin-top: 1;
}}

#toast-host {{
    layer: overlay;
    dock: bottom;
    width: 100%;
    height: auto;
    align: right bottom;
    padding: 0 2 1 0;
    background: transparent;
}}

Toast {{
    width: 52;
    height: auto;
    margin: 1 0 0 0;
    padding: 0 1;
    background: {PANEL_ALT};
    border: round {BORDER};
    color: {FG};
}}

Toast.-ok {{ border: round {GREEN}; }}
Toast.-warn {{ border: round {AMBER}; }}
Toast.-error {{ border: round {RED}; }}
Toast.-info {{ border: round {CRIMSON}; }}

/* ══════════════════════════════════════════════════════════════════
   Splash
   ══════════════════════════════════════════════════════════════════ */
#splash {{
    width: 90%;
    max-width: 80;
    height: auto;
    max-height: 90%;
    padding: 1 3;
    background: {BG_DEEP};
    border: double {CRIMSON};
    align: center middle;
}}

#splash.-tight {{
    padding: 0 2;
}}

#splash.-compact {{
    width: 90%;
    max-width: 64;
    padding: 0 2;
    border: round {CRIMSON};
}}

#splash.-minimal {{
    width: 90%;
    max-width: 54;
    padding: 0 1;
    border: round {CRIMSON_DIM};
}}

#splash-art {{
    height: auto;
    content-align: center middle;
}}

#splash-body {{
    height: auto;
    padding: 1 0 0 0;
    color: {MUTED};
}}

#splash.-compact #splash-body,
#splash.-minimal #splash-body {{
    padding: 0;
}}

/* ══════════════════════════════════════════════════════════════════
   Markdown inside transcript
   ══════════════════════════════════════════════════════════════════ */
Markdown H1, Markdown H2, Markdown H3 {{
    color: {CRIMSON};
    background: transparent;
    margin: 0;
    padding: 0;
}}

Markdown H4, Markdown H5, Markdown H6 {{
    color: {PLUM};
}}

Markdown BlockQuote {{
    border-left: thick {PLUM};
    background: transparent;
}}

Markdown Fence {{
    background: {BG_DEEP};
    border: round {BORDER_SOFT};
    margin: 0;
}}

Markdown InlineCode {{
    background: {ELEVATED};
    color: {CRIMSON};
}}

Markdown BulletList, Markdown OrderedList {{
    margin: 0;
}}

Markdown Table {{
    border: round {BORDER_SOFT};
}}

Markdown Link {{
    color: {METAL};
    text-style: underline;
}}

/* ══════════════════════════════════════════════════════════════════
   Custom code fence (widgets/codeblock.py)
   ══════════════════════════════════════════════════════════════════ */
StudioMarkdown {{
    height: auto;
}}

StudioFence {{
    height: auto;
    max-height: 26;
    overflow: auto auto;
    background: {BG_DEEP};
    border: round {BORDER_SOFT};
    margin: 0 0 1 0;
    padding: 0;
    width: 1fr;
}}

StudioFence:focus {{
    border: round {CRIMSON};
}}

StudioFence .fence-head {{
    height: 1;
    background: {PANEL_ALT};
    color: {FAINT};
    padding: 0 1;
}}

StudioFence .fence-lang {{
    width: 1fr;
    color: {CRIMSON};
    text-style: bold;
}}

StudioFence .fence-meta {{
    width: auto;
    color: {FAINT};
    padding: 0 1;
}}

StudioFence .fence-copy {{
    width: 8;
    color: {METAL};
    text-align: right;
    text-style: bold;
}}

StudioFence .fence-copy:hover {{
    color: {CRIMSON};
    background: {ELEVATED};
}}

StudioFence #code-content {{
    padding: 0 1;
    color: {FG};
}}

StudioFence .fence-code-row {{
    height: auto;
    width: 1fr;
}}

StudioFence .fence-gutter {{
    width: auto;
    height: auto;
    padding: 0 1 0 1;
    color: {CRIMSON_DEEP};
    background: {PANEL};
}}

Footer {{
    background: {BG_DEEP};
    color: {MUTED};
}}
"""
