"""Google Drive through the claude.ai MCP connector.

Uses the Drive connector already attached to your Claude account, so there is
no Google Cloud project, no OAuth client, and no redirect URI to configure -
the CLI is already authorised.

How it works: the app runs `claude -p` headlessly with *only* the Drive MCP
tools allowed, hands it the file as base64 on stdin, and asks for the shared
link back as JSON.

Trade-offs versus the direct API path in drive.py:
  * setup      - none here, versus a one-time Google Cloud client there
  * cost       - consumes Max-plan usage roughly in proportion to file size,
                 because the bytes travel through the model's context
  * speed      - seconds rather than milliseconds
  * determinism- a model performs the upload, so the returned link is
                 validated here rather than trusted
Large files are refused for exactly that reason.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from pathlib import Path
from typing import Any

from .claude_engine import ClaudeCodeError, cli_path

log = logging.getLogger(__name__)

DRIVE_PREFIX = "mcp__claude_ai_Google_Drive__"
ALLOWED_TOOLS = [
    f"{DRIVE_PREFIX}create_file",
    f"{DRIVE_PREFIX}search_files",
    f"{DRIVE_PREFIX}get_file_metadata",
    f"{DRIVE_PREFIX}get_file_permissions",
]

FOLDER_NAME = "Upsurge WhatsApp Manager"

# Hard ceiling, and much lower than it looks like it should be.
#
# The MCP tool takes file content as a base64 *argument*, which means the model
# has to emit the whole encoded file as tool-call output. Measured on this
# setup: a 560 B file (~187 output tokens) uploads in 31s; a 66 KB PDF needs
# ~22,500 output tokens and never completes. Output limits, not context limits,
# are the binding constraint.
#
# 16 KB ~= 5,500 output tokens, which stays comfortably inside them. Real PDFs
# are far bigger, so they route to the direct API path instead.
MAX_BYTES = 16 * 1024

TIMEOUT_SECONDS = 300

_AVAILABLE: bool | None = None


class DriveMCPError(RuntimeError):
    """Upload failed. Message is safe to show in the UI."""


class TooLargeForMCP(DriveMCPError):
    """File exceeds what a tool-call argument can carry. Try another provider."""


async def _run(prompt: str, *, timeout: int = TIMEOUT_SECONDS) -> str:
    """Run the CLI with Drive tools enabled, prompt supplied on stdin.

    stdin matters: a multi-megabyte base64 payload as an argv entry would blow
    past the OS argument-length limit.
    """
    path = cli_path()
    if not path:
        raise DriveMCPError("The `claude` CLI is not installed.")

    args = [
        path,
        "-p",
        "--model", "haiku",          # mechanical work; no need for a big model
        "--output-format", "json",
        "--no-session-persistence",
        "--disable-slash-commands",
        # NOT --strict-mcp-config: that flag strips the claude.ai connectors,
        # which is exactly where the Drive tools live.
        "--allowedTools", *ALLOWED_TOOLS,
        "--disallowedTools", "Bash", "Edit", "Write", "Read", "WebFetch", "WebSearch",
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(prompt.encode()), timeout=timeout
        )
    except asyncio.TimeoutError as exc:
        raise DriveMCPError(f"Drive upload timed out after {timeout}s.") from exc
    except OSError as exc:
        raise DriveMCPError(f"Could not start the claude CLI: {exc}") from exc

    raw = stdout.decode().strip()
    if not raw:
        raise DriveMCPError(stderr.decode().strip()[:300] or "The CLI returned nothing.")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw

    result = str(data.get("result") or "").strip()
    if data.get("is_error"):
        lowered = result.lower()
        if "not logged in" in lowered or "/login" in lowered:
            raise DriveMCPError("Claude Code is not logged in. Run `claude` and use /login.")
        raise DriveMCPError(result or "The CLI reported an error.")
    return result


async def check() -> dict[str, Any]:
    """Is the Drive connector reachable headlessly? Cached after first success."""
    global _AVAILABLE

    if not cli_path():
        return {"available": False,
                "detail": "The `claude` CLI is not installed.",
                "hint": "Install Claude Code, then run `claude` and /login."}

    try:
        answer = await _run(
            "Reply with exactly YES if you can call "
            f"{DRIVE_PREFIX}create_file, otherwise exactly NO.",
            timeout=90,
        )
    except DriveMCPError as exc:
        _AVAILABLE = False
        return {"available": False, "detail": str(exc),
                "hint": "Check `claude mcp list` shows Google Drive as Connected."}

    ok = "yes" in answer.lower()[:20]
    _AVAILABLE = ok
    return {
        "available": ok,
        "detail": "Connected via your claude.ai Google Drive connector."
        if ok else "The Drive connector is not reachable from the CLI.",
        "hint": "" if ok else
        "Run `claude mcp list` — Google Drive must show ✔ Connected.",
    }


def _extract_link(text: str) -> str:
    """Pull a Drive URL out of the model's reply, whatever shape it took."""
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    body = fence.group(1) if fence else text

    match = re.search(r"\{.*\}", body, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            data = {}
        for key in ("link", "webViewLink", "url", "shareLink"):
            value = data.get(key)
            if isinstance(value, str) and "drive.google.com" in value:
                return value.strip()
        file_id = data.get("id") or data.get("fileId")
        if isinstance(file_id, str) and file_id.strip():
            return f"https://drive.google.com/file/d/{file_id.strip()}/view?usp=sharing"

    url = re.search(r"https://drive\.google\.com/\S+", text)
    if url:
        return url.group(0).rstrip(").,'\"")
    return ""


async def upload(path: Path, *, mimetype: str, name: str | None = None) -> dict[str, str]:
    """Upload a file to Drive via MCP and return its shareable link."""
    if not path.exists():
        raise DriveMCPError(f"{path.name} is not on disk any more.")

    size = path.stat().st_size
    if size > MAX_BYTES:
        raise TooLargeForMCP(
            f"{path.name} is {size / 1024:.0f} KB. The Claude connector can only carry "
            f"about {MAX_BYTES // 1024} KB, because the file has to be written out as "
            "base64 inside a tool call. Switch Drive to “Own Google client (API)” in "
            "Settings for real-sized documents."
        )

    filename = name or path.name
    encoded = base64.b64encode(path.read_bytes()).decode()

    prompt = (
        "Upload one file to Google Drive and return its shareable link.\n\n"
        "Steps, in order:\n"
        f"1. Call {DRIVE_PREFIX}search_files with "
        f"query: title = '{FOLDER_NAME}' and "
        "mimeType = 'application/vnd.google-apps.folder'\n"
        f"   If nothing is found, call {DRIVE_PREFIX}create_file with "
        f"title '{FOLDER_NAME}' and mimeType "
        "'application/vnd.google-apps.folder' to create it. Note its id.\n"
        f"2. Call {DRIVE_PREFIX}create_file with:\n"
        f"   title: {filename}\n"
        f"   contentMimeType: {mimetype}\n"
        "   disableConversionToGoogleType: true\n"
        "   parentId: the folder id from step 1\n"
        "   base64Content: the BASE64 block at the end of this message\n"
        "3. Reply with ONLY a JSON object, no prose and no code fence:\n"
        '   {"id": "<file id>", "link": "<webViewLink>"}\n\n'
        "Do not summarise, explain, or add commentary. JSON only.\n\n"
        f"BASE64:\n{encoded}\n"
    )

    answer = await _run(prompt)
    link = _extract_link(answer)
    if not link:
        raise DriveMCPError(
            "The upload did not return a Drive link. Reply was: " + answer[:200]
        )

    file_id = ""
    id_match = re.search(r"/d/([A-Za-z0-9_-]{20,})", link)
    if id_match:
        file_id = id_match.group(1)

    log.info("Uploaded %s to Drive via MCP (%s)", filename, file_id or "id unknown")
    return {"id": file_id, "name": filename, "link": link, "download": ""}
