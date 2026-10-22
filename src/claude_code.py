"""Claude Code for Coffy, through Claude Code's own MCP server.

``claude mcp serve`` runs Claude Code as an MCP server over stdio, and LiveKit's
``MCPToolset`` exposes its tools to the agent, the same way ``browser.py``
exposes Playwright. See https://docs.livekit.io/agents/logic/tools/mcp/.

The policy enforced here mirrors the browser's:

* Reading files and fetching or searching the web are free: no approval needed.
* Anything that changes the computer (running a shell command, editing or
  writing a file, or handing a task to a Claude Code agent, which can do all of
  those) is exposed to the model, but the agent is instructed to call
  ``propose_action`` first, which speaks the question itself, and
  ``record_decision`` records the answer.
* Every other Claude Code tool (scheduling, worktrees, messaging, notifications,
  and so on) is never exposed.

``claude mcp serve`` does not apply the permission rules in ``.claude/settings.json``
to tools called over MCP (checked with Claude Code 2.1.305: a denied ``rm -rf``
ran). So both rules are enforced here, before a call reaches Claude Code:

* Each change needs its own yes: one approval is spent by one command, edit,
  write, or agent task, as for Obsidian writes.
* Some things are refused even with a yes (:func:`blocked_reason`): reading or
  changing secrets, recursive deletes, ``sudo``, ``git push``, ``curl``/``wget``,
  and changing files outside the project or Coffy's own certificates, startup
  scripts, and permissions. The command checks match text, so they stop mistakes
  and casual prompt injection, not a determined attacker; the yes is the real gate.
"""

from __future__ import annotations

import glob
import logging
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from livekit.agents import mcp
from typing_extensions import Self

from browser import (
    _NOT_APPROVED,
    PROJECT_ROOT,
    BrowserState,
    GatedStdioServer,
    ToolCheck,
    _env_bool,
    _make_result_resolver,
    audit,
    take_approval,
)

logger = logging.getLogger("claude_code")

# Reading files and the web changes nothing, so it needs no approval.
READ_ONLY_TOOLS: tuple[str, ...] = (
    "Read",
    "WebFetch",
    "WebSearch",
)

# These change the computer, so the agent must ask first. Agent is here because a
# Claude Code agent can itself run commands and edit files.
CONFIRMATION_TOOLS: tuple[str, ...] = (
    "Bash",
    "Edit",
    "Write",
    "Agent",
)

# Shell commands refused even with approval, with the reason Coffy is given.
_BLOCKED_COMMANDS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\brm\b[^;&|\n]*\s(-\w*[rR]\w*|--recursive)\b"),
        "deleting folders recursively",
    ),
    (re.compile(r"\bsudo\b"), "running commands as administrator"),
    (re.compile(r"\bgit\s+push\b"), "pushing to a remote repository"),
    (re.compile(r"\b(curl|wget)\b"), "downloading or uploading with curl or wget"),
    (re.compile(r"\blaunchctl\b|LaunchAgents"), "changing background services"),
    (re.compile(r"\bsecurity\s+\w"), "using the macOS Keychain"),
    (
        re.compile(r"\.env(?![\w.])|\.env\.(?!example\b)|\.ssh\b|\.aws\b"),
        "touching secret files",
    ),
    (re.compile(r"\.claude/"), "changing Claude Code's settings"),
)

# Folders inside the project that hold Coffy's own certificates, startup scripts,
# and permissions. Never edited through Claude Code.
_PROTECTED_DIRS: tuple[str, ...] = ("certs", "launchd", ".claude")


def _is_secret(path: Path) -> bool:
    name = path.name
    home = Path.home()
    return (
        (name.startswith(".env") and name != ".env.example")
        or name.endswith((".pem", ".key"))
        or name.startswith("id_rsa")
        or name.startswith("id_ed25519")
        # Obsidian plugin settings hold the vault's API key and private key.
        or (name == "data.json" and ".obsidian" in path.parts)
        or path.is_relative_to(home / ".ssh")
        or path.is_relative_to(home / ".aws")
    )


def blocked_reason(name: str, arguments: dict[str, Any], workdir: Path) -> str | None:
    """Why this call is refused even with the user's approval, or None."""
    if name == "Bash":
        command = str(arguments.get("command", ""))
        for pattern, reason in _BLOCKED_COMMANDS:
            if pattern.search(command):
                return reason
        return None
    if name in ("Read", "Edit", "Write"):
        raw = arguments.get("file_path")
        if not raw:
            return None
        path = Path(str(raw)).expanduser()
        if not path.is_absolute():
            path = workdir / path
        path = path.resolve()
        if _is_secret(path):
            return "reading or changing secret files"
        if name == "Read":
            return None
        root = workdir.resolve()
        if not path.is_relative_to(root):
            return "changing files outside the project folder"
        if any(path.is_relative_to(root / d) for d in _PROTECTED_DIRS):
            return "changing Coffy's certificates, startup scripts, or permissions"
    return None


def _describe(name: str, arguments: dict[str, Any]) -> str:
    """A short, secret-free note of what an approved call does, for the audit log."""
    if name == "Bash":
        return str(arguments.get("command", ""))[:300]
    if name in ("Edit", "Write"):
        return str(arguments.get("file_path", ""))
    return str(arguments.get("description") or arguments.get("prompt", ""))[:300]


def claude_code_check(state: BrowserState, workdir: Path) -> ToolCheck:
    def check(name: str, arguments: dict[str, Any]) -> str | None:
        reason = blocked_reason(name, arguments, workdir)
        if reason:
            audit("blocked", tool=name, reason=reason)
            return (
                f"Refused: {reason} is never allowed through Claude Code, even with "
                "the user's approval. Tell the user so in one sentence."
            )
        if name in CONFIRMATION_TOOLS:
            if not take_approval(state):
                audit("refused", tool=name, reason="no unspent approval")
                return _NOT_APPROVED
            audit("allowed", tool=name, detail=_describe(name, arguments))
        return None

    return check


# Where the VS Code extension keeps its copy of the CLI, newest version last.
_VSCODE_BINARY_GLOB = (
    "~/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude"
)


def find_claude_command() -> str | None:
    """The ``claude`` executable: CLAUDE_CODE_COMMAND, then PATH, then VS Code's copy."""
    configured = os.getenv("CLAUDE_CODE_COMMAND")
    if configured:
        return configured
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    bundled = sorted(glob.glob(os.path.expanduser(_VSCODE_BINARY_GLOB)))
    return bundled[-1] if bundled else None


@dataclass(frozen=True)
class ClaudeCodeConfig:
    """Runtime configuration for the Claude Code toolset, read from the environment."""

    enabled: bool = True
    command: str | None = None
    workdir: Path = PROJECT_ROOT
    # A Claude Code agent working through a task can easily take minutes.
    timeout_seconds: float = 600.0
    max_result_chars: int = 8000

    @property
    def allowed_tools(self) -> tuple[str, ...]:
        return READ_ONLY_TOOLS + CONFIRMATION_TOOLS

    @classmethod
    def from_env(cls) -> ClaudeCodeConfig:
        return cls(
            enabled=_env_bool("CLAUDE_CODE_ENABLED", True),
            command=find_claude_command(),
            workdir=Path(os.getenv("CLAUDE_CODE_WORKDIR", str(PROJECT_ROOT))),
            timeout_seconds=float(os.getenv("CLAUDE_CODE_TIMEOUT_SECONDS", "600")),
            max_result_chars=int(os.getenv("CLAUDE_CODE_MAX_RESULT_CHARS", "8000")),
        )


class ClaudeCodeToolset(mcp.MCPToolset):
    """Claude Code's MCP server, restricted to the tools Coffy is allowed to use.

    As with the browser, a stdio server has no ``allowed_tools`` option, so the
    list is filtered here once ``setup()`` has fetched it.
    """

    def __init__(
        self, config: ClaudeCodeConfig, state: BrowserState | None = None
    ) -> None:
        if not config.command:
            raise ValueError("no claude executable configured")
        self._config = config
        server = GatedStdioServer(
            check=claude_code_check(state or BrowserState(), config.workdir),
            command=config.command,
            args=["mcp", "serve"],
            # Claude Code works relative to this directory.
            cwd=str(config.workdir),
            client_session_timeout_seconds=config.timeout_seconds,
            tool_result_resolver=_make_result_resolver(config.max_result_chars),
        )
        super().__init__(id="claude_code", mcp_server=server)

    async def setup(self, *, reload: bool = False) -> Self:
        await super().setup(reload=reload)
        allowed = set(self._config.allowed_tools)
        self.filter_tools(lambda tool: tool.id in allowed)
        logger.info("claude code tools available: %s", sorted(t.id for t in self.tools))
        return self


def build_claude_code_tools(
    config: ClaudeCodeConfig | None = None, state: BrowserState | None = None
) -> list[Any]:
    """The Claude Code toolset, or nothing if it is disabled or not installed.

    The approval gate (``propose_action`` and ``record_decision``) is shared with
    the browser and comes from ``build_browser_tools``. ``state`` must be the
    session's userdata, or no change to the computer will ever be allowed.
    """
    config = config or ClaudeCodeConfig.from_env()
    if not config.enabled:
        logger.info("claude code tools disabled by CLAUDE_CODE_ENABLED")
        return []
    if not config.command:
        logger.warning(
            "claude code tools unavailable: no claude executable found. "
            "Install Claude Code or set CLAUDE_CODE_COMMAND."
        )
        return []
    logger.info("claude code tools using %s in %s", config.command, config.workdir)
    return [ClaudeCodeToolset(config, state)]
