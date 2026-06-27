"""Render a tailored resume into the approved v2 design (HTML -> PDF via
Playwright), auto-fitting the variable tailored content to exactly one Letter
page. Content is supplied verbatim by the tailor stage (honesty-checked there);
this module only lays it out.
"""
from __future__ import annotations

import html as _html
from pathlib import Path

PAGE_PX = 1056  # Letter height at 96dpi (11in)


def _esc(s: str) -> str:
    return _html.escape(str(s or ""))


def _css(scale: float) -> str:
    # base px values from config/resume_v2.html, multiplied by `scale` so the
    # whole resume shrinks uniformly to fit one page when content is long.
    def s(v: float) -> str:
        return f"{round(v * scale, 2)}px"
    return f"""
  @page {{ size: Letter; margin: 0; }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  :root {{ --ink:#1a2233; --sub:#455062; --muted:#6b7686; --accent:#2257c4; --line:#d4dae4; }}
  html, body {{ background:#fff; }}
  body {{ font-family:"Inter",-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    color:var(--ink); font-size:{s(10.4)}; line-height:1.36; -webkit-font-smoothing:antialiased; }}
  .page {{ width:8.5in; height:11in; padding:0.46in 0.46in 0.34in; display:flex; flex-direction:column; }}
  header {{ display:flex; justify-content:space-between; align-items:flex-end;
    border-bottom:2.5px solid var(--accent); padding-bottom:{s(10)}; }}
  .name {{ font-size:{s(28)}; font-weight:800; letter-spacing:-0.5px; line-height:1; }}
  .role {{ font-size:{s(12)}; font-weight:600; color:var(--accent); margin-top:{s(5)}; letter-spacing:0.2px; }}
  .contact {{ text-align:right; font-size:{s(9.5)}; color:var(--sub); line-height:1.75; }}
  .contact a {{ color:var(--sub); text-decoration:none; }}
  section {{ margin-top:{s(9)}; }}
  h2 {{ font-size:{s(10.6)}; font-weight:800; letter-spacing:1.5px; text-transform:uppercase;
    color:var(--accent); display:flex; align-items:center; gap:9px; margin-bottom:{s(7)}; }}
  h2::after {{ content:""; flex:1; height:1.5px; background:var(--line); }}
  .summary {{ font-size:{s(10.5)}; color:var(--sub); line-height:1.44; text-align:justify; }}
  .skills {{ display:grid; grid-template-columns:{s(140)} 1fr; row-gap:{s(6)}; column-gap:12px; }}
  .skills .cat {{ font-weight:700; color:var(--ink); font-size:{s(10.1)}; }}
  .skills .val {{ color:var(--sub); font-size:{s(10.1)}; line-height:1.45; text-align:justify; }}
  .job {{ margin-bottom:{s(7)}; }}
  .job:last-child {{ margin-bottom:0; }}
  .job-head {{ display:flex; justify-content:space-between; align-items:baseline; }}
  .job-title {{ font-weight:700; font-size:{s(11)}; color:var(--ink); }}
  .job-title .co {{ color:var(--accent); }}
  .job-date {{ font-size:{s(9.5)}; color:var(--muted); font-weight:600; white-space:nowrap; }}
  ul {{ list-style:none; margin-top:{s(4)}; }}
  li {{ position:relative; padding-left:14px; margin-bottom:{s(2.6)}; color:var(--sub);
    font-size:{s(10.2)}; line-height:1.37; text-align:justify; text-justify:inter-word; }}
  li:last-child {{ margin-bottom:0; }}
  li::before {{ content:""; position:absolute; left:2px; top:{s(5.5)}; width:4px; height:4px;
    border-radius:50%; background:var(--accent); }}
  li b {{ color:var(--ink); font-weight:700; }}
  .proj-stack {{ font-size:{s(9.4)}; color:var(--muted); font-weight:600; margin-top:2px; }}
  .edu-line {{ display:flex; justify-content:space-between; align-items:baseline; font-size:{s(10.2)}; }}
  .edu-line .what b {{ font-weight:700; color:var(--ink); }} .edu-line .what {{ color:var(--sub); }}
  .edu-line .when {{ color:var(--muted); font-weight:600; font-size:{s(9.5)}; }}
  .awards li {{ font-size:{s(10.1)}; margin-bottom:{s(3.5)}; }}
"""


def _body(c: dict) -> str:
    def bullets(bs):
        return "".join(f"<li>{_esc(b)}</li>" for b in bs if b)
    exp = "".join(
        f'<div class="job"><div class="job-head">'
        f'<div class="job-title"><span class="co">{_esc(j["co"])}</span> — {_esc(j["title"])}</div>'
        f'<div class="job-date">{_esc(j["dates"])}</div></div>'
        f'<ul>{bullets(j["bullets"])}</ul></div>'
        for j in c.get("experience", [])
    )
    projs = "".join(
        f'<div class="job"><div class="job-head">'
        f'<div class="job-title">{_esc(p["name"])}</div>'
        f'<div class="job-date">{_esc(p.get("dates",""))}</div></div>'
        + (f'<div class="proj-stack">{_esc(p["stack"])}</div>' if p.get("stack") else "")
        + f'<ul>{bullets(p["bullets"])}</ul></div>'
        for p in c.get("projects", [])
    )
    skills = "".join(
        f'<div class="cat">{_esc(s["label"])}</div><div class="val">{_esc(s["items"])}</div>'
        for s in c.get("skills", [])
    )
    awards = "".join(f"<li>{_esc(a)}</li>" for a in c.get("awards", []))
    edu = c.get("education", {})
    proj_section = (f'<section><h2>Projects</h2>{projs}</section>' if projs else "")
    awards_section = (f'<section class="awards"><h2>Awards &amp; Recognition</h2><ul>{awards}</ul></section>'
                      if awards else "")
    return f"""<div class="page">
  <header>
    <div><div class="name">{_esc(c['name'])}</div>
    <div class="role">{_esc(c['role'])}</div></div>
    <div class="contact"><a href="mailto:{_esc(c['email'])}">{_esc(c['email'])}</a> &nbsp;·&nbsp; {_esc(c['phone'])}<br>
    <a href="{_esc(c['linkedin'])}">{_esc(c['linkedin']).replace('https://','')}</a></div>
  </header>
  <section><h2>Summary</h2><div class="summary">{_esc(c['summary'])}</div></section>
  <section><h2>Skills</h2><div class="skills">{skills}</div></section>
  <section><h2>Experience</h2>{exp}</section>
  {proj_section}
  <section><h2>Education</h2><div class="edu-line">
    <div class="what">{_esc(edu.get('line',''))}</div><div class="when">{_esc(edu.get('when',''))}</div></div></section>
  {awards_section}
</div>"""


def _html_doc(c: dict, scale: float) -> str:
    return (f'<!DOCTYPE html><html><head><meta charset="utf-8"><style>{_css(scale)}</style></head>'
            f'<body>{_body(c)}</body></html>')


def render_resume_v2(content: dict, out_pdf: Path) -> dict:
    """Render `content` into the v2 design at out_pdf, shrinking to fit 1 page.
    Returns {pages, scale, fill_pct}."""
    from playwright.sync_api import sync_playwright
    out_pdf = Path(out_pdf)
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 816, "height": 1056}, device_scale_factor=2)
        scale = 1.0
        h = PAGE_PX
        for _ in range(8):
            pg.set_content(_html_doc(content, scale), wait_until="networkidle")
            h = pg.evaluate('document.querySelector(".page").scrollHeight')
            if h <= PAGE_PX + 1:
                break
            scale *= (PAGE_PX / h) ** 0.5 * 0.99  # shrink toward fit, damped
            scale = max(scale, 0.72)
        pg.pdf(path=str(out_pdf), prefer_css_page_size=True, print_background=True)
        b.close()
    return {"scale": round(scale, 3), "fill_pct": round(min(h, PAGE_PX) / PAGE_PX * 100)}
