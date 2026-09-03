"""Shared presentation constants for the Binance Orderbook dashboard."""

# ── Accent palette ───────────────────────────────────────────────────────────
C_BG        = "#06080d"
C_CARD      = "#0c0f16"
C_BORDER    = "#151b27"
C_GREEN     = "#00e6a7"
C_RED       = "#ff5c5c"
C_AMBER     = "#ffb400"
C_BLUE      = "#3b8bff"
C_PURPLE    = "#a78bfa"
C_TEXT      = "#e2e8f0"
C_MUTED     = "#64748b"

# ── Dashboard CSS ─────────────────────────────────────────────────────────────
DASHBOARD_CSS = """
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
    @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&display=swap');

    /* ── Base ─────────────────────────────────────────────────────────────── */
    .stApp {
        font-family: 'Inter', -apple-system, sans-serif !important;
        background: linear-gradient(180deg, #06080d 0%, #080b12 100%);
        color: #e2e8f0;
    }
    .stApp div, .stApp p, .stApp label,
    .stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp td, .stApp th {
        font-family: 'Inter', -apple-system, sans-serif !important;
    }
    .stApp code, .stApp pre, .stApp .stCode {
        font-family: 'JetBrains Mono', monospace !important;
    }
    .stApp input, .stApp button {
        font-family: 'Inter', -apple-system, sans-serif !important;
    }
    section[data-testid="stSidebar"] {
        background: linear-gradient(180deg, #080b12 0%, #0a0d14 100%);
        border-right: 1px solid #151b27;
    }

    /* ── Inputs ───────────────────────────────────────────────────────────── */
    .stApp input, .stApp textarea {
        background: #0f1219 !important; color: #e2e8f0 !important;
        border: 1px solid #1e2536 !important; border-radius: 8px !important;
        font-size: 0.92rem !important;
    }
    .stApp input:focus, .stApp textarea:focus {
        border-color: #3b8bff !important;
        box-shadow: 0 0 0 2px rgba(59,139,255,0.15) !important;
    }
    .stApp label, .stApp [data-testid="stWidgetLabel"] p {
        color: #7cb4ff !important; font-weight: 600 !important;
        font-size: 0.85rem !important; text-transform: uppercase !important;
        letter-spacing: 0.5px !important;
    }
    .stApp [data-baseweb="select"] > div {
        background: #0f1219 !important; color: #e2e8f0 !important;
        border-color: #1e2536 !important; border-radius: 8px !important;
    }
    .stApp [data-baseweb="popover"] { background: #111827 !important; border: 1px solid #1e2536 !important; border-radius: 8px !important; }
    .stApp [data-baseweb="popover"] li { background: #111827 !important; color: #e2e8f0 !important; }
    .stApp [data-baseweb="popover"] li:hover { background: #1e2536 !important; }

    /* ── Tabs ──────────────────────────────────────────────────────────────── */
    .stTabs [data-baseweb="tab-list"] {
        gap: 0; border-bottom: 2px solid #151b27;
        background: #080b12; padding: 0 8px;
    }
    .stTabs [data-baseweb="tab"] {
        background: none; border-radius: 0; border: none;
        padding: 12px 24px; color: #94a3b8; font-weight: 600;
        font-size: 0.92rem; letter-spacing: 0.3px;
        transition: all 0.2s ease;
    }
    .stTabs [data-baseweb="tab"]:hover { color: #ffffff; }
    .stTabs [aria-selected="true"] {
        background: none !important; color: #00e6a7 !important;
        border-bottom: 2px solid #00e6a7 !important; font-weight: 700;
    }

    /* ── KPI Cards ────────────────────────────────────────────────────────── */
    .kpi-card {
        background: linear-gradient(135deg, #0c0f16 0%, #0f1219 100%);
        border: 1px solid #151b27; border-radius: 12px;
        padding: 18px 20px; text-align: center;
        transition: border-color 0.2s ease;
    }
    .kpi-card:hover { border-color: #1e2536; }
    .kpi-card .label {
        color: #7cb4ff; font-size: 0.78rem; font-weight: 700;
        text-transform: uppercase; letter-spacing: 1px; margin-bottom: 6px;
    }
    .kpi-card .value { color: #ffffff; font-size: 1.4rem; font-weight: 800; }
    .kpi-card .sub   { color: #e0a846; font-size: 0.82rem; font-weight: 500; margin-top: 4px; }
    div[data-testid="stMetricValue"] { color: #ffffff; font-weight: 800; }
    div[data-testid="stMetricLabel"] { color: #7cb4ff; font-weight: 600; text-transform: uppercase; font-size: 0.82rem; letter-spacing: 0.5px; }

    /* ── Badges ────────────────────────────────────────────────────────────── */
    .badge-profit { background: rgba(0,230,167,0.1); color: #00e6a7; border: 1px solid rgba(0,230,167,0.2); padding: 4px 12px; border-radius: 6px; font-size: 0.84rem; font-weight: 700; display: inline-block; }
    .badge-loss   { background: rgba(255,92,92,0.1);  color: #ff5c5c; border: 1px solid rgba(255,92,92,0.2);  padding: 4px 12px; border-radius: 6px; font-size: 0.84rem; font-weight: 700; display: inline-block; }
    .badge-warn   { background: rgba(255,180,0,0.1);  color: #ffb400; border: 1px solid rgba(255,180,0,0.2);  padding: 4px 12px; border-radius: 6px; font-size: 0.84rem; font-weight: 700; display: inline-block; }
    .badge-info   { background: rgba(59,139,255,0.1);  color: #7cb4ff; border: 1px solid rgba(59,139,255,0.2);  padding: 4px 12px; border-radius: 6px; font-size: 0.84rem; font-weight: 700; display: inline-block; }
    .gate-pass    { background: rgba(0,230,167,0.1);  color: #00e6a7; border: 1px solid rgba(0,230,167,0.2);  padding: 3px 10px; border-radius: 6px; font-size: 0.82rem; font-weight: 600; display: inline-block; }
    .gate-fail    { background: rgba(100,116,139,0.1);color: #64748b; border: 1px solid rgba(100,116,139,0.2); padding: 3px 10px; border-radius: 6px; font-size: 0.82rem; font-weight: 600; display: inline-block; }

    /* ── P/L colors ───────────────────────────────────────────────────────── */
    .profit { color: #00e6a7 !important; font-weight: 700; }
    .loss   { color: #ff5c5c !important; font-weight: 700; }

    /* ── Log levels ───────────────────────────────────────────────────────── */
    .log-error   { color: #ff5c5c; font-weight: 600; }
    .log-warning { color: #ffb400; font-weight: 600; }
    .log-success { color: #00e6a7; font-weight: 600; }
    .log-info    { color: #7cb4ff; font-weight: 500; }

    /* ── Section header ───────────────────────────────────────────────────── */
    .section-header {
        color: #f1f5f9; font-size: 1.05rem; font-weight: 700;
        padding-bottom: 10px; border-bottom: 2px solid #151b27;
        margin-bottom: 16px; text-transform: uppercase; letter-spacing: 0.8px;
        display: flex; align-items: center; gap: 8px;
    }

    /* ── Status dots ──────────────────────────────────────────────────────── */
    .status-dot {
        display: inline-block; width: 8px; height: 8px;
        border-radius: 50%; margin-right: 6px;
    }
    .status-running { background: #00e6a7; box-shadow: 0 0 10px rgba(0,230,167,0.5); animation: pulse-green 2s infinite; }
    .status-stopped { background: #ff5c5c; box-shadow: 0 0 10px rgba(255,92,92,0.5); }
    @keyframes pulse-green { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }

    /* ── Insight box ──────────────────────────────────────────────────────── */
    .insight-box {
        background: linear-gradient(135deg, #0c1020 0%, #0d1117 100%);
        border: 1px solid #1e2536; border-left: 3px solid #3b8bff;
        border-radius: 8px; padding: 14px 18px; margin-top: 10px;
    }
    .insight-box .title { color: #7cb4ff; font-size: 0.82rem; font-weight: 700; margin-bottom: 6px; text-transform: uppercase; letter-spacing: 0.8px; }
    .insight-box .text  { color: #cbd5e1; font-size: 0.92rem; font-weight: 500; line-height: 1.7; }

    /* ── Expander fix: keep header dark on expand ─────────────────────────── */
    .stApp details[open] > summary,
    .stApp [data-testid="stExpander"] details[open] > summary,
    .stApp [data-testid="stExpander"] summary,
    .stApp [data-testid="stExpander"] [data-testid="stExpanderToggleDetails"],
    .stApp details > summary {
        background-color: #0c0f16 !important;
        color: #e2e8f0 !important;
    }
    .stApp [data-testid="stExpander"] {
        background: #0c0f16 !important;
        border: 1px solid #151b27 !important;
        border-radius: 10px !important;
        overflow: hidden;
    }
    .stApp [data-testid="stExpander"] details {
        background: #0c0f16 !important;
    }
    .stApp [data-testid="stExpander"] summary {
        background: #0c0f16 !important;
        border-bottom: 1px solid #151b27;
        padding: 10px 16px !important;
    }
    .stApp [data-testid="stExpander"] summary:hover {
        background: #0f1219 !important;
    }
    .stApp [data-testid="stExpander"] summary p,
    .stApp [data-testid="stExpander"] summary span {
        color: #e2e8f0 !important;
    }
    .stApp [data-testid="stExpander"] [data-testid="stExpanderDetails"] {
        background: #080b12 !important;
        border-top: 1px solid #151b27;
        padding: 16px !important;
    }

    /* ── Position row ─────────────────────────────────────────────────────── */
    .pos-row {
        display: flex; justify-content: space-between; align-items: center;
        padding: 12px 16px; background: #0c0f16;
        border: 1px solid #151b27; border-radius: 10px; margin-bottom: 8px;
        transition: all 0.2s ease; cursor: default;
    }
    .pos-row:hover { border-color: #1e2d42; background: #0e1219; }
    .pos-sym    { font-weight: 800; color: #ffffff; font-size: 1.1rem; min-width: 90px; }
    .pos-price  { color: #5eead4; font-size: 0.9rem; font-family: 'JetBrains Mono', monospace; }
    .pos-pl     { font-weight: 700; font-size: 1rem; }
    .pos-meta   { color: #e0a846; font-size: 0.85rem; }

    /* ── Detail grid ──────────────────────────────────────────────────────── */
    .detail-grid {
        display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
        gap: 12px; padding: 12px 0;
    }
    .detail-item { }
    .detail-label { color: #7cb4ff; font-size: 0.8rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 3px; }
    .detail-value { color: #ffffff; font-size: 0.95rem; font-weight: 600; }

    /* ── Event row ────────────────────────────────────────────────────────── */
    .event-row {
        display: flex; gap: 12px; align-items: center;
        padding: 8px 14px; border-bottom: 1px solid #0f1420;
        transition: background 0.15s ease;
    }
    .event-row:hover { background: #0c0f16; }
    .event-icon  { font-size: 1rem; min-width: 24px; text-align: center; }
    .event-time  { color: #5eead4; font-size: 0.84rem; min-width: 110px; font-family: 'JetBrains Mono', monospace; }
    .event-sym   { font-weight: 700; color: #ffffff; min-width: 80px; font-size: 0.95rem; }
    .event-type  { color: #e0a846; font-size: 0.88rem; font-family: 'JetBrains Mono', monospace; }
    .event-msg   { color: #c4b5fd; font-size: 0.84rem; margin-left: auto; max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

    /* ── Log terminal ─────────────────────────────────────────────────────── */
    .log-terminal {
        background: #06080d; border: 1px solid #151b27; border-radius: 10px;
        padding: 16px 18px; max-height: 620px; overflow-y: auto;
        font-family: 'JetBrains Mono', monospace; font-size: 0.88rem;
    }
    .log-line {
        margin-bottom: 4px; line-height: 1.6; padding: 3px 0;
        border-bottom: 1px solid #0a0d14;
    }
    .log-ts { color: #5eead4; margin-right: 10px; }

    /* ── Gates visual ─────────────────────────────────────────────────────── */
    .gates-bar { display: flex; gap: 4px; align-items: center; }
    .gate-dot  {
        width: 26px; height: 26px; border-radius: 6px;
        display: flex; align-items: center; justify-content: center;
        font-size: 0.75rem; font-weight: 700; line-height: 1;
    }
    .gate-dot.pass { background: rgba(0,230,167,0.15); color: #00e6a7; border: 1px solid rgba(0,230,167,0.25); }
    .gate-dot.fail { background: rgba(100,116,139,0.08); color: #4a5568; border: 1px solid rgba(100,116,139,0.15); }

    /* ── Scrollbar ────────────────────────────────────────────────────────── */
    ::-webkit-scrollbar { width: 6px; }
    ::-webkit-scrollbar-track { background: #06080d; }
    ::-webkit-scrollbar-thumb { background: #1e2536; border-radius: 3px; }
    ::-webkit-scrollbar-thumb:hover { background: #2d3a50; }

    /* ── Divider ──────────────────────────────────────────────────────────── */
    .stApp hr { border-color: #151b27 !important; }

    /* ── Current dashboard direction: light, compact, row-based ───────────── */
    .stApp {
        background: #f4f7fb !important;
        color: #172033 !important;
    }
    .stApp div, .stApp p, .stApp label,
    .stApp h1, .stApp h2, .stApp h3, .stApp h4,
    .stApp td, .stApp th { color: #172033 !important; }
    .stApp [data-testid="stHeader"] { background: #f4f7fb !important; }
    .stTabs [data-baseweb="tab-list"] {
        background: #ffffff !important;
        border-bottom: 1px solid #d8e0eb !important;
    }
    .stTabs [data-baseweb="tab"] { color: #607089 !important; }
    .stTabs [aria-selected="true"] {
        color: #087f73 !important;
        border-bottom-color: #087f73 !important;
    }
    .stApp [data-testid="stExpander"],
    .stApp [data-testid="stExpander"] details,
    .stApp [data-testid="stExpander"] summary,
    .stApp [data-testid="stExpander"] [data-testid="stExpanderDetails"] {
        background: #ffffff !important;
        border-color: #d8e0eb !important;
    }
    .stApp [data-testid="stExpander"] summary p,
    .stApp [data-testid="stExpander"] summary span { color: #172033 !important; }
    .stApp input, .stApp textarea,
    .stApp [data-baseweb="select"] > div {
        background: #ffffff !important;
        color: #172033 !important;
        border-color: #cbd5e1 !important;
    }
    .kpi-card { background: #ffffff !important; border-color: #d8e0eb !important; border-radius: 6px !important; }
    .kpi-card .label, .detail-label { color: #087f73 !important; }
    .kpi-card .value, .detail-value { color: #172033 !important; }
    .section-header { color: #172033 !important; border-bottom-color: #d8e0eb !important; }
    .badge-info { color: #1769aa !important; background: #eaf4ff !important; border-color: #b9dcfa !important; }
    .badge-profit { color: #087f73 !important; background: #e5f7f3 !important; border-color: #a9e4d8 !important; }
    .badge-warn { color: #946200 !important; background: #fff5d9 !important; border-color: #f1d486 !important; }
    .badge-loss { color: #b42318 !important; background: #fff0ee !important; border-color: #f2b8b3 !important; }
    .insight-box { background: #ffffff !important; border-color: #d8e0eb !important; }
    .insight-box .title, .insight-box .text { color: #334155 !important; }
    .log-terminal { background: #ffffff !important; border-color: #d8e0eb !important; }
    .event-row:hover { background: #f5f8fc !important; }
    .event-time { color: #087f73 !important; }
    .event-sym { color: #172033 !important; }
    .event-msg { color: #475569 !important; }
    .stApp [data-testid="stVerticalBlock"] > div:has(> [data-testid="stHorizontalBlock"]) {
        border-bottom: 1px solid #e5eaf1;
        padding-bottom: 10px;
        margin-bottom: 10px;
    }

    /* ── Plotly chart container ────────────────────────────────────────────── */
    .stPlotlyChart { border-radius: 10px; overflow: hidden; }

    /* ── Scanner: compact trader rows ─────────────────────────────────────── */
    .scan-row {
        background: #ffffff;
        border: 1px solid #d8e0eb;
        border-radius: 8px;
        padding: 8px 14px;
        margin-top: 8px;
        margin-bottom: 2px;
        transition: border-color .15s ease, box-shadow .15s ease;
    }
    .scan-row:hover {
        border-color: #087f73;
        box-shadow: 0 1px 4px rgba(8,127,115,0.08);
    }
    .scan-row-main {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        row-gap: 4px;
    }
    .scan-row-name {
        display: flex; align-items: center; gap: 10px;
        min-width: 240px; flex: 1 1 auto;
    }
    .scan-name  { font-weight: 700; color: #172033 !important; font-size: 0.98rem; }
    .scan-short {
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.78rem; color: #64748b !important;
        background: #f1f5f9; padding: 2px 6px; border-radius: 4px;
    }
    .scan-row-metrics {
        display: flex; align-items: center; gap: 14px;
        flex-wrap: wrap; row-gap: 4px;
    }
    .scan-metric {
        display: inline-flex; flex-direction: column; align-items: flex-start;
        line-height: 1.1; min-width: 58px;
    }
    .scan-metric em {
        color: #64748b !important;
        font-style: normal; font-size: 0.68rem;
        font-weight: 600; text-transform: uppercase; letter-spacing: 0.4px;
    }
    .scan-metric b {
        color: #172033 !important;
        font-size: 0.92rem; font-weight: 700;
        font-family: 'JetBrains Mono', monospace;
    }
    .scan-metric b.profit { color: #087f73 !important; }
    .scan-metric b.loss   { color: #b42318 !important; }
    .scan-live {
        display: inline-flex; align-items: center; gap: 6px;
        padding-left: 8px; border-left: 1px solid #e2e8f0;
    }
    .scan-live em {
        color: #64748b !important; font-style: normal;
        font-size: 0.72rem; font-weight: 500;
    }
    .scan-live-list {
        display: flex; flex-direction: column; gap: 4px;
        margin: 6px 0 10px;
    }
    .scan-live-row {
        display: flex; gap: 14px; align-items: center;
        padding: 6px 10px; background: #f8fafc;
        border: 1px solid #e2e8f0; border-radius: 6px;
        font-size: 0.86rem;
    }
    .scan-live-row b { color: #172033 !important; min-width: 90px; }
    .scan-live-row span { color: #475569 !important; }
    .scan-links {
        margin-top: 10px; font-size: 0.86rem; color: #475569 !important;
    }
    .scan-links a { color: #1769aa !important; text-decoration: none; margin: 0 4px; }
    .scan-links a:hover { text-decoration: underline; }

    /* Compact expander directly under a .scan-row */
    .stApp [data-testid="stExpander"] { margin-top: 0 !important; margin-bottom: 6px !important; }
    .stApp [data-testid="stExpander"] summary {
        padding: 6px 14px !important; font-size: 0.82rem !important;
    }

    /* ── Pipeline status strip (top of "Meine Trader") ────────────────────── */
    .pipeline-strip {
        display: flex; align-items: center; gap: 10px;
        padding: 8px 14px; margin: 4px 0 10px;
        background: #ffffff;
        border: 1px solid #d8e0eb;
        border-radius: 8px;
        font-size: 0.86rem;
    }
    .pipeline-strip em {
        font-style: normal;
        color: #64748b !important;
        font-size: 0.82rem;
    }
    .pipeline-strip code {
        background: #f1f5f9; color: #172033 !important;
        padding: 1px 6px; border-radius: 4px;
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.8rem;
    }
</style>
"""
