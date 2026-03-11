from __future__ import annotations

import base64
import json
import tempfile
import hashlib
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from shiny import App, Inputs, Outputs, Session, reactive, render, ui

from core import apply_adjustments, load_template_modules
from storage import (
    StorageError,
    get_active_template_local_path,
    load_adjustment,
    save_adjustment,
    save_template,
)

CAREER_PATHWAYS_DEFAULT = [
    "Academic",
    "Research",
    "Education",
    "Clinical psychology or psychotherapy",
    "Health and social care",
    "Business and wider industry roles",
]

CAREER_PATHWAY_EXPLANATIONS: Dict[str, str] = {
    "Academic": "the module prepares you for a career in higher education institutions, developing knowledge through research and teaching it others.",
    "Research": "the module prepares you to conduct research across contexts. Research roles are common across industries, not just in HE.",
    "Education": "the module prepares you to deliver teaching to others, this is not just in schools but would be relevant to learning and development roles across contexts.",
    "Clinical psychology or psychotherapy": "there are many different careers and roles that involve support others with their mental health. Modules scoring high here prepare you for any of these pathways.",
    "Health and social care": "the module prepares you for roles in supporting others with their health in care in the community.",
    "Business and wider industry roles": "the module prepares you for a wider range of professional roles in such as management, operations (such as marketing, HR, or finance), professional services, and others that are commonly found in both public and private organisations.",
}
SKILLS_PAGE_SIZE = 12


def normalize_assessment_type(value: Any) -> str:
    """Map free-text assessment labels to the canonical values used by filters."""
    if value is None:
        return ""
    raw = str(value).strip()
    if not raw:
        return ""
    normalized = raw.lower().replace("&", "and").replace("/", " ").replace("-", " ")
    normalized = " ".join(normalized.split())
    has_coursework = "coursework" in normalized or normalized in ("cw", "course work")
    has_exam = "exam" in normalized or "examination" in normalized
    if "hybrid" in normalized or (has_coursework and has_exam) or ("coursework" in normalized and "exam" in normalized):
        return "Hybrid"
    if has_coursework:
        return "Coursework"
    if has_exam:
        return "Exam"
    return ""


def parse_keywords(raw: str) -> List[str]:
    """Parse comma/newline/semicolon separated keywords into a clean list."""
    parts: List[str] = []
    for token in (raw or "").replace("\n", ",").replace(";", ",").split(","):
        token = token.strip()
        if token:
            parts.append(token)
    return parts


def get_skill_score_sum(module: Dict[str, Any], keywords: List[str]) -> int:
    """Sum scores for skills whose label contains any requested keyword."""
    skills = module.get("skills", []) or []
    if not skills:
        return 0
    keyword_set = [kw.lower() for kw in keywords]
    total = 0
    matched = False
    for item in skills:
        label_lower = str(item.get("label", "")).lower()
        if not any(kw in label_lower for kw in keyword_set):
            continue
        total += int(item.get("score", 0))
        matched = True
    return total if matched else 0


def get_career_score(module: Dict[str, Any], target: str) -> int:
    """Return best matching career score using exact match, then partial fallback."""
    careers = module.get("careers", []) or []
    if not careers:
        return 0
    target_norm = target.strip().lower()
    best: Optional[int] = None
    for item in careers:
        label = str(item.get("career", "")).strip()
        if not label:
            continue
        label_norm = label.lower()
        if label_norm == target_norm:
            return int(item.get("score", 0))
        if target_norm in label_norm or label_norm in target_norm:
            score = int(item.get("score", 0))
            if best is None or score > best:
                best = score
    return best if best is not None else 0


def _normalize_location_text(value: Any) -> str:
    return str(value or "").strip().lower().replace("’", "'")


def _location_map_url(location: str) -> str:
    loc_norm = _normalize_location_text(location)
    if "guy's" in loc_norm or "guys campus" in loc_norm:
        return "https://www.google.com/maps/search/?api=1&query=Guy%27s+Campus+London"
    if "denmark hill" in loc_norm:
        return "https://www.google.com/maps/search/?api=1&query=Denmark+Hill+Campus+London"
    return ""


def _safe_external_url(raw: str) -> str:
    url = str(raw or "").strip()
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return url


def _parse_module_leader_entry(raw: str) -> Tuple[str, str]:
    text = str(raw or "").strip()
    if not text:
        return "", ""

    # Support plain "Name(https://...)" format.
    paren_match = re.match(r"^(.+?)\((https?://[^\s)]+)\)$", text)
    if paren_match:
        return paren_match.group(1).strip(), _safe_external_url(paren_match.group(2))

    md_match = re.match(r"^\[(.+?)\]\((https?://[^\s)]+)\)$", text)
    if md_match:
        return md_match.group(1).strip(), _safe_external_url(md_match.group(2))

    if "|" in text:
        label, url = text.split("|", 1)
        label = label.strip()
        safe_url = _safe_external_url(url)
        return (label if label else safe_url), safe_url

    http_idx = text.find("http://")
    if http_idx < 0:
        http_idx = text.find("https://")
    if http_idx > 0:
        label = text[:http_idx].strip(" -:([")
        safe_url = _safe_external_url(text[http_idx:].strip())
        return (label if label else safe_url), safe_url

    direct_url = _safe_external_url(text)
    if direct_url:
        return direct_url, direct_url
    return text, ""


def _module_leaders_display(value: Any):
    raw = str(value or "").strip()
    if not raw:
        return "(not set)"

    tokens = [t.strip() for t in raw.replace("\n", ";").split(";") if t.strip()]
    nodes: List[Any] = []
    for idx, token in enumerate(tokens):
        label, url = _parse_module_leader_entry(token)
        node = (
            ui.tags.a(label, href=url, target="_blank", rel="noopener noreferrer")
            if url
            else label
        )
        if idx > 0:
            nodes.append(", ")
        nodes.append(node)
    return ui.TagList(*nodes)


def _normalize_multiline_text(value: Any) -> str:
    """Convert escaped newline sequences to real newlines for display/editing."""
    text = str(value or "")
    text = text.replace("\r\n", "\n")
    text = text.replace("\\r\\n", "\n")
    text = text.replace("\\n", "\n")
    return text


def _find_text_exact(raw: str, query: str) -> Tuple[int, int]:
    """Find exact, case-sensitive text in raw JSON."""
    if not raw or not query:
        return -1, 0
    idx = raw.find(query)
    return (idx, len(query)) if idx >= 0 else (-1, 0)


def _line_col_from_index(raw: str, idx: int) -> Tuple[int, int]:
    """Convert a 0-based character index to 1-based line and column."""
    if idx < 0:
        return 0, 0
    line = raw.count("\n", 0, idx) + 1
    last_nl = raw.rfind("\n", 0, idx)
    col = idx + 1 if last_nl < 0 else idx - last_nl
    return line, col


def normalize_adjustment_keys(adj: Any) -> Dict[str, Any]:
    """
    Ensure adjustment JSON is keyed by pure module code.
    Accepts legacy keys like "6PAHPSON Social Neuroscience" and converts them to "6PAHPSON".
    """
    if not isinstance(adj, dict):
        return {"modules": {}}
    modules = adj.get("modules", {})
    if not isinstance(modules, dict):
        modules = {}
    fixed: Dict[str, Any] = {"modules": {}}
    for k, v in modules.items():
        if not isinstance(k, str):
            continue
        code = k.strip().split()[0] if k.strip() else ""
        if not code:
            continue
        if not isinstance(v, dict):
            continue
        fixed["modules"].setdefault(code, {})
        fixed["modules"][code].update(v)
    return fixed


def _apply_matplotlib_style() -> None:
    """Keep Matplotlib output visually consistent with the app theme."""
    try:
        import matplotlib as mpl

        mpl.rcParams.update(
            {
                "font.family": "Segoe UI",
                "font.sans-serif": ["Segoe UI", "Arial", "sans-serif"],
                "text.color": "#1f2933",
                "axes.labelcolor": "#1f2933",
                "xtick.color": "#1f2933",
                "ytick.color": "#1f2933",
            }
        )
    except Exception:
        return


CSS = """
@import url("https://fonts.googleapis.com/css2?family=Fraunces:wght@500;700&family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap");

:root {
  --kmsh-bg: #f7f1e6;
  --kmsh-surface: #ffffff;
  --kmsh-surface-2: #fff6ea;
  --kmsh-text: #1f2933;
  --kmsh-muted: #5f6b7a;
  --kmsh-primary: #0f766e;
  --kmsh-primary-strong: #0b4f4b;
  --kmsh-accent: #c2410c;
  --kmsh-border: #e6ddcd;
  --kmsh-ring: rgba(15, 118, 110, 0.18);
  --kmsh-shadow: 0 16px 38px rgba(31, 41, 51, 0.16);
  --kmsh-shadow-soft: 0 6px 22px rgba(31, 41, 51, 0.12);
  --kmsh-navbar-height: 60px;
  --kmsh-page-bg:
    radial-gradient(1200px 520px at 18% -10%, rgba(15, 118, 110, 0.12), transparent 60%),
    radial-gradient(900px 420px at 95% 0%, rgba(194, 65, 12, 0.12), transparent 55%),
    var(--kmsh-bg);
}

body {
  font-family: "Segoe UI", "Plus Jakarta Sans", sans-serif;
  color: var(--kmsh-text);
  line-height: 1.55;
  min-height: 100vh;
  margin: 0;
  background: var(--kmsh-page-bg);
  background-attachment: fixed;
}

html,
body {
  min-height: 100%;
  height: 100%;
}

html {
  background: var(--kmsh-page-bg);
  /* Reserve gutter so modal open/close does not shift the card grid. */
  scrollbar-gutter: stable both-edges;
}

/* Bootstrap sets body padding-right when modal opens; neutralize that compensation. */
body.modal-open {
  padding-right: 0 !important;
}

.bslib-page-navbar,
.bslib-page-navbar > .container-fluid,
.bslib-page-navbar .tab-content,
.bslib-page-navbar .tab-pane {
  background: transparent !important;
}

.bslib-page-navbar {
  min-height: 100vh;
}

h1, h2, h3, h4, h5, .h1, .h2, .h3, .h4, .h5 {
  font-family: "Fraunces", "Plus Jakarta Sans", serif;
  letter-spacing: 0.2px;
}

a { color: var(--kmsh-primary); }
a:hover { color: var(--kmsh-primary-strong); }

.navbar {
  background: rgba(255, 255, 255, 0.92);
  backdrop-filter: blur(10px);
  border-bottom: 1px solid var(--kmsh-border);
  padding: 0;
  height: var(--kmsh-navbar-height);
}

.navbar > .container-fluid {
  padding-left: 0;
  padding-right: 24px;
  height: 100%;
  align-items: center;
}

.navbar-brand {
  font-weight: 700;
  color: var(--kmsh-primary);
  padding: 0;
  margin: 0;
  height: 100%;
  display: inline-flex;
  align-items: center;
}

.kmsh-brand {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  padding: 0 16px 0 0;
  height: 100%;
}

.kmsh-logo {
  height: 100%;
  width: auto;
  display: block;
  object-fit: cover;
  object-position: left center;
}

@media (max-width: 600px) {
  :root { --kmsh-navbar-height: 54px; }
}

.navbar .nav-link {
  color: #334155;
  font-weight: 600;
}

.navbar .nav-link.active,
.navbar .nav-link:hover {
  color: var(--kmsh-primary);
}

.container-fluid {
  padding-left: 24px;
  padding-right: 24px;
}

.card {
  border: 1px solid var(--kmsh-border);
  border-radius: 16px;
  box-shadow: var(--kmsh-shadow-soft);
  background: var(--kmsh-surface);
}

.card-header {
  background: linear-gradient(135deg, rgba(15, 118, 110, 0.08), rgba(194, 65, 12, 0.08));
  border-bottom: 1px solid var(--kmsh-border);
  font-weight: 600;
}

.btn,
.kmsh-btn,
.kmsh-btn-full {
  height: 38px;
  padding: 6px 18px;
  border-radius: 999px;
  border: 0;
  background: var(--kmsh-primary);
  color: #fff;
  box-shadow: 0 6px 16px rgba(15, 118, 110, 0.25);
  transition: transform 0.16s ease, box-shadow 0.16s ease, background 0.16s ease;
}

.btn:hover,
.kmsh-btn:hover,
.kmsh-btn-full:hover {
  background: var(--kmsh-primary-strong);
  transform: translateY(-1px);
  box-shadow: 0 10px 22px rgba(15, 118, 110, 0.28);
  color: #fff;
}

.btn:focus,
.kmsh-btn:focus,
.kmsh-btn-full:focus {
  outline: 0;
  box-shadow: 0 0 0 4px var(--kmsh-ring);
}

.form-control,
.form-select,
.form-check-input {
  border-radius: 12px;
  border: 1px solid var(--kmsh-border);
  background: #fffaf4;
}

.form-control:focus,
.form-select:focus,
.form-check-input:focus {
  border-color: var(--kmsh-primary);
  box-shadow: 0 0 0 3px var(--kmsh-ring);
}

.table {
  border-collapse: separate;
  border-spacing: 0;
  border: 1px solid rgba(143, 124, 96, 0.28);
  border-radius: 14px;
  overflow: hidden;
  background: rgba(255, 250, 244, 0.68);
  backdrop-filter: blur(6px);
  box-shadow: 0 10px 26px rgba(31, 41, 51, 0.08);
}

.table thead th {
  background: rgba(255, 242, 225, 0.9);
  color: #334155;
  border-bottom: 1px solid rgba(143, 124, 96, 0.22);
  font-weight: 600;
}

.table-striped > tbody > tr:nth-of-type(odd) > * {
  background: rgba(255, 255, 255, 0.45);
}

.table-striped > tbody > tr:nth-of-type(even) > * {
  background: rgba(250, 240, 228, 0.35);
}

.table tbody tr:hover > * {
  background: rgba(15, 118, 110, 0.08);
}

hr {
  border: 0;
  height: 1px;
  margin: 18px 0 12px;
  background: linear-gradient(
    90deg,
    rgba(143, 124, 96, 0.1),
    rgba(143, 124, 96, 0.4),
    rgba(143, 124, 96, 0.1)
  );
}

/* Home cards */
.kmsh-home-grid { display: grid; grid-template-columns: repeat(4, minmax(240px, 1fr)); gap: 18px; }
@media (max-width: 1200px) { .kmsh-home-grid { grid-template-columns: repeat(2, minmax(240px, 1fr)); } }
@media (max-width: 650px) { .kmsh-home-grid { grid-template-columns: repeat(1, minmax(240px, 1fr)); } }
.kmsh-home-card {
  border: 1px solid var(--kmsh-border);
  border-radius: 18px;
  padding: 16px 16px;
  min-height: 205px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  background: linear-gradient(140deg, #ffffff 0%, var(--kmsh-surface-2) 100%);
  box-shadow: var(--kmsh-shadow-soft);
  transition: transform 0.18s ease, box-shadow 0.18s ease;
}
.kmsh-home-card p { margin-bottom: 0; min-height: 3.2em; }
.kmsh-home-card .action-button { align-self: flex-start; margin-top: 10px; }
.kmsh-home-card:hover { transform: translateY(-3px); box-shadow: var(--kmsh-shadow); }

/* Toolbar */
.kmsh-toolbar { display: flex; gap: 12px; align-items: center; }
.kmsh-mod-search { display: flex; gap: 12px; align-items: center; }
.kmsh-mod-search .kmsh-mod-refresh { margin-left: auto; }
.kmsh-mod-search .form-group { margin-bottom: 0; }
.kmsh-mod-search .form-group { flex: 1 1 auto; }
.kmsh-mod-search .form-group input { width: 100%; }
.kmsh-adjustment-search { display: flex; gap: 12px; align-items: flex-end; margin-bottom: 10px; }
.kmsh-adjustment-search .form-group { flex: 1 1 auto; margin-bottom: 0; }
.kmsh-adjustment-search .form-group input { width: 100%; }
.kmsh-adjustment-search .btn { white-space: nowrap; }
@media (max-width: 768px) {
  .kmsh-adjustment-search { flex-direction: column; align-items: stretch; }
}

/* Modules cards (ui.card + layout_column_wrap) */
.kmsh-mod-card .card { border-radius: 18px; height: 220px; }
.kmsh-mod-card .card-header { color: var(--kmsh-primary-strong); }
.kmsh-mod-body { display: flex; flex-direction: column; height: 100%; }
.kmsh-mod-code { font-weight: 700; letter-spacing: 0.2px; }
.kmsh-mod-name { margin-top: 4px; }
.kmsh-mod-meta { margin-top: 10px; font-size: 0.9em; color: var(--kmsh-muted); }
.kmsh-spacer { flex: 1 1 auto; }
.kmsh-btn-full { width: 100%; }
.kmsh-pager { display: flex; justify-content: flex-end; align-items: center; gap: 10px; margin-top: 8px; }
.kmsh-analyzer-actions { display: flex; gap: 12px; align-items: center; }
.kmsh-skills-actions { margin: 4px 0 12px; }
.kmsh-empty-plot {
  min-height: 208px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--kmsh-text);
}
.kmsh-table-row { cursor: pointer; }
.kmsh-multiline-text { white-space: pre-wrap; }
.kmsh-section-gap { margin-bottom: 1em; }
.kmsh-career-card { width: 100%; }
.kmsh-career-card .card-body { display: flex; justify-content: center; }
.kmsh-career-paths .form-check,
.kmsh-career-paths label,
.kmsh-career-paths .form-check-label {
  white-space: nowrap !important;
  overflow-wrap: normal;
  word-break: keep-all;
}
.kmsh-status {
  margin: 6px 0 20px;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 8px 14px;
  border-radius: 999px;
  background: rgba(255, 250, 244, 0.65);
  border: 1px solid rgba(143, 124, 96, 0.2);
  color: var(--kmsh-muted);
  font-size: 0.9em;
  box-shadow: 0 8px 18px rgba(31, 41, 51, 0.08);
}

#status_msg_out {
  margin: 0;
}

.kmsh-skill-showcase {
  display: grid;
  gap: 14px;
  grid-template-columns: repeat(7, minmax(170px, 1fr));
  min-width: calc(7 * 170px + 6 * 14px);
}

.modal-dialog:has(.kmsh-skill-showcase) {
  max-width: none !important;
  width: fit-content !important;
  margin: 0.6rem auto;
}

.modal-dialog:has(.kmsh-skill-showcase) .modal-content {
  width: fit-content;
}

.modal-dialog:has(.kmsh-skill-showcase) .modal-body {
  max-height: 80vh;
  overflow-y: auto;
  overflow-x: hidden;
}

.kmsh-skill-col {
  --kmsh-skill-main: #6b7280;
  --kmsh-skill-soft: #e5e7eb;
  --kmsh-skill-surface: #f3f4f6;
  border-radius: 22px;
  background: linear-gradient(180deg, #eff3f6 0%, #f9fbfd 100%);
  padding: 14px 12px 12px;
  border: 1px solid rgba(51, 65, 85, 0.14);
  box-shadow: 0 8px 20px rgba(31, 41, 51, 0.12);
}

.kmsh-skill-col--foundational {
  --kmsh-skill-main: #3faa74;
  --kmsh-skill-soft: #a9e2c1;
  --kmsh-skill-surface: #5cbc87;
}

.kmsh-skill-col--communication {
  --kmsh-skill-main: #ef7d3a;
  --kmsh-skill-soft: #f6c2a1;
  --kmsh-skill-surface: #ec6a2f;
}

.kmsh-skill-col--leadership {
  --kmsh-skill-main: #e0b72f;
  --kmsh-skill-soft: #f2dd93;
  --kmsh-skill-surface: #e4c24f;
}

.kmsh-skill-col--adaptability {
  --kmsh-skill-main: #3f9fa5;
  --kmsh-skill-soft: #8ccdcf;
  --kmsh-skill-surface: #4e9ca3;
}

.kmsh-skill-col--creativity {
  --kmsh-skill-main: #4aa9db;
  --kmsh-skill-soft: #9bd4ef;
  --kmsh-skill-surface: #57aacf;
}

.kmsh-skill-col--digital {
  --kmsh-skill-main: #e56a83;
  --kmsh-skill-soft: #f1afbc;
  --kmsh-skill-surface: #e26d7f;
}

.kmsh-skill-col--other {
  --kmsh-skill-main: #6b7280;
  --kmsh-skill-soft: #c4c9d1;
  --kmsh-skill-surface: #7b8291;
}

.kmsh-skill-col-top {
  display: flex;
  justify-content: center;
  margin-bottom: 10px;
}

.kmsh-skill-icon-wrap {
  width: 68px;
  height: 68px;
  border-radius: 18px;
  background: #fff;
  border: 3px solid var(--kmsh-skill-soft);
  box-shadow: 0 4px 12px rgba(31, 41, 51, 0.1);
  display: grid;
  place-items: center;
}

.kmsh-skill-icon {
  width: 26px;
  height: 26px;
  border-radius: 8px;
  transform: rotate(45deg);
  background: var(--kmsh-skill-main);
  box-shadow: 0 0 0 6px rgba(255, 255, 255, 0.95);
}

.kmsh-skill-title {
  text-align: center;
  font-family: "Fraunces", "Plus Jakarta Sans", serif;
  font-size: 1.08rem;
  font-weight: 700;
  line-height: 1.12;
  letter-spacing: 0.05px;
  overflow-wrap: anywhere;
  color: #1e293b;
  border: 2px solid var(--kmsh-skill-soft);
  border-radius: 12px;
  background: rgba(255, 255, 255, 0.7);
  padding: 10px 8px;
  min-height: 68px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.kmsh-skill-list {
  margin-top: 12px;
  border-radius: 16px;
  background: var(--kmsh-skill-surface);
  color: #132234;
  padding: 12px 10px;
  min-height: 260px;
  max-height: 58vh;
  overflow: auto;
}

.kmsh-skill-item {
  text-align: center;
  font-weight: 600;
  line-height: 1.15;
  letter-spacing: 0.1px;
  padding: 9px 2px;
}

.kmsh-skill-item + .kmsh-skill-item {
  border-top: 1px solid rgba(255, 255, 255, 0.35);
}
"""

KCL_LOGO_FILE = Path(__file__).resolve().parent / "logo" / "KCLlogo.png"


def _kcl_logo_data_uri() -> str:
    """Embed logo bytes as a data URI so deployment does not depend on static hosting."""
    try:
        encoded = base64.b64encode(KCL_LOGO_FILE.read_bytes()).decode("ascii")
    except Exception:
        return ""
    return f"data:image/png;base64,{encoded}"


KCL_LOGO_SRC = _kcl_logo_data_uri()


# ----------------------------
# UI
# ----------------------------

SHOW_ADMIN_MENU = False
"""Important: Make sure it is False when you push it to shinyapps"""

home_panel = ui.nav_panel(
    "Home",
    ui.page_fluid(
        ui.output_ui("home_or_tool"),
        ui.hr(),
        ui.div({"class": "kmsh-status"}, ui.output_text("status_msg_out")),
    ),
)

join_us_panel = ui.nav_panel(
    "Join us",
    ui.page_fluid(
        ui.h3("Join us"),
        ui.p("KingsMSH is meant to be developed and maintained by Psychology BSc students"),
        ui.p("We welcome contributors interested in psychology education, data visualisation, and product building."),
        ui.p("Original codes & tutorial: ", ui.tags.a('https://github.com/Richard-YH/KCL-Psychology-Module-Helper', href = 'https://github.com/Richard-YH/KCL-Psychology-Module-Helper')),
        ui.h4("How to contribute"),
        ui.tags.ul(
            ui.tags.li("Report issues and suggest improvements: Contact oliver.runswick@kcl.ac.uk"),
            ui.tags.li("Help improve module data quality and descriptions: Provide your feedback for your modules"),
            ui.tags.li("Contribute code, UI refinements, and analysis features: Apply for our RES program"),
        ),
        ui.h4("Become a member"),
        ui.p(
            "Check the timeline for RES programs which start application every October/February. Details will be made available on:"),
        ui.p("KEATS - BSc Psychology - Extracurricular Forum - Research Experience Scheme (RES)."),
        ui.h4("Contribution list:"),
        ui.p("Supervision: ", ui.tags.a("Dr Oliver Runswick",href = 'https://www.kcl.ac.uk/people/oliver-runswick')),
        ui.p("2025-2026: Aoife Filipe & Yifan Huang"),
    ),
)

admin_panel = ui.nav_panel(
    "Admin",
    ui.page_fluid(
        ui.output_ui("admin_features_ui"),
    ),
)

navbar_panels = [home_panel, join_us_panel]
if SHOW_ADMIN_MENU:
    navbar_panels.append(admin_panel)


def _brand_title() -> ui.Tag:
    if KCL_LOGO_SRC:
        return ui.tags.div(
            {"class": "kmsh-brand"},
            ui.tags.img(
                src=KCL_LOGO_SRC,
                alt="King's College London logo",
                class_="kmsh-logo",
            ),
            ui.tags.span("KingsMSH"),
        )
    return ui.tags.div({"class": "kmsh-brand"}, ui.tags.span("KingsMSH"))


app_ui = ui.page_navbar(
    *navbar_panels,
    title=_brand_title(),
    header=ui.TagList(
        ui.tags.style(CSS),
        ui.tags.script(
            """
            document.addEventListener("dblclick", function (e) {
              const row = e.target.closest("tr.kmsh-module-row");
              if (!row) return;
              const code = row.getAttribute("data-code");
              if (!code) return;
              if (window.Shiny && Shiny.setInputValue) {
                Shiny.setInputValue("module_row_dblclick", code, { priority: "event" });
              }
            });
            """
        ),
        ui.tags.script(
            """
            document.addEventListener("shown.bs.modal", function (e) {
              function resizePlots() {
                const plots = document.querySelectorAll(".kmsh-plotly-skill .plotly-graph-div");
                if (window.Plotly && plots.length) {
                  plots.forEach(function (p) { Plotly.Plots.resize(p); });
                }
              }
              setTimeout(resizePlots, 80);
              setTimeout(resizePlots, 240);
            });
            """
        ),
        ui.tags.script(
            """
            if (window.Shiny && Shiny.addCustomMessageHandler) {
              function locateTextareaCaretTop(textarea, index) {
                const mirror = document.createElement("div");
                const style = window.getComputedStyle(textarea);
                const props = [
                  "boxSizing", "width", "fontFamily", "fontSize", "fontWeight", "fontStyle",
                  "lineHeight", "letterSpacing", "textTransform", "textIndent", "textAlign",
                  "paddingTop", "paddingRight", "paddingBottom", "paddingLeft",
                  "borderTopWidth", "borderRightWidth", "borderBottomWidth", "borderLeftWidth",
                  "whiteSpace", "wordBreak", "overflowWrap", "tabSize"
                ];

                mirror.style.position = "absolute";
                mirror.style.visibility = "hidden";
                mirror.style.top = "0";
                mirror.style.left = "-9999px";
                mirror.style.whiteSpace = "pre-wrap";
                mirror.style.wordBreak = "break-word";
                mirror.style.overflowWrap = "break-word";
                mirror.style.overflow = "hidden";
                props.forEach(function (prop) { mirror.style[prop] = style[prop]; });
                mirror.style.width = textarea.clientWidth + "px";

                const before = textarea.value.slice(0, index);
                mirror.textContent = before;

                const marker = document.createElement("span");
                marker.textContent = textarea.value.slice(index, index + 1) || " ";
                mirror.appendChild(marker);
                document.body.appendChild(mirror);

                const top = marker.offsetTop;
                document.body.removeChild(mirror);
                return top;
              }

              Shiny.addCustomMessageHandler("adjustment_json_locate", function (msg) {
                const textarea = document.getElementById("adjustment_json");
                if (!textarea) return;

                const index = Number(msg && msg.index);
                const length = Number(msg && msg.length) || 1;
                if (!Number.isFinite(index) || index < 0) return;

                textarea.focus();
                textarea.setSelectionRange(index, index + Math.max(length, 1));
                const caretTop = locateTextareaCaretTop(textarea, index);
                const viewportOffset = Math.max(20, Math.floor(textarea.clientHeight * 0.3));
                textarea.scrollTop = Math.max(0, caretTop - viewportOffset);
              });
            }
            """
        ),
    ),
)

# ----------------------------
# Server
# ----------------------------


def server(input: Inputs, output: Outputs, session: Session):
    tmp_dir = Path(tempfile.gettempdir()) / "kingsmsh"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    current_tool = reactive.value("")  # "", "career", "skills", "modules", "analyzer"

    base_modules = reactive.value([])
    effective_modules = reactive.value([])
    adjustment_val = reactive.value({"modules": {}})

    status_msg = reactive.value("")
    template_msg = reactive.value("")
    admin_msg = reactive.value("")

    # tool state
    skills_filtered = reactive.value([])
    career_filtered = reactive.value([])
    analyzer_modules_effective = reactive.value([])
    detail_module: reactive.Value[Optional[Dict[str, Any]]] = reactive.value(None)
    skills_page = reactive.value(1)
    career_page = reactive.value(1)
    analyzer_selected_codes = reactive.value([])

    def _reset_tool_state() -> None:
        skills_filtered.set([])
        career_filtered.set([])
        analyzer_modules_effective.set([])
        analyzer_selected_codes.set([])
        detail_module.set(None)
        skills_page.set(1)
        career_page.set(1)

    # ---- data loading
    def load_active() -> None:
        adj = normalize_adjustment_keys(load_adjustment())
        adjustment_val.set(adj)

        template_path = get_active_template_local_path(tmp_dir)
        if template_path is None:
            base_modules.set([])
            effective_modules.set([])
            _reset_tool_state()
            status_msg.set("No active template available.")
            return

        mods = load_template_modules(str(template_path))
        base_modules.set(mods)
        # "Effective" modules are the uploaded template with adjustment overlay applied.
        effective_modules.set(apply_adjustments(mods, adj))
        _reset_tool_state()
        status_msg.set(f"Loaded active template. Modules: {len(mods)}")

    load_active()

    def _find_by_code(mods: List[Dict[str, Any]], code: str) -> Optional[Dict[str, Any]]:
        code_l = code.strip().lower()
        for m in mods:
            if (m.get("code") or "").strip().lower() == code_l:
                return m
        return None

    def _admin_module_choices() -> Dict[str, str]:
        mods = base_modules.get() or []
        choices: Dict[str, str] = {}
        for m in mods:
            code = (m.get("code") or "").strip()
            if not code:
                continue
            name = (m.get("name") or "").strip()
            choices[f"{code}  {name}"] = code
        return choices


    def _module_btn_id(code: str, name: str, idx: int) -> str:
        # Keep ids deterministic and short; avoids collisions when rendering many cards.
        raw = f"{code}|{name}|{idx}".encode("utf-8")
        h = hashlib.md5(raw).hexdigest()[:10]
        return f"mod_open__{h}"

    def _analyzer_rm_btn_id(code: str) -> str:
        # Same strategy for dynamically generated remove buttons.
        h = hashlib.md5(code.encode("utf-8")).hexdigest()[:10]
        return f"analyzer_rm__{h}"

    def _normalize_module_code(value: Any) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        return raw.split()[0]

    def _module_code_sort_key_for_cards(code: Any) -> Tuple[int, str]:
        """Sort by first numeric digit in code, then full code text."""
        text = str(code or "").strip()
        match = re.search(r"\d", text)
        first_digit = int(match.group(0)) if match else 99
        return first_digit, text.lower()

    # ---- routing from Home
    @reactive.effect
    @reactive.event(input.go_career)
    def _():
        current_tool.set("career")
        career_page.set(1)

    @reactive.effect
    @reactive.event(input.go_skills)
    def _():
        current_tool.set("skills")
        skills_page.set(1)

    @reactive.effect
    @reactive.event(input.go_modules)
    def _():
        current_tool.set("modules")

    @reactive.effect
    @reactive.event(input.go_analyzer)
    def _():
        current_tool.set("analyzer")

    @reactive.effect
    @reactive.event(input.tool_back)
    def _():
        current_tool.set("")

    @reactive.effect
    @reactive.event(input.save_template_btn)
    def _save_template():
        files = input.template_file()
        if not files:
            template_msg.set("No template file uploaded.")
            return

        f0 = files[0]
        try:
            meta = save_template(f0["datapath"])
            template_msg.set(f"Template saved as active: {meta.filename} (sha256={meta.sha256[:12]}...)")
            load_active()
        except StorageError as e:
            template_msg.set(f"Storage error: {e}")
        except Exception as e:
            template_msg.set(f"Save failed: {e}")

    def _refresh_adjustment_textarea() -> None:
        ui.update_text_area("adjustment_json", value=json.dumps(adjustment_val.get(), ensure_ascii=False, indent=2))

    @reactive.effect
    def _init_adjustment_area():
        _refresh_adjustment_textarea()

    @reactive.effect
    @reactive.event(input.adjustment_find_field)
    async def _locate_adjustment_field():
        raw = input.adjustment_json() or ""
        query = (input.adjustment_field_search() or "").strip()
        if not query:
            admin_msg.set("Enter text to search (exact, case-sensitive).")
            return

        idx, match_len = _find_text_exact(raw, query)
        if idx < 0:
            admin_msg.set(f'Exact match not found: "{query}".')
            return

        line, col = _line_col_from_index(raw, idx)
        await session.send_custom_message("adjustment_json_locate", {"index": idx, "length": match_len})
        admin_msg.set(f'Located "{query}" at line {line}, column {col}.')

    @reactive.effect
    @reactive.event(input.admin_save_adjustment)
    def _save_adjustment_json():
        raw = input.adjustment_json() or ""
        try:
            adj = normalize_adjustment_keys(json.loads(raw) if raw.strip() else {"modules": {}})
            save_adjustment(adj)
            adjustment_val.set(adj)
            effective_modules.set(apply_adjustments(base_modules.get(), adj))
            _reset_tool_state()
            admin_msg.set("Adjustment saved.")
        except Exception as e:
            admin_msg.set(f"Save failed: {e}")

    @reactive.effect
    @reactive.event(input.admin_refresh_effective)
    def _refresh_effective():
        load_active()
        _refresh_adjustment_textarea()
        admin_msg.set("Refreshed effective data.")

    @reactive.effect
    def _update_admin_module_choices():
        ui.update_select("admin_module", choices=_admin_module_choices())

    @reactive.effect
    @reactive.event(input.admin_module)
    def _populate_admin_fields():
        code = (input.admin_module() or "").strip()
        if not code:
            return
        adj = adjustment_val.get()
        patch = (adj.get("modules", {}) or {}).get(code, {}) if isinstance(adj, dict) else {}
        bm = _find_by_code(base_modules.get() or [], code) or {}

        ui.update_text("admin_name", value=(patch.get("name") or bm.get("name") or ""))
        ui.update_text("admin_module_leaders", value=(patch.get("module_leaders") or bm.get("module_leaders") or ""))
        ui.update_text("admin_location", value=(patch.get("location") or bm.get("location") or ""))
        ui.update_text_area(
            "admin_information",
            value=_normalize_multiline_text(patch.get("information") or bm.get("information") or ""),
        )
        ui.update_text_area(
            "admin_assessment",
            value=_normalize_multiline_text(patch.get("assessment") or bm.get("assessment") or ""),
        )
        ui.update_text("admin_assessment_type", value=(patch.get("assessment_type") or bm.get("assessment_type") or ""))
        ui.update_text("admin_code", value=(patch.get("code") or bm.get("code") or ""))

    @reactive.effect
    @reactive.event(input.admin_save_module_patch)
    def _admin_save_module_patch():
        code = (input.admin_module() or "").strip()
        if not code:
            admin_msg.set("Select a module first.")
            return

        adj = normalize_adjustment_keys(adjustment_val.get())
        adj.setdefault("modules", {})
        new_code = (input.admin_code() or "").strip()

        patch: Dict[str, Any] = {}
        raw_fields = {
            "name": input.admin_name(),
            "module_leaders": input.admin_module_leaders(),
            "location": input.admin_location(),
            "information": input.admin_information(),
            "assessment": input.admin_assessment(),
        }
        for key, value in raw_fields.items():
            text = str(value or "")
            if text.strip():
                patch[key] = text

        assessment_type = normalize_assessment_type(input.admin_assessment_type())
        if assessment_type:
            patch["assessment_type"] = assessment_type
        if new_code:
            patch["code"] = new_code
        if not patch:
            admin_msg.set("No non-empty fields to save.")
            return

        adj["modules"].setdefault(code, {})
        adj["modules"][code].update(patch)
        try:
            save_adjustment(adj)
            adjustment_val.set(adj)
            effective_modules.set(apply_adjustments(base_modules.get(), adj))
            _reset_tool_state()
            _refresh_adjustment_textarea()
            admin_msg.set(f"Saved changes for {code}.")
        except Exception as e:
            admin_msg.set(f"Save failed: {e}")

    # ----------------------------
    # Module detail modal (read-only for all users)
    # ----------------------------

    def _open_detail_for_code(code: str) -> None:
        mod = _find_by_code(effective_modules.get() or [], code)
        if not mod:
            status_msg.set("Module not found.")
            return
        detail_module.set(mod)
        ui.modal_show(
            ui.modal(ui.output_ui("detail_modal_ui"), title="Module details", easy_close=True, size="l", id="detail_modal")
        )

    @output
    @render.ui
    def detail_modal_ui():
        mod = detail_module.get()
        if not mod:
            return ui.p("No module selected.")
        code = (mod.get("code") or "").strip()
        name = (mod.get("name") or "").strip()
        info = _normalize_multiline_text(mod.get("information") or "")
        assessment = _normalize_multiline_text(mod.get("assessment") or "")
        atype = mod.get("assessment_type") or ""
        module_leaders = mod.get("module_leaders") or ""
        location = mod.get("location") or ""
        location_url = _location_map_url(location)

        return ui.TagList(
            ui.h4(f"{code} - {name}"),
            ui.p(f"Assessment type: {atype}" if atype else "Assessment type: (not set)"),
            ui.p("Module leader(s): ", _module_leaders_display(module_leaders)),
            ui.p(
                "Location: ",
                ui.tags.a(
                    location,
                    href=location_url,
                    target="_blank",
                    rel="noopener noreferrer",
                )
                if location and location_url
                else (location if location else "(not set)"),
            ),
            ui.h5("Information"),
            ui.div(info if info else "(empty)", class_="kmsh-multiline-text kmsh-section-gap"),
            ui.h5("Assessment"),
            ui.div(assessment if assessment else "(empty)", class_="kmsh-multiline-text"),
            ui.hr(),
            ui.card(
                ui.card_header("Careers"),
                ui.div(ui.output_plot("detail_careers_plot", height="360px"), class_="kmsh-career-plot"),
                class_="kmsh-career-card",
            ),
            ui.hr(),
            ui.card(ui.card_header("Skills"), ui.output_ui("detail_skills_plotly")),
        )


    # ---- Dynamic observers for module "Open details" buttons
    _module_btn_observers: Dict[str, Any] = {}
    _analyzer_rm_observers: Dict[str, Any] = {}

    def _cleanup_stale_observers(store: Dict[str, Any], valid_ids: set[str]) -> None:
        # Remove observers for controls that no longer exist in the current UI state.
        stale_ids = [oid for oid in store.keys() if oid not in valid_ids]
        for oid in stale_ids:
            obs = store.pop(oid, None)
            if obs is None:
                continue
            try:
                obs.destroy()
            except Exception:
                # Best-effort cleanup; stale observers are already detached from the map.
                pass

    @reactive.effect
    def _ensure_module_btn_observers():
        mods = effective_modules.get() or []
        valid_ids: set[str] = set()
        for idx, m in enumerate(mods):
            code = (m.get("code") or "").strip()
            name = (m.get("name") or "").strip()
            if not code:
                continue
            btn_id = _module_btn_id(code, name, idx)
            valid_ids.add(btn_id)
            if btn_id in _module_btn_observers:
                continue

            def _make_handler(_btn_id: str, _code: str):
                # Factory binds loop values so each observer targets the correct module.
                @reactive.effect
                @reactive.event(input[_btn_id])
                def _handler():
                    if current_tool.get() != "modules":
                        return
                    _open_detail_for_code(_code)
                return _handler

            _module_btn_observers[btn_id] = _make_handler(btn_id, code)
        _cleanup_stale_observers(_module_btn_observers, valid_ids)

    @reactive.effect
    def _ensure_analyzer_rm_observers():
        valid_ids: set[str] = set()
        for code in (analyzer_selected_codes.get() or []):
            norm = _normalize_module_code(code)
            if not norm:
                continue
            btn_id = _analyzer_rm_btn_id(norm)
            valid_ids.add(btn_id)
            if btn_id in _analyzer_rm_observers:
                continue

            def _make_rm_handler(_btn_id: str, _code: str):
                # Bind id/code once to avoid Python late-binding pitfalls in loops.
                @reactive.effect
                @reactive.event(input[_btn_id])
                def _handler():
                    analyzer_selected_codes.set(
                        [_normalize_module_code(c) for c in (analyzer_selected_codes.get() or []) if _normalize_module_code(c) != _code]
                    )
                return _handler

            _analyzer_rm_observers[btn_id] = _make_rm_handler(btn_id, norm)
        _cleanup_stale_observers(_analyzer_rm_observers, valid_ids)

    # ----------------------------
    # Modules tool: card click == radio selection change (A)
    # ----------------------------

    # ----------------------------
    # Tools: Skills / Career / Analyzer
    # ----------------------------

    @reactive.effect
    @reactive.event(input.skills_apply)
    def _skills_apply():
        skills_page.set(1)
        mods = effective_modules.get() or []
        keywords = parse_keywords(input.skills_keywords() or "")
        atype_filter = input.skills_assessment_type()
        q = (input.skills_search() or "").strip().lower()

        out = []
        for m in mods:
            code = (m.get("code") or "").strip()
            name = (m.get("name") or "").strip()
            if q and q not in code.lower() and q not in name.lower():
                continue
            mt = normalize_assessment_type(m.get("assessment_type"))
            if atype_filter and atype_filter != "(Any)" and mt != atype_filter:
                continue
            strength = get_skill_score_sum(m, keywords) if keywords else 0
            if keywords and strength <= 0:
                continue
            out.append((strength, m))

        out.sort(key=lambda x: x[0], reverse=True)
        skills_filtered.set([m for _, m in out])

    @reactive.effect
    @reactive.event(input.skills_check_all)
    def _skills_check_all():
        mods = effective_modules.get() or []
        acc: Dict[str, Dict[str, Any]] = {}
        for m in mods:
            code = (m.get("code") or "").strip()
            for s in (m.get("skills", []) or []):
                label = str(s.get("label") or s.get("skill") or s.get("full_label") or "").strip()
                if not label:
                    continue
                category = _normalize_skill_category(str(s.get("category") or ""))
                score = int(s.get("score") or 0)
                if label not in acc:
                    acc[label] = {
                        "skill": label,
                        "category": category,
                        "total_score": 0,
                        "_modules": set(),
                    }
                acc[label]["total_score"] += score
                if code:
                    acc[label]["_modules"].add(code)

        category_spec = [
            ("Foundational Skill", "Foundational", "foundational"),
            ("Communication", "Communication", "communication"),
            ("Leadership", "Leadership", "leadership"),
            ("Adaptability", "Adaptability", "adaptability"),
            ("Creativity", "Creativity", "creativity"),
            ("Digital Competency", "Digital Competency", "digital"),
        ]
        grouped: Dict[str, List[Dict[str, Any]]] = {name: [] for name, _, _ in category_spec}
        grouped["Other"] = []

        for item in acc.values():
            entry = {
                "skill": item["skill"],
                "module_count": len(item["_modules"]),
                "total_score": item["total_score"],
            }
            cat = item.get("category") or "Other"
            if cat not in grouped:
                cat = "Other"
            grouped[cat].append(entry)

        total_count = 0
        for cat in grouped:
            grouped[cat].sort(key=lambda r: (-int(r["total_score"]), str(r["skill"]).lower()))
            total_count += len(grouped[cat])

        if total_count == 0:
            content = ui.p("No skills found in current template.")
        else:
            def _skill_column(title: str, slug: str, items: List[Dict[str, Any]]):
                nodes = [ui.tags.div({"class": "kmsh-skill-item"}, str(r.get("skill") or "")) for r in items]
                if not nodes:
                    nodes = [ui.tags.div({"class": "kmsh-skill-item"}, "No skills in this category.")]
                return ui.tags.div(
                    {"class": f"kmsh-skill-col kmsh-skill-col--{slug}"},
                    ui.tags.div(
                        {"class": "kmsh-skill-col-top"},
                        ui.tags.div(
                            {"class": "kmsh-skill-icon-wrap"},
                            ui.tags.div({"class": "kmsh-skill-icon"}),
                        ),
                    ),
                    ui.tags.div({"class": "kmsh-skill-title"}, title),
                    ui.tags.div({"class": "kmsh-skill-list"}, *nodes),
                )

            columns = []
            for cat_name, display_name, slug in category_spec:
                columns.append(_skill_column(display_name, slug, grouped.get(cat_name, [])))
            if grouped.get("Other"):
                columns.append(_skill_column("Other", "other", grouped["Other"]))
            content = ui.tags.div({"class": "kmsh-skill-showcase"}, *columns)

        ui.modal_show(
            ui.modal(
                content,
                title="All Skills",
                easy_close=True,
                size="l",
                id="skills_modal",
            )
        )

    @reactive.effect
    @reactive.event(input.skills_prev_page)
    def _skills_prev_page():
        skills_page.set(max(1, skills_page.get() - 1))

    @reactive.effect
    @reactive.event(input.skills_next_page)
    def _skills_next_page():
        total_rows = len(_filtered_skill_rows())
        total_pages = max(1, math.ceil(total_rows / SKILLS_PAGE_SIZE))
        skills_page.set(min(total_pages, skills_page.get() + 1))

    @reactive.effect
    @reactive.event(input.career_apply)
    def _career_apply():
        career_page.set(1)
        mods = effective_modules.get() or []
        selected = input.career_paths() or []

        out = []
        for m in mods:
            code = (m.get("code") or "").strip()
            name = (m.get("name") or "").strip()
            score = sum(get_career_score(m, p) for p in selected) if selected else 0
            out.append((score, m))

        out.sort(key=lambda x: x[0], reverse=True)
        career_filtered.set([m for _, m in out])

    @reactive.effect
    @reactive.event(input.career_explain)
    def _career_explain():
        items: List[ui.Tag] = []
        for pathway in CAREER_PATHWAYS_DEFAULT:
            desc = CAREER_PATHWAY_EXPLANATIONS.get(pathway, "")
            items.append(
                ui.tags.li(
                    ui.tags.strong(f"{pathway}: "),
                    desc,
                )
            )

        ui.modal_show(
            ui.modal(
                ui.tags.ul(*items),
                title="Explanation of each pathway",
                easy_close=True,
                size="l",
                id="career_pathway_explain_modal",
            )
        )

    @reactive.effect
    @reactive.event(input.career_prev_page)
    def _career_prev_page():
        career_page.set(max(1, career_page.get() - 1))

    @reactive.effect
    @reactive.event(input.career_next_page)
    def _career_next_page():
        total_rows = len(_filtered_career_rows())
        total_pages = max(1, math.ceil(total_rows / SKILLS_PAGE_SIZE))
        career_page.set(min(total_pages, career_page.get() + 1))

    @reactive.effect
    @reactive.event(input.analyzer_apply)
    def _analyzer_apply():
        # Resolve selected codes into full module records for downstream charts.
        codes = [_normalize_module_code(c) for c in (analyzer_selected_codes.get() or [])]
        mods = []
        for c in codes:
            if not c:
                continue
            m = _find_by_code(effective_modules.get() or [], c)
            if m:
                mods.append(m)
        analyzer_modules_effective.set(mods)

    @reactive.effect
    @reactive.event(input.analyzer_add_module)
    def _analyzer_add_module():
        code = _normalize_module_code(input.analyzer_search_pick())
        if not code:
            choices = _analyzer_match_choices()
            if choices:
                # Fall back to first match when select input has no explicit value yet.
                code = _normalize_module_code(next(iter(choices.values())))
        if not code:
            status_msg.set("No matching module to add.")
            return
        current = [_normalize_module_code(c) for c in (analyzer_selected_codes.get() or [])]
        if code not in current:
            current.append(code)
            analyzer_selected_codes.set(current)

    @reactive.effect
    @reactive.event(input.analyzer_clear_selected)
    def _analyzer_clear_selected():
        analyzer_selected_codes.set([])

    @reactive.effect
    @reactive.event(input.module_row_dblclick)
    def _module_row_dblclick():
        code = (input.module_row_dblclick() or "").strip()
        if not code:
            return
        _open_detail_for_code(code)

    # ----------------------------
    # Home/tool dynamic UI
    # ----------------------------

    def _tool_header(title: str) -> ui.Tag:
        return ui.div(
            {"class": "kmsh-toolbar"},
            ui.input_action_button("tool_back", "Back to Home", class_="kmsh-btn"),
            ui.h4(title),
        )

    @output
    @render.ui
    def home_or_tool():
        tool = current_tool.get()

        if not tool:
            return ui.TagList(
                ui.h3("What is KingsMSH?"),
                ui.p("KingsMSH (Module Selection Helper) is a open-source tool aims to help Psychology students choose/analyse their modules."),
                ui.p("This website is maintained by students as a part of RES project."),
                ui.p("If you are interested in contributing, please go to the Join us tab."),
                ui.h3("Why KingsMSH?"),
                ui.p("•Clear demonstration of module info"),
                ui.p("•Multiple sort functions allowed"),
                ui.p("•Visualised data"),
                ui.p("•Give you a better understanding on what you have got from your degree program"),
                ui.h3("Let's get started:"),
                ui.div(
                    {"class": "kmsh-home-grid"},
                    ui.div(
                        {"class": "kmsh-home-card"},
                        ui.h5("A career path"),
                        ui.p("Select a career pathway and see best-fit modules."),
                        ui.input_action_button("go_career", "Open"),
                    ),
                    ui.div(
                        {"class": "kmsh-home-card"},
                        ui.h5("Skills training"),
                        ui.p("Filter modules by skill keyword(s) and assessment type."),
                        ui.input_action_button("go_skills", "Open"),
                    ),
                    ui.div(
                        {"class": "kmsh-home-card"},
                        ui.h5("All available modules"),
                        ui.p("Browse modules and view details."),
                        ui.input_action_button("go_modules", "Open"),
                    ),
                    ui.div(
                        {"class": "kmsh-home-card"},
                        ui.h5("Analyse my modules"),
                        ui.p("Select modules and see skills/career aggregates."),
                        ui.input_action_button("go_analyzer", "Open"),
                    ),
                ),
            )

        if tool == "modules":
            return ui.TagList(
                _tool_header("All modules"),
                ui.div(
                    {"class": "kmsh-mod-search"},
                    ui.tags.span("Search (code/name)"),
                    ui.input_text("modules_search", "", placeholder="e.g. 6PAH / psychology"),
                    ui.div({"class": "kmsh-mod-refresh"}, ui.input_action_button("modules_refresh", "Refresh")),
                ),
                ui.br(),
                ui.output_ui("modules_cards_ui"),
            )

        if tool == "skills":
            return ui.TagList(
                _tool_header("Skills filter"),
                ui.br(),
                ui.input_text_area("skills_keywords", "Skill keyword(s) (comma-separated)", rows=2, placeholder="e.g. critical, communication"),
                ui.input_select("skills_assessment_type", "Assessment type", choices=["(Any)", "Coursework", "Exam", "Hybrid"], selected="(Any)"),
                ui.input_text("skills_search", "Search (code/name)", placeholder="Optional"),
                ui.div(
                    {"class": "kmsh-analyzer-actions kmsh-skills-actions"},
                    ui.input_action_button("skills_apply", "Apply", class_="kmsh-btn"),
                    ui.input_action_button("skills_check_all", "Click here to show all skills", class_="kmsh-btn"),
                ),
                ui.output_ui("skills_table"),
                ui.output_ui("skills_pagination"),
            )

        if tool == "career":
            return ui.TagList(
                _tool_header("Career pathway"),
                ui.br(),
                ui.div(
                    {"class": "kmsh-career-paths"},
                    ui.input_checkbox_group(
                        "career_paths",
                        "Select pathway(s)",
                        choices=CAREER_PATHWAYS_DEFAULT,
                        selected=[CAREER_PATHWAYS_DEFAULT[0]],
                    ),
                ),
                ui.div(
                    {"class": "kmsh-analyzer-actions kmsh-skills-actions"},
                    ui.input_action_button("career_apply", "Apply", class_="kmsh-btn"),
                    ui.input_action_button(
                        "career_explain",
                        "Click here for explanation of each pathway",
                        class_="kmsh-btn",
                    ),
                ),
                ui.output_ui("career_table"),
                ui.output_ui("career_pagination"),
            )

        if tool == "analyzer":
            return ui.TagList(
                _tool_header("Analyse my modules"),
                ui.p("Search modules by code/name, add them to the selection list, then click the analyse button."),
                ui.input_text("analyzer_query", "Search module (code/name)", placeholder="e.g. 6PAH / neuroscience"),
                ui.output_ui("analyzer_search_pick_ui"),
                ui.div(
                    {"class": "kmsh-analyzer-actions"},
                    ui.input_action_button("analyzer_add_module", "Add module", class_="kmsh-btn"),
                    ui.input_action_button("analyzer_clear_selected", "Clear list", class_="kmsh-btn"),
                ),
                ui.br(),
                ui.output_ui("analyzer_selected_table_ui"),
                ui.layout_columns(ui.input_action_button("analyzer_apply", "Analyse", class_="kmsh-btn"), col_widths=(2,)),
                ui.hr(),
                ui.layout_columns(
                    ui.card(ui.card_header("Skills"), ui.output_ui("analyzer_skills_plot")),
                    ui.card(ui.card_header("Career pathways"), ui.output_plot("analyzer_careers_plot")),
                    col_widths=(6, 6),
                ),
            )

        return ui.p("Unknown tool.")

    # ----------------------------
    # Tool actions
    # ----------------------------

    @reactive.effect
    @reactive.event(input.modules_refresh)
    def _modules_refresh():
        load_active()

    # ----------------------------
    # Outputs
    # ----------------------------

    @output
    @render.text
    def status_msg_out():
        return status_msg.get()

    @output
    @render.text
    def template_status():
        return template_msg.get()

    @output
    @render.text
    def admin_save_status():
        return admin_msg.get()

    @output
    @render.ui
    def admin_features_ui():
        return ui.TagList(
            ui.layout_columns(
                ui.card(
                    ui.card_header("Template management"),
                    ui.p("Upload a template file and set it as active on the server."),
                    ui.input_file("template_file", "Upload template", accept=[".csv", ".xlsx", ".xls"]),
                    ui.input_action_button("save_template_btn", "Save as active template"),
                    ui.output_text_verbatim("template_status"),
                ),
                col_widths=(8,),
            ),
            ui.hr(),
            ui.h4("Module detail editor"),
            ui.p("Edits are stored in adjustment.json and applied as an overlay on top of the template."),
            ui.layout_columns(
                ui.card(
                    ui.card_header("Select module"),
                    ui.input_select("admin_module", "Module", choices=_admin_module_choices()),
                    ui.input_text("admin_code", "Code"),
                    ui.input_text("admin_name", "Name"),
                    ui.input_text("admin_module_leaders", "Module leader(s) (supports [Name](url) or Name|url)"),
                    ui.input_text("admin_location", "Location"),
                    ui.input_text_area("admin_information", "Information", rows=5),
                    ui.input_text_area("admin_assessment", "Assessment", rows=5),
                    ui.input_text("admin_assessment_type", "Assessment type (Coursework/Exam/Hybrid)"),
                    ui.layout_columns(
                        ui.input_action_button("admin_save_module_patch", "Save module changes"),
                        col_widths=(4,),
                    ),
                    ui.output_text_verbatim("admin_save_status"),
                ),
                ui.card(
                    ui.card_header("Adjustment JSON (read/write)"),
                    ui.div(
                        {"class": "kmsh-adjustment-search"},
                        ui.input_text(
                            "adjustment_field_search",
                            "Search field",
                            placeholder="Exact, case-sensitive text (e.g. \"assessment_type\")",
                        ),
                        ui.input_action_button("adjustment_find_field", "Search & locate"),
                    ),
                    ui.input_text_area(
                        "adjustment_json",
                        "",
                        rows=22,
                        placeholder='{"modules": {"<code>": {"module_leaders": "[Jane Doe](https://example.com) ; John Smith|https://example.com", "location": "...", "information": "...", "assessment": "...", "assessment_type": "..."}}}',
                    ),
                    ui.layout_columns(
                        ui.input_action_button("admin_save_adjustment", "Save adjustment JSON"),
                        ui.input_action_button("admin_refresh_effective", "Refresh effective data"),
                        col_widths=(4, 4),
                    ),
                ),
                col_widths=(5, 7),
            ),
        )

    @output
    @render.ui
    def analyzer_search_pick_ui():
        choices = _analyzer_match_choices()
        if not choices:
            return ui.p("No modules match your search.")

        return ui.input_select(
            "analyzer_search_pick",
            "Matching modules",
            choices=choices,
            selected=next(iter(choices.values())),
        )

    @output
    @render.ui
    def analyzer_selected_table_ui():
        selected_codes = [_normalize_module_code(c) for c in (analyzer_selected_codes.get() or [])]
        if not selected_codes:
            return ui.p("Selected modules: none.")

        rows: List[ui.Tag] = []
        for code in selected_codes:
            m = _find_by_code(effective_modules.get() or [], code)
            if not m:
                continue
            btn_id = _analyzer_rm_btn_id(code)
            rows.append(
                ui.tags.tr(
                    ui.tags.td(code),
                    ui.tags.td((m.get("name") or "").strip()),
                    ui.tags.td(ui.input_action_button(btn_id, "Remove", class_="btn btn-sm btn-outline-secondary")),
                )
            )

        if not rows:
            return ui.p("Selected modules: none.")

        header = ui.tags.thead(ui.tags.tr(ui.tags.th("Code"), ui.tags.th("Name"), ui.tags.th("Action")))
        body = ui.tags.tbody(*rows)
        return ui.TagList(
            ui.h5("Selected modules list"),
            ui.tags.table({"class": "table table-striped table-sm"}, header, body),
        )

    # ---- Modules cards UI (stable Shiny approach: radio inputs styled as cards)
    @output
    @render.ui
    def modules_cards_ui():
        mods = effective_modules.get() or []
        q = (input.modules_search() or "").strip().lower()

        filtered: List[Tuple[int, Dict[str, Any]]] = []
        for idx, m in enumerate(mods):
            code = (m.get("code") or "").strip()
            name = (m.get("name") or "").strip()
            if q and q not in code.lower() and q not in name.lower():
                continue
            filtered.append((idx, m))

        filtered.sort(key=lambda item: _module_code_sort_key_for_cards(item[1].get("code")))

        if not filtered:
            return ui.p("No modules match your search.")

        cards: List[ui.Tag] = []
        for idx, m in filtered:
            code = (m.get("code") or "").strip()
            name = (m.get("name") or "").strip()

            btn_id = _module_btn_id(code, name, idx)
            cards.append(
                ui.div(
                    {"class": "kmsh-mod-card"},
                    ui.card(
                        ui.card_header(ui.tags.div({"class": "kmsh-mod-code"}, code)),
                        ui.tags.div(
                            {"class": "kmsh-mod-body"},
                            ui.tags.div({"class": "kmsh-mod-name"}, name),
                            ui.tags.div({"class": "kmsh-spacer"}),
                            ui.input_action_button(btn_id, "Open details", class_="kmsh-btn-full"),
                        ),
                    ),
                )
            )

        try:
            return ui.layout_column_wrap(*cards, width="300px")
        except Exception:
            return ui.div(*cards)

    def _simple_table(rows: List[Dict[str, Any]], columns: List[Tuple[str, str]], empty_msg: str):
        if not rows:
            return ui.p(empty_msg)
        header = ui.tags.thead(ui.tags.tr(*[ui.tags.th(label) for _, label in columns]))
        body_rows = []
        for row in rows:
            body_rows.append(ui.tags.tr(*[ui.tags.td(row.get(key, "")) for key, _ in columns]))
        body = ui.tags.tbody(*body_rows)
        return ui.tags.table({"class": "table table-striped table-sm"}, header, body)

    def _analyzer_match_choices() -> Dict[str, str]:
        mods = effective_modules.get() or []
        q = (input.analyzer_query() or "").strip().lower()
        choices: Dict[str, str] = {}
        for m in mods:
            code = (m.get("code") or "").strip()
            name = (m.get("name") or "").strip()
            if not code:
                continue
            if q and q not in code.lower() and q not in name.lower():
                continue
            choices[f"{code}  {name}"] = code
            if len(choices) >= 60:
                break
        return choices

    def _filtered_skill_rows() -> List[Dict[str, Any]]:
        mods = skills_filtered.get() or []
        if not mods:
            # Before "Apply", derive rows directly from current effective modules.
            mods = effective_modules.get() or []
        keywords = parse_keywords(input.skills_keywords() or "")
        q = (input.skills_search() or "").strip().lower()
        atype_filter = input.skills_assessment_type()

        rows: List[Dict[str, Any]] = []
        for m in mods:
            code = (m.get("code") or "").strip()
            name = (m.get("name") or "").strip()
            if q and q not in code.lower() and q not in name.lower():
                continue
            mt = normalize_assessment_type(m.get("assessment_type"))
            if atype_filter and atype_filter != "(Any)" and mt != atype_filter:
                continue
            strength = get_skill_score_sum(m, keywords) if keywords else 0
            rows.append({"code": code, "name": name, "assessment_type": mt, "skill_strength": strength})
        return rows

    def _filtered_career_rows() -> List[Dict[str, Any]]:
        mods = career_filtered.get() or []
        if not mods:
            mods = effective_modules.get() or []
        selected = input.career_paths() or []

        rows: List[Dict[str, Any]] = []
        for m in mods:
            code = (m.get("code") or "").strip()
            name = (m.get("name") or "").strip()
            score = sum(get_career_score(m, p) for p in selected) if selected else 0
            rows.append({"code": code, "name": name, "career_fit": score})
        return rows

    # ---- Skills table
    @output
    @render.ui
    def skills_pagination():
        total_rows = len(_filtered_skill_rows())
        if total_rows == 0:
            return ui.p("")
        total_pages = max(1, math.ceil(total_rows / SKILLS_PAGE_SIZE))
        page = min(max(1, skills_page.get()), total_pages)
        return ui.div(
            {"class": "kmsh-pager"},
            ui.input_action_button("skills_prev_page", "Previous", class_="kmsh-btn"),
            ui.input_action_button("skills_next_page", "Next", class_="kmsh-btn"),
            ui.tags.span(f"Page {page} / {total_pages}"),
            ui.tags.span(f"({total_rows} modules)"),
        )

    # ---- Skills table
    @output
    @render.ui
    def skills_table():
        rows = _filtered_skill_rows()
        total_pages = max(1, math.ceil(len(rows) / SKILLS_PAGE_SIZE))
        page = min(max(1, skills_page.get()), total_pages)
        start = (page - 1) * SKILLS_PAGE_SIZE
        end = start + SKILLS_PAGE_SIZE
        paged_rows = rows[start:end]

        columns = [
            ("code", "Code"),
            ("name", "Name"),
            ("assessment_type", "Assessment type"),
            ("skill_strength", "Skill strength"),
        ]
        if not paged_rows:
            return ui.p("No modules match your filters.")
        header = ui.tags.thead(ui.tags.tr(*[ui.tags.th(label) for _, label in columns]))
        body_rows = []
        for row in paged_rows:
            body_rows.append(
                ui.tags.tr(
                    {"class": "kmsh-table-row kmsh-module-row", "data-code": row.get("code", "")},
                    *[ui.tags.td(row.get(key, "")) for key, _ in columns],
                )
            )
        body = ui.tags.tbody(*body_rows)
        return ui.tags.table({"class": "table table-striped table-sm"}, header, body)

    # ---- Career table
    @output
    @render.ui
    def career_pagination():
        total_rows = len(_filtered_career_rows())
        if total_rows == 0:
            return ui.p("")
        total_pages = max(1, math.ceil(total_rows / SKILLS_PAGE_SIZE))
        page = min(max(1, career_page.get()), total_pages)
        return ui.div(
            {"class": "kmsh-pager"},
            ui.input_action_button("career_prev_page", "Previous", class_="kmsh-btn"),
            ui.input_action_button("career_next_page", "Next", class_="kmsh-btn"),
            ui.tags.span(f"Page {page} / {total_pages}"),
            ui.tags.span(f"({total_rows} modules)"),
        )

    # ---- Career table
    @output
    @render.ui
    def career_table():
        rows = _filtered_career_rows()

        if not rows:
            return ui.p("No modules match your filters.")

        total_pages = max(1, math.ceil(len(rows) / SKILLS_PAGE_SIZE))
        page = min(max(1, career_page.get()), total_pages)
        start = (page - 1) * SKILLS_PAGE_SIZE
        end = start + SKILLS_PAGE_SIZE
        paged_rows = rows[start:end]

        columns = [("code", "Code"), ("name", "Name"), ("career_fit", "Career fit")]
        header = ui.tags.thead(ui.tags.tr(*[ui.tags.th(label) for _, label in columns]))
        body_rows = []
        for row in paged_rows:
            body_rows.append(
                ui.tags.tr(
                    {"class": "kmsh-table-row kmsh-module-row", "data-code": row.get("code", "")},
                    *[ui.tags.td(row.get(key, "")) for key, _ in columns],
                )
            )
        body = ui.tags.tbody(*body_rows)
        return ui.tags.table({"class": "table table-striped table-sm"}, header, body)

    # ---- Detail modal tables
    def _normalize_skill_category(cat: str) -> str:
        if cat == "Foundational Skills":
            return "Foundational Skill"
        if cat in ("Communication", "Leadership", "Adaptability", "Creativity", "Digital Competency"):
            return cat
        return "Other"

    def _aggregate_skills(mods: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        # Merge duplicate skill labels across modules by summing their scores.
        acc: Dict[str, Dict[str, Any]] = {}
        for m in mods:
            for s in (m.get("skills", []) or []):
                label = str(s.get("label") or s.get("skill") or s.get("full_label") or "").strip()
                if not label:
                    continue
                category = str(s.get("category") or "")
                score = int(s.get("score") or 0)
                if label not in acc:
                    acc[label] = {"label": label, "category": category, "score": 0}
                acc[label]["score"] += score
        return list(acc.values())

    def _aggregate_careers(mods: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        # Same aggregation pattern for career totals.
        acc: Dict[str, int] = {}
        for m in mods:
            for c in (m.get("careers", []) or []):
                label = str(c.get("career") or "").strip()
                if not label:
                    continue
                acc[label] = acc.get(label, 0) + int(c.get("score") or 0)
        return [{"career": k, "score": v} for k, v in acc.items()]

    def _skills_plotly_ui(
        skills: List[Dict[str, Any]],
        empty_message: str,
        empty_height_px: int = 208,
        plot_height_px: int = 208,
    ):
        """Render a Plotly bar chart for skills, or an empty placeholder message."""
        import plotly.express as px
        import plotly.io as pio

        if not skills:
            return ui.div({"class": "kmsh-empty-plot", "style": f"min-height:{empty_height_px}px;"}, empty_message)

        labels = [str(s.get("label") or s.get("skill") or "") for s in skills]
        values = [int(s.get("score") or 0) for s in skills]
        categories = [_normalize_skill_category(str(s.get("category") or "")) for s in skills]
        if not any(values):
            return ui.div({"class": "kmsh-empty-plot", "style": f"min-height:{empty_height_px}px;"}, "No scores available.")

        # Plotly hides zero-height bars, so use a tiny display value while showing true score in hover.
        display_values = [v if v > 0 else 0.1 for v in values]
        color_map = {
            "Foundational Skill": "#1f77b4",
            "Communication": "#ff7f0e",
            "Leadership": "#2ca02c",
            "Adaptability": "#d62728",
            "Creativity": "#9467bd",
            "Digital Competency": "#8c564b",
            "Other": "#111111",
        }
        plot_data = {
            "label": labels,
            "display_score": display_values,
            "score": values,
            "category": categories,
        }
        fig = px.bar(
            plot_data,
            x="label",
            y="display_score",
            color="category",
            color_discrete_map=color_map,
            custom_data=["score"],
        )
        fig.update_traces(
            hovertemplate="%{x}<br>Score: %{customdata[0]}<extra></extra>",
        )
        fig.update_xaxes(visible=False, showticklabels=False)
        fig.update_yaxes(title="Score")
        fig.update_layout(
            margin=dict(l=10, r=10, t=10, b=10),
            uniformtext_minsize=8,
            uniformtext_mode="hide",
            height=plot_height_px,
            paper_bgcolor="white",
            plot_bgcolor="white",
            showlegend=True,
            legend_title_text="",
        )
        return ui.div(
            {"class": "kmsh-plotly-skill"},
            ui.HTML(pio.to_html(fig, include_plotlyjs=True, full_html=False, config={"displayModeBar": False})),
        )

    def _render_careers_radar(ax, fig, careers: List[Dict[str, Any]]):
        """Draw radar chart for career scores and adjust long labels for readability."""
        import matplotlib.transforms as mtransforms
        import numpy as np

        labels = [str(c.get("career") or "") for c in careers]
        values = [int(c.get("score") or 0) for c in careers]
        if not labels or not any(values):
            ax.text(
                0.05,
                0.5,
                "No scores available.",
                transform=ax.transAxes,
                fontfamily="Segoe UI",
                fontsize=12,
                color="#1f2933",
            )
            ax.axis("off")
            return

        # Repeat first point to close the radar polygon.
        values_loop = values + [values[0]]
        angles = np.linspace(0, 2 * np.pi, len(values_loop), endpoint=True)

        ax.plot(angles, values_loop, color="#1f77b4", linewidth=2)
        ax.fill(angles, values_loop, color="#1f77b4", alpha=0.2)
        label_overrides = {
            "Clinical psychology or psychotherapy": "Clinical psychology or\npsychotherapy",
            "Health and social care": "Health and\nsocial care",
            "Business and wider industry roles": "Business and wider\nindustry roles",
        }
        display_labels = [label_overrides.get(l, l) for l in labels]
        ax.set_thetagrids(np.degrees(angles[:-1]), display_labels, fontsize=11)
        ax.tick_params(axis="x", pad=15)
        for tick, theta in zip(ax.get_xticklabels(), angles[:-1]):
            tick.set_clip_on(False)
            tick.set_fontfamily("Segoe UI")
            if tick.get_text() == "Clinical psychology or\npsychotherapy":
                tick.set_horizontalalignment("right")
                x, y = tick.get_position()
                tick.set_position((x - 0.02, y))
            if tick.get_text() in (
                "Academic",
                "Health and\nsocial care",
                "Business and wider\nindustry roles",
            ):
                # Nudge selected labels radially outward to reduce overlap with axes.
                dx_pts = 9.0 * float(np.cos(theta))
                dy_pts = 9.0 * float(np.sin(theta))
                offset = mtransforms.ScaledTranslation(dx_pts / 72.0, dy_pts / 72.0, fig.dpi_scale_trans)
                tick.set_transform(tick.get_transform() + offset)
        ax.set_ylim(0, max(values_loop) if max(values_loop) > 0 else 1)
        ax.set_yticklabels([])
        fig.subplots_adjust(left=0.17, right=0.83, top=0.85, bottom=0.22)

    @output
    @render.ui
    def detail_skills_plotly():
        mod = detail_module.get()
        skills = [] if not mod else (mod.get("skills", []) or [])
        return _skills_plotly_ui(skills, "No skills available.")

    @output
    @render.plot
    def detail_careers_plot():
        import matplotlib.pyplot as plt

        _apply_matplotlib_style()
        mod = detail_module.get()
        fig, ax = plt.subplots(subplot_kw={"polar": True})

        if not mod:
            ax.text(
                0.05,
                0.5,
                "No careers available.",
                transform=ax.transAxes,
                fontfamily="Segoe UI",
                fontsize=12,
                color="#1f2933",
            )
            ax.axis("off")
            return fig

        careers = mod.get("careers", []) or []
        if not careers:
            ax.text(
                0.05,
                0.5,
                "No careers available.",
                transform=ax.transAxes,
                fontfamily="Segoe UI",
                fontsize=12,
                color="#1f2933",
            )
            ax.axis("off")
            return fig

        _render_careers_radar(ax, fig, careers)
        return fig

    # ---- Analyzer plots
    @output
    @render.ui
    def analyzer_skills_plot():
        mods = analyzer_modules_effective.get() or []
        if not mods:
            return _skills_plotly_ui(
                [],
                "Select module(s) and click Analyse.",
                empty_height_px=400,
                plot_height_px=400,
            )
        return _skills_plotly_ui(
            _aggregate_skills(mods),
            "No skills available.",
            empty_height_px=400,
            plot_height_px=400,
        )

    @output
    @render.plot
    def analyzer_careers_plot():
        import matplotlib.pyplot as plt

        _apply_matplotlib_style()
        mods = analyzer_modules_effective.get() or []
        fig, ax = plt.subplots(subplot_kw={"polar": True})

        if not mods:
            ax.text(
                0.5,
                0.5,
                "Select module(s) and click Analyse.",
                transform=ax.transAxes,
                ha="center",
                va="center",
                fontfamily="Segoe UI",
                fontsize=12,
                color="#1f2933",
            )
            ax.axis("off")
            return fig

        _render_careers_radar(ax, fig, _aggregate_careers(mods))
        return fig


app = App(app_ui, server)
