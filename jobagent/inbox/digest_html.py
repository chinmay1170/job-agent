"""HTML rendering for the daily digest email.

Gmail-safe: table-based layout, fully inlined styles, system fonts, no
external assets, max-width 600. Dark-on-light (email clients are hostile to
dark themes), accent indigo, status pills color-coded.
"""
from __future__ import annotations

from html import escape

# palette
BG = "#f4f5f7"
CARD = "#ffffff"
INK = "#1a1f2e"
MUTED = "#6b7280"
LINE = "#e5e7eb"
ACCENT = "#4f46e5"
GREEN = "#047857"
GREEN_BG = "#d1fae5"
RED = "#b91c1c"
RED_BG = "#fee2e2"
AMBER = "#92400e"
AMBER_BG = "#fef3c7"
BLUE = "#1d4ed8"
BLUE_BG = "#dbeafe"
GRAY_BG = "#f3f4f6"

_PILL_COLORS = {
    "interview": (GREEN, GREEN_BG),
    "interview_request": (GREEN, GREEN_BG),
    "submitted": (BLUE, BLUE_BG),
    "confirmed": (BLUE, BLUE_BG),
    "rejected": (RED, RED_BG),
    "rejection": (RED, RED_BG),
    "needs_review": (AMBER, AMBER_BG),
    "pending": (MUTED, GRAY_BG),
}


def pill(text: str) -> str:
    fg, bg = _PILL_COLORS.get(text.lower().strip(), (MUTED, GRAY_BG))
    return (
        f'<span style="display:inline-block;padding:2px 10px;border-radius:999px;'
        f'background:{bg};color:{fg};font-size:12px;font-weight:600;'
        f'white-space:nowrap;">{escape(text.replace("_", " "))}</span>'
    )


def _font(size: int = 14, color: str = INK, weight: int = 400) -> str:
    return (
        f"font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,"
        f"Arial,sans-serif;font-size:{size}px;color:{color};font-weight:{weight};"
    )


def stat_cell(label: str, value: str, color: str = INK) -> str:
    return f"""<td align="center" style="padding:14px 6px;">
      <div style="{_font(24, color, 700)}line-height:1;">{escape(value)}</div>
      <div style="{_font(11, MUTED)}margin-top:6px;text-transform:uppercase;letter-spacing:.06em;">{escape(label)}</div>
    </td>"""


def section(title: str, inner_html: str, badge: str | None = None) -> str:
    badge_html = (
        f'<span style="{_font(12, MUTED)}background:{GRAY_BG};border-radius:999px;'
        f'padding:2px 10px;margin-left:8px;font-weight:600;">{escape(badge)}</span>'
        if badge is not None else ""
    )
    return f"""<tr><td style="padding:0 24px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="background:{CARD};border:1px solid {LINE};border-radius:12px;margin-bottom:16px;">
        <tr><td style="padding:16px 20px 4px;{_font(15, INK, 700)}">{escape(title)}{badge_html}</td></tr>
        <tr><td style="padding:8px 20px 16px;">{inner_html}</td></tr>
      </table>
    </td></tr>"""


def empty_state(text: str) -> str:
    return f'<div style="{_font(13, MUTED)}padding:6px 0;">{escape(text)}</div>'


def row_table(rows: list[str]) -> str:
    return ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
            + "".join(rows) + "</table>")


def two_col_row(left_html: str, right_html: str, last: bool = False) -> str:
    border = "" if last else f"border-bottom:1px solid {LINE};"
    return (f'<tr><td style="padding:10px 0;{border}">{left_html}</td>'
            f'<td align="right" style="padding:10px 0 10px 12px;{border}vertical-align:top;">'
            f"{right_html}</td></tr>")


def progress_bar(label: str, used: int, cap: int) -> str:
    pct = 0 if not cap else min(100, round(used * 100 / cap))
    color = ACCENT if pct < 80 else (AMBER if pct < 100 else RED)
    return f"""<div style="margin:8px 0;">
      <div style="{_font(12, MUTED)}margin-bottom:4px;">{escape(label)}
        <span style="float:right;{_font(12, INK, 600)}">{used} / {cap}</span></div>
      <div style="background:{GRAY_BG};border-radius:999px;height:8px;">
        <div style="background:{color};border-radius:999px;height:8px;width:{pct}%;"></div>
      </div>
    </div>"""


def render(today: str, data: dict) -> str:
    """data keys: apps(list of rows), sent, replies, rq_open(int),
    borderline, errors, counters(dict kind->(used, cap)), funnel(dict)."""
    f = data["funnel"]
    interviews = f.get("interviews", 0)
    stats = f"""<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
        style="background:{CARD};border:1px solid {LINE};border-radius:12px;">
      <tr>
        {stat_cell("Discovered", str(f.get("discovered", 0)))}
        {stat_cell("Queued", str(f.get("queued", 0)), ACCENT)}
        {stat_cell("Applied", str(f.get("applied", 0)), BLUE)}
        {stat_cell("Replies", str(f.get("replies", 0)))}
        {stat_cell("Interviews", str(interviews), GREEN if interviews else INK)}
      </tr>
    </table>"""

    # Applications submitted today
    if data["apps"]:
        rows = []
        for i, r in enumerate(data["apps"]):
            left = (f'<div style="{_font(14, INK, 600)}">{escape(r["company"] or "?")}</div>'
                    f'<div style="{_font(13, MUTED)}margin-top:2px;">{escape(r["title"] or "?")}</div>')
            rows.append(two_col_row(left, pill(r["status"] or "submitted"),
                                    last=i == len(data["apps"]) - 1))
        apps_html = row_table(rows)
    else:
        apps_html = empty_state("No applications submitted today.")

    # Outreach
    if data["sent"]:
        rows = [two_col_row(
            f'<div style="{_font(13, INK)}">{escape(r["subject"] or "")}</div>'
            f'<div style="{_font(12, MUTED)}margin-top:2px;">to {escape(r["email"] or "?")}</div>',
            pill(r["kind"] or "first_touch"), last=i == len(data["sent"]) - 1)
            for i, r in enumerate(data["sent"])]
        sent_html = row_table(rows)
    else:
        sent_html = empty_state("No outreach emails sent today.")

    # Replies
    if data["replies"]:
        rows = [two_col_row(
            f'<div style="{_font(13, INK)}">{escape((r["subject"] or "(no subject)"))}</div>'
            f'<div style="{_font(12, MUTED)}margin-top:2px;">{escape(r["from_email"] or "")}</div>',
            pill(r["classification"] or "other"), last=i == len(data["replies"]) - 1)
            for i, r in enumerate(data["replies"])]
        replies_html = row_table(rows)
    else:
        replies_html = empty_state("No new replies — they'll appear here as recruiters respond.")

    # Review queue
    if data["rq_open"]:
        rq_html = (
            f'<div style="{_font(13, AMBER, 600)}background:{AMBER_BG};border-radius:8px;'
            f'padding:10px 14px;">{data["rq_open"]} application(s) need your input — '
            f'open the dashboard at <span style="font-weight:700;">localhost:8787</span> '
            f"to resolve them.</div>"
        )
    else:
        rq_html = empty_state("Queue is clear — nothing needs you today.")

    # Borderline
    if data["borderline"]:
        rows = [two_col_row(
            f'<div style="{_font(13, INK)}">{escape(r["company"] or "?")} — {escape(r["title"] or "?")}</div>',
            f'<span style="{_font(13, MUTED, 600)}white-space:nowrap;">score {r["score"]}</span>',
            last=i == len(data["borderline"]) - 1)
            for i, r in enumerate(data["borderline"])]
        borderline_html = row_table(rows)
    else:
        borderline_html = empty_state("No borderline jobs awaiting a decision.")

    # Errors
    if data["errors"]:
        rows = [
            f'<div style="{_font(12, RED)}font-family:ui-monospace,Menlo,monospace;'
            f'padding:4px 0;">{escape(r["ts"])} {escape(r["event"])}: '
            f'{escape((r["detail"] or "")[:120])}</div>'
            for r in data["errors"]
        ]
        errors_html = "".join(rows)
    else:
        errors_html = (f'<div style="{_font(13, GREEN, 600)}">All clear — no errors today.</div>')

    bars = "".join(
        progress_bar(kind.capitalize(), used, cap)
        for kind, (used, cap) in data["counters"].items() if cap
    )

    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:{BG};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{BG};">
<tr><td align="center" style="padding:28px 12px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">
  <tr><td style="padding:0 24px 18px;">
    <span style="{_font(20, INK, 800)}letter-spacing:-.02em;">Job<span style="color:{ACCENT};">Agent</span></span>
    <span style="{_font(13, MUTED)}float:right;padding-top:6px;">{escape(today)}</span>
  </td></tr>
  <tr><td style="padding:0 24px 16px;">{stats}</td></tr>
  {section("Applications submitted today", apps_html, badge=str(len(data["apps"])))}
  {section("Outreach sent", sent_html, badge=str(len(data["sent"])))}
  {section("Recruiter replies", replies_html, badge=str(len(data["replies"])))}
  {section("Needs your attention", rq_html, badge=str(data["rq_open"]))}
  {section("Borderline jobs", borderline_html, badge=str(len(data["borderline"])))}
  {section("Health", errors_html + bars)}
  <tr><td align="center" style="padding:8px 24px 28px;{_font(12, MUTED)}">
    JobAgent · autonomous job search · dashboard at localhost:8787
  </td></tr>
</table>
</td></tr></table>
</body></html>"""
