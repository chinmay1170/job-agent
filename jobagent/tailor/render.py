"""Render resume/cover-letter PDFs with the typst CLI.

Content is passed to the templates as a JSON string via
``--input data=<json>`` and read inside typst with
``json(bytes(sys.inputs.at("data")))``.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

TYPST_BIN = shutil.which("typst") or "/opt/homebrew/bin/typst"
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


class RenderError(RuntimeError):
    pass


def _compile(template_name: str, content: dict, out_pdf: Path) -> None:
    out_pdf = Path(out_pdf)
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    template = TEMPLATES_DIR / template_name
    data = json.dumps(content, ensure_ascii=False)
    proc = subprocess.run(
        [
            TYPST_BIN, "compile",
            str(template), str(out_pdf),
            "--input", f"data={data}",
        ],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise RenderError(
            f"typst compile {template_name} failed rc={proc.returncode}:\n{proc.stderr}"
        )


def render_resume(content: dict, out_pdf: Path) -> None:
    _compile("resume.typ", content, out_pdf)


def render_cover(content: dict, out_pdf: Path) -> None:
    _compile("cover.typ", content, out_pdf)
