"""Claude Code as a generation engine.

Runs the local `claude` CLI in headless mode on your Max subscription, so
there is no per-call API cost and no free-tier rate limit.

It is deliberately locked down:
  * runs in an empty scratch directory, never the project, so it cannot read
    your code or .env
  * all tools disabled - this is a text generator, not an agent
  * no session persistence, no MCP servers, no slash commands

That keeps output deterministic and fast, and means a prompt built from
untrusted web research can never cause a file read or a shell command.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path

from ..config import BASE_DIR

log = logging.getLogger(__name__)

# Empty, disposable cwd. Claude Code is launched here so CLAUDE.md discovery
# and any relative file access find nothing of ours.
WORKDIR = BASE_DIR / ".claude-workdir"

DEFAULT_TIMEOUT = 300  # generating a full PDF outline can take a while

# Always passed explicitly. Your ~/.claude/settings.json may point at a model
# this app cannot use (a local Ollama entry, say), and inheriting it would fail
# every generation. Pinning here keeps the app working without touching your
# personal Claude Code config.
DEFAULT_MODEL = "sonnet"
ALLOWED_MODELS = ("haiku", "sonnet", "opus")

# Everything, so the CLI cannot touch the filesystem or network.
BLOCKED_TOOLS = [
    "Bash", "Edit", "Write", "Read", "Glob", "Grep", "WebFetch", "WebSearch",
    "Task", "NotebookEdit", "TodoWrite", "Agent",
]


class ClaudeCodeError(RuntimeError):
    """Any failure running the CLI. Message is safe to show in the UI."""

    def __init__(self, message: str, *, needs_login: bool = False):
        super().__init__(message)
        self.needs_login = needs_login


def cli_path() -> str | None:
    """Locate the claude binary, including the common non-PATH install spot."""
    found = shutil.which("claude")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "claude"
    return str(fallback) if fallback.exists() else None


def is_available() -> bool:
    return cli_path() is not None


_CHECK_CACHE: dict[str, object] = {}
_CHECK_CACHE_AT: float = 0.0
CHECK_TTL_SECONDS = 600


async def check(force: bool = False) -> dict[str, object]:
    """Report whether the CLI exists and is logged in. Never raises.

    Probing costs a real (tiny) generation, so the answer is cached - the
    Composer asks this on every load and login state rarely changes.
    """
    global _CHECK_CACHE, _CHECK_CACHE_AT

    loop = asyncio.get_running_loop()
    now = loop.time()
    if not force and _CHECK_CACHE and (now - _CHECK_CACHE_AT) < CHECK_TTL_SECONDS:
        return dict(_CHECK_CACHE)

    result = await _check_uncached()
    _CHECK_CACHE, _CHECK_CACHE_AT = dict(result), now
    return result


async def _check_uncached() -> dict[str, object]:
    path = cli_path()
    if not path:
        return {
            "available": False,
            "logged_in": False,
            "version": "",
            "error": "The `claude` CLI is not installed.",
            "hint": "Install Claude Code, then run `claude` once and use /login.",
        }

    version = ""
    try:
        proc = await asyncio.create_subprocess_exec(
            path, "--version",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        version = out.decode().strip()
    except (OSError, asyncio.TimeoutError) as exc:
        return {
            "available": False, "logged_in": False, "version": "",
            "error": f"Could not run the claude CLI: {exc}", "hint": "",
        }

    try:
        # Cheapest model - this only proves the CLI can reach the API.
        await generate("Reply with exactly: OK", system="Reply with exactly what is asked.",
                       model="haiku", timeout=90)
    except ClaudeCodeError as exc:
        return {
            "available": True,
            "logged_in": not exc.needs_login,
            "version": version,
            "error": str(exc),
            "hint": "Run `claude` in a terminal, then /login with your Max plan."
            if exc.needs_login else "",
        }

    return {"available": True, "logged_in": True, "version": version, "error": None, "hint": ""}


async def generate(
    prompt: str,
    *,
    system: str = "",
    model: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """One headless generation. Returns the assistant text."""
    path = cli_path()
    if not path:
        raise ClaudeCodeError(
            "The `claude` CLI is not installed, so the Claude Code engine is unavailable."
        )

    WORKDIR.mkdir(parents=True, exist_ok=True)

    chosen = (model or "").strip() or DEFAULT_MODEL

    args = [
        path,
        "-p", prompt,
        "--model", chosen,          # never inherit the user's configured default
        "--output-format", "json",
        "--no-session-persistence",
        "--strict-mcp-config",      # ignore the user's MCP servers
        "--disable-slash-commands",
        "--disallowedTools", *BLOCKED_TOOLS,
    ]
    if system:
        args[1:1] = ["--system-prompt", system]

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(WORKDIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise ClaudeCodeError(
            f"Claude Code did not finish within {timeout}s. Try a shorter brief."
        ) from exc
    except OSError as exc:
        raise ClaudeCodeError(f"Could not start the claude CLI: {exc}") from exc

    raw = stdout.decode().strip()
    if not raw:
        detail = stderr.decode().strip()[:300] or "no output"
        raise ClaudeCodeError(f"Claude Code returned nothing ({detail}).")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Older builds can emit plain text despite --output-format json.
        return raw

    result = str(data.get("result") or "").strip()

    if data.get("is_error") or data.get("subtype") not in (None, "success"):
        lowered = result.lower()
        if "not logged in" in lowered or "/login" in lowered:
            raise ClaudeCodeError(
                "Claude Code is not logged in. Run `claude` in a terminal and use /login.",
                needs_login=True,
            )
        if "issue with the selected model" in lowered:
            raise ClaudeCodeError(
                f"Claude Code rejected the model '{chosen}'. "
                f"Pick one of: {', '.join(ALLOWED_MODELS)}."
            )
        raise ClaudeCodeError(f"Claude Code failed: {result or 'unknown error'}")

    if not result:
        raise ClaudeCodeError("Claude Code returned an empty result.")
    return result
