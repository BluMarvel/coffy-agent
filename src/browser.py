"""Browser control for Coffy, powered by Microsoft's Playwright MCP server.

The browser is driven by ``@playwright/mcp``, launched as a child process over
stdio and exposed to the agent through LiveKit's ``MCPToolset``. See
https://docs.livekit.io/agents/logic/tools/mcp/ for the integration.

The policy enforced here:

* Navigating and reading a page are free: no approval needed.
* Anything that changes a page or an account (clicking, typing, submitting a
  form, choosing an option, pressing keys, uploading a file, accepting a
  dialog) is exposed to the model, but the agent is instructed to call
  :func:`propose_action` first. That tool holds the floor and speaks the
  question itself, so the user always hears exactly what is about to happen,
  and :func:`record_decision` records their answer.
* That rule is also enforced in code: :class:`GatedStdioServer` checks every
  MCP call before it reaches the server, and refuses a page-changing tool unless
  the user said yes to the latest proposal within the last few minutes. An
  approved action such as "log in" takes several clicks, so one yes is not spent
  per call here.
* Every proposal, answer, and refusal is appended to ``logs/approvals.jsonl``
  (``COFFY_AUDIT_LOG``), so what Coffy was allowed to do survives the call.
* ``browser_evaluate`` and ``browser_run_code_unsafe`` execute arbitrary
  JavaScript inside the MCP server process and are never exposed unless
  ``BROWSER_ALLOW_UNSAFE_TOOLS`` is set.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from livekit.agents import RunContext, ToolError, function_tool, mcp
from typing_extensions import Self

logger = logging.getLogger("browser")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Tools that only read the current page. Safe to run without asking.
READ_ONLY_TOOLS: tuple[str, ...] = (
    "browser_snapshot",
    "browser_find",
    "browser_wait_for",
    "browser_console_messages",
    "browser_network_requests",
    "browser_network_request",
    "browser_take_screenshot",
)

# Moving around does not change anything on a site, so it needs no approval.
NAVIGATION_TOOLS: tuple[str, ...] = (
    "browser_navigate",
    "browser_navigate_back",
    "browser_resize",
    "browser_tabs",
)

# These change a page or an account, so the agent must ask first.
CONFIRMATION_TOOLS: tuple[str, ...] = (
    "browser_click",
    "browser_type",
    "browser_fill_form",
    "browser_select_option",
    "browser_press_key",
    "browser_hover",
    "browser_drag",
    "browser_drop",
    "browser_file_upload",
    "browser_handle_dialog",
    "browser_emulate_media",
    "browser_close",
)

# Arbitrary JavaScript inside the MCP server process. Off by default.
UNSAFE_TOOLS: tuple[str, ...] = (
    "browser_evaluate",
    "browser_run_code_unsafe",
)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class BrowserConfig:
    """Runtime configuration for the browser toolset, read from the environment."""

    headless: bool = False
    isolated: bool = True
    browser: str = "chromium"
    mcp_version: str = "latest"
    timeout_seconds: float = 120.0
    max_result_chars: int = 8000
    output_dir: Path = PROJECT_ROOT / ".browser-workspace"
    allow_unsafe_tools: bool = False

    @property
    def allowed_tools(self) -> tuple[str, ...]:
        tools = READ_ONLY_TOOLS + NAVIGATION_TOOLS + CONFIRMATION_TOOLS
        if self.allow_unsafe_tools:
            tools += UNSAFE_TOOLS
        return tools

    @classmethod
    def from_env(cls) -> BrowserConfig:
        return cls(
            headless=_env_bool("BROWSER_HEADLESS", False),
            isolated=_env_bool("BROWSER_ISOLATED", True),
            browser=os.getenv("BROWSER_KIND", "chromium"),
            mcp_version=os.getenv("BROWSER_MCP_VERSION", "latest"),
            timeout_seconds=float(os.getenv("BROWSER_TIMEOUT_SECONDS", "120")),
            max_result_chars=int(os.getenv("BROWSER_MAX_RESULT_CHARS", "8000")),
            output_dir=Path(
                os.getenv(
                    "BROWSER_OUTPUT_DIR", str(PROJECT_ROOT / ".browser-workspace")
                )
            ),
            allow_unsafe_tools=_env_bool("BROWSER_ALLOW_UNSAFE_TOOLS", False),
        )


def launch_args(config: BrowserConfig) -> list[str]:
    """Arguments for ``npx @playwright/mcp``, without the command itself."""
    args = [
        "-y",
        f"@playwright/mcp@{config.mcp_version}",
        "--browser",
        config.browser,
        # Never hand raw images to the model: they cost latency and a voice agent
        # cannot act on them. Screenshots are still written to the output dir.
        "--image-responses",
        "omit",
        "--console-level",
        "error",
        "--output-dir",
        str(config.output_dir),
    ]
    if config.isolated:
        args.append("--isolated")
    else:
        args += ["--user-data-dir", str(config.output_dir / "profile")]
    if config.headless:
        args.append("--headless")
    return args


def _flatten_content(result: Any) -> str:
    """Render an MCP tool result as text, never as raw image bytes."""
    parts: list[str] = []
    for item in getattr(result, "content", None) or []:
        kind = getattr(item, "type", None)
        if kind == "text":
            text = getattr(item, "text", "") or ""
            if text:
                parts.append(text)
        elif kind == "image":
            parts.append("[image omitted: not sent to the model]")
        else:
            # Unknown part type, so keep its shape visible rather than dropping it.
            try:
                parts.append(
                    json.dumps(item.model_dump(by_alias=True), ensure_ascii=False)
                )
            except Exception:
                parts.append(str(item))
    if not parts:
        return "The browser returned no content for that step."
    return "\n".join(parts)


def _strip_internal_code(text: str) -> str:
    """Drop the "### Ran Playwright code" block Playwright MCP prepends.

    That block is JavaScript for the MCP server, not page content, and a voice
    agent should never read it aloud.
    """
    marker = "### Ran Playwright code"
    start = text.find(marker)
    if start == -1:
        return text
    following_heading = text.find("\n### ", start + len(marker))
    if following_heading == -1:
        return text[:start].rstrip()
    return f"{text[:start]}{text[following_heading + 1 :]}"


def _trim(text: str, limit: int) -> str:
    """Collapse blank runs and cap the length so snapshots stay affordable."""
    text = re.sub(r"\n{3,}", "\n\n", text.strip())
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n... [truncated {len(text) - limit} characters]"


def _make_result_resolver(limit: int) -> Any:
    def resolve(ctx: mcp.MCPToolResultContext) -> str:
        text = _strip_internal_code(_flatten_content(ctx.result))
        return _trim(text, limit)

    return resolve


class BrowserToolset(mcp.MCPToolset):
    """Playwright MCP, restricted to the tools Coffy is allowed to use.

    ``allowed_tools`` is only offered by the HTTP server type, so a stdio server
    is filtered here, once ``setup()`` has fetched the tool list.
    """

    def __init__(
        self, config: BrowserConfig, state: BrowserState | None = None
    ) -> None:
        self._config = config
        # The server is launched with this as its cwd, and MCP spawn fails if the
        # directory does not exist, so create it before anything else.
        config.output_dir.mkdir(parents=True, exist_ok=True)
        server = GatedStdioServer(
            check=browser_check(state or BrowserState()),
            command="npx",
            args=launch_args(config),
            # Playwright treats this as the workspace root, which bounds where it
            # may read and write files. Everything it produces lands here too.
            cwd=str(config.output_dir),
            # The MCP default is 5s, which a real page load easily exceeds.
            client_session_timeout_seconds=config.timeout_seconds,
            tool_result_resolver=_make_result_resolver(config.max_result_chars),
        )
        super().__init__(id="browser", mcp_server=server)

    async def setup(self, *, reload: bool = False) -> Self:
        await super().setup(reload=reload)
        allowed = set(self._config.allowed_tools)
        self.filter_tools(lambda tool: tool.id in allowed)
        logger.info("browser tools available: %s", sorted(t.id for t in self.tools))
        return self


@dataclass
class BrowserState:
    """Per-session browser state, reachable from tools as ``ctx.userdata``."""

    proposed: int = 0
    approved: int = 0
    declined: int = 0
    last_proposed_action: str | None = None
    # A yes that no write has used yet. Tools that enforce approval in code (the
    # Obsidian writes and Claude Code) spend it; a new proposal or a no clears it.
    unspent_approval: bool = False
    # When the user last said yes (time.monotonic()), for the browser's window.
    # A new proposal or a no clears it.
    approved_at: float | None = None


def _state(ctx: RunContext[BrowserState]) -> BrowserState:
    state = ctx.userdata
    if isinstance(state, BrowserState):
        return state
    logger.warning(
        "session userdata is not BrowserState, approval state will not persist"
    )
    return BrowserState()


# How long one yes keeps the browser's page-changing tools open.
BROWSER_APPROVAL_SECONDS = 300.0

_NOT_APPROVED = (
    "Not done: this needs the user's approval first. Call propose_action "
    "describing exactly what you will do, wait for the answer, record it with "
    "record_decision, and only then try again."
)


def take_approval(state: BrowserState) -> bool:
    """Spend the user's latest yes on one action. False if there is none to spend."""
    if not state.unspent_approval:
        return False
    state.unspent_approval = False
    return True


def browser_action_approved(state: BrowserState) -> bool:
    """Whether the user said yes to the latest proposal recently enough."""
    return (
        state.approved_at is not None
        and time.monotonic() - state.approved_at < BROWSER_APPROVAL_SECONDS
    )


def audit_log_path() -> Path:
    return Path(
        os.getenv("COFFY_AUDIT_LOG", str(PROJECT_ROOT / "logs" / "approvals.jsonl"))
    )


def audit(event: str, **fields: Any) -> None:
    """Append one line to the approval audit log. Never fails the caller."""
    record = {
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event": event,
        **fields,
    }
    path = audit_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        logger.exception("could not write the approval audit log at %s", path)


# Returns why a call is refused, or None to let it through.
ToolCheck = Callable[[str, dict[str, Any]], str | None]


class GatedStdioServer(mcp.MCPServerStdio):
    """A stdio MCP server that runs Coffy's approval check before every call.

    MCP tools are not Coffy's own Python functions, so this is the one place the
    approval gate can be enforced in code for them rather than by the prompt.
    """

    def __init__(self, *, check: ToolCheck, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._check = check

    async def _call_tool(
        self, name: str, arguments: dict[str, Any], **kwargs: Any
    ) -> Any:
        refusal = self._check(name, arguments)
        if refusal:
            raise ToolError(refusal)
        return await super()._call_tool(name, arguments, **kwargs)


def browser_check(state: BrowserState) -> ToolCheck:
    def check(name: str, arguments: dict[str, Any]) -> str | None:
        if name in CONFIRMATION_TOOLS and not browser_action_approved(state):
            audit("refused", tool=name, reason="no recent approval")
            return _NOT_APPROVED
        return None

    return check


@function_tool()
async def propose_action(ctx: RunContext[BrowserState], action: str, why: str) -> str:
    """Ask the user to approve an action before it is performed.

    Call this before anything that changes a page, an account, or the computer,
    and wait for the answer before acting. In the browser: clicking, typing or
    filling a form, choosing an option, pressing a key, dragging, uploading a file,
    or accepting a dialog. With Claude Code: running a command, editing or writing
    a file, or handing a task to a Claude Code agent. In Obsidian: creating a note
    or adding to one. Navigating, reading pages, files or notes, searching, and
    taking a screenshot do not need approval.

    Args:
        action: The action you intend to take, in plain spoken language, as a verb
            phrase, for example "click the Buy now button on the product page" or
            "edit the agent file to fix the greeting".
        why: One short spoken sentence on why the user would want this.
    """
    state = _state(ctx)
    state.proposed += 1
    state.last_proposed_action = action
    state.unspent_approval = False
    state.approved_at = None
    logger.info("browser action proposed: %s (reason: %s)", action, why)
    audit("proposed", action=action, why=why)

    # The question is spoken by this tool rather than generated, so the user is
    # always told exactly what is about to happen, in the same words each time.
    # foreground() holds the floor so the question lands on its own.
    #
    # Note: a nested AgentTask cannot be used here. Google's realtime model has no
    # separate LLM inside a tool activity, so a task cannot generate its own
    # questions, and LiveKit logs that resuming function calls from chat context is
    # unsupported for it.
    async with ctx.foreground():
        await ctx.session.say(f"I would {action}. Shall I go ahead?")

    return (
        "The question has already been asked out loud. Do not repeat it, rephrase it, "
        "or say anything else. Do not touch the browser. Wait for the user's answer, "
        "record it with record_decision, and only then act."
    )


@function_tool()
async def record_decision(
    ctx: RunContext[BrowserState], approved: bool, heard: str
) -> str:
    """Record the user's answer to the last propose_action question.

    Call this as soon as the user answers, before doing anything else.

    Args:
        approved: True only if the user clearly agreed. Silence, hesitation, a
            question, or a change of subject is not agreement.
        heard: A few words of what the user actually said.
    """
    state = _state(ctx)
    action = state.last_proposed_action or "the last action"

    state.unspent_approval = approved
    state.approved_at = time.monotonic() if approved else None
    audit("approved" if approved else "declined", action=action, heard=heard)
    if approved:
        state.approved += 1
        logger.info("browser action approved: %s (user said: %s)", action, heard)
        return (
            f"Recorded that the user approved: {action}. "
            "Perform it now with the matching tool, then say what changed."
        )

    state.declined += 1
    logger.info("browser action declined: %s (user said: %s)", action, heard)
    return (
        f"Recorded that the user declined: {action}. Do not perform it and do not ask "
        "again. Say so in one sentence, then ask what they would like instead."
    )


def build_browser_tools(
    config: BrowserConfig | None = None, state: BrowserState | None = None
) -> list[Any]:
    """The tools to hand to the assistant: the Playwright toolset and the gate.

    ``state`` must be the session's userdata, the same object ``propose_action``
    and ``record_decision`` update, or no page-changing call will ever be allowed.
    """
    config = config or BrowserConfig.from_env()
    return [BrowserToolset(config, state), propose_action, record_decision]
