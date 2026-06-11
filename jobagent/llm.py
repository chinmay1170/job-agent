"""Wrapper around headless `claude -p` calls returning validated JSON.

All LLM judgment in the pipeline goes through here. Uses the user's Claude
subscription via the CLI; no raw API key. Strips markdown fences, validates
against a pydantic model, retries once on malformed output.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Type, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

CLAUDE_BIN = shutil.which("claude") or "/opt/homebrew/bin/claude"
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class LLMError(RuntimeError):
    pass


def _run_claude(prompt: str, model: str, timeout: int) -> str:
    proc = subprocess.run(
        [
            CLAUDE_BIN, "-p", prompt,
            "--output-format", "json",
            "--model", model,
            "--allowedTools", "",
        ],
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        raise LLMError(f"claude -p failed rc={proc.returncode}: {proc.stderr[:500]}")
    envelope = json.loads(proc.stdout)
    if envelope.get("is_error"):
        raise LLMError(f"claude -p error result: {envelope.get('result', '')[:500]}")
    return envelope["result"]


def _extract_json(text: str) -> str:
    text = _FENCE.sub("", text).strip()
    # If the model wrapped JSON in prose, grab the outermost object/array.
    if not text.startswith(("{", "[")):
        m = re.search(r"[\[{].*[\]}]", text, re.DOTALL)
        if m:
            text = m.group(0)
    return text


def ask_json(prompt: str, schema: Type[T], model: str = "sonnet",
             timeout: int = 300, retries: int = 1) -> T:
    """Run a judgment prompt and parse the response into `schema`."""
    contract = (
        "\n\nRespond with ONLY a JSON object matching this schema, no prose, "
        f"no markdown fences:\n{json.dumps(schema.model_json_schema(), indent=None)}"
    )
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        p = prompt + contract
        if attempt and last_err:
            p += f"\n\nYour previous response was invalid ({last_err}). Output strictly valid JSON."
        try:
            raw = _run_claude(p, model, timeout)
            return schema.model_validate_json(_extract_json(raw))
        except (json.JSONDecodeError, ValidationError, LLMError) as e:
            last_err = e
    raise LLMError(f"LLM output invalid after {retries + 1} attempts: {last_err}")
