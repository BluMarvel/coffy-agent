# Unit tests for the browser toolset: configuration, tool exposure, and result
# shaping. These never launch a browser or the MCP server, so they run anywhere.
#
# End-to-end browser behavior (that Coffy asks before it clicks, and reads pages
# out loud) is covered by the scenarios in scenarios.browser.yaml.

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from livekit.agents import ToolError, mcp

import browser
from browser import (
    BROWSER_APPROVAL_SECONDS,
    PROJECT_ROOT,
    UNSAFE_TOOLS,
    BrowserConfig,
    BrowserState,
    BrowserToolset,
    _flatten_content,
    _make_result_resolver,
    _strip_internal_code,
    _trim,
    audit,
    browser_check,
    launch_args,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep a developer's local .env from leaking into the assertions."""
    monkeypatch.setenv("COFFY_AUDIT_LOG", str(tmp_path / "approvals.jsonl"))
    for name in (
        "BROWSER_HEADLESS",
        "BROWSER_ISOLATED",
        "BROWSER_KIND",
        "BROWSER_MCP_VERSION",
        "BROWSER_TIMEOUT_SECONDS",
        "BROWSER_MAX_RESULT_CHARS",
        "BROWSER_OUTPUT_DIR",
        "BROWSER_ALLOW_UNSAFE_TOOLS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_defaults_are_a_visible_throwaway_browser() -> None:
    config = BrowserConfig.from_env()

    assert config.headless is False, "the user should be able to watch the browser"
    assert config.isolated is True, "no cookies or logins should survive the call"
    assert config.output_dir == PROJECT_ROOT / ".browser-workspace"
    assert config.allow_unsafe_tools is False


def test_env_overrides_are_parsed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BROWSER_HEADLESS", "true")
    monkeypatch.setenv("BROWSER_ISOLATED", "0")
    monkeypatch.setenv("BROWSER_KIND", "firefox")
    monkeypatch.setenv("BROWSER_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("BROWSER_MAX_RESULT_CHARS", "1234")
    monkeypatch.setenv("BROWSER_OUTPUT_DIR", str(tmp_path))

    config = BrowserConfig.from_env()

    assert config.headless is True
    assert config.isolated is False
    assert config.browser == "firefox"
    assert config.timeout_seconds == 45
    assert config.max_result_chars == 1234
    assert config.output_dir == tmp_path


def test_unsafe_javascript_tools_are_withheld_by_default() -> None:
    withheld = BrowserConfig().allowed_tools

    for name in UNSAFE_TOOLS:
        assert name not in withheld, (
            f"{name} runs arbitrary JavaScript and must be withheld"
        )


def test_unsafe_javascript_tools_are_opt_in() -> None:
    exposed = BrowserConfig(allow_unsafe_tools=True).allowed_tools

    for name in UNSAFE_TOOLS:
        assert name in exposed


def test_launch_args_isolate_the_profile_and_omit_images() -> None:
    args = launch_args(BrowserConfig())

    assert "--isolated" in args
    assert "--headless" not in args, "the browser should be visible by default"
    assert args[args.index("--image-responses") + 1] == "omit"
    assert args[args.index("--output-dir") + 1].endswith(".browser-workspace")


def test_launch_args_switch_to_headless_and_a_persistent_profile() -> None:
    args = launch_args(
        BrowserConfig(
            headless=True, isolated=False, output_dir=Path("/tmp/coffy-browser")
        )
    )

    assert "--headless" in args
    assert "--isolated" not in args
    assert args[args.index("--user-data-dir") + 1] == "/tmp/coffy-browser/profile"


def _result(*items: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(content=list(items))


def test_flatten_content_reads_text() -> None:
    text = _flatten_content(_result(SimpleNamespace(type="text", text="hello world")))

    assert text == "hello world"


def test_flatten_content_never_forwards_image_bytes() -> None:
    # An image part carries base64 data; sending that to a voice model is both
    # useless and expensive, so it must be replaced with a placeholder.
    image = SimpleNamespace(type="image", data="c2VjcmV0LWJ5dGVz", mimeType="image/png")

    text = _flatten_content(_result(image))

    assert "c2VjcmV0LWJ5dGVz" not in text
    assert "image omitted" in text


def test_flatten_content_handles_an_empty_result() -> None:
    assert _flatten_content(_result()) != ""


def test_flatten_content_serialises_unknown_parts() -> None:
    class Odd:
        def model_dump(self, by_alias: bool = False) -> dict[str, str]:
            return {"type": "resource", "uri": "file:///tmp/page.html"}

    text = _flatten_content(_result(Odd()))

    assert json.loads(text)["uri"] == "file:///tmp/page.html"


def test_strip_internal_code_removes_playwrights_own_javascript() -> None:
    raw = (
        "### Ran Playwright code\n"
        "```js\n"
        "await page.goto('https://example.com');\n"
        "```\n"
        "### Page\n"
        "- Page URL: https://example.com/\n"
    )

    cleaned = _strip_internal_code(raw)

    assert "await page.goto" not in cleaned
    assert cleaned.startswith("### Page")


def test_strip_internal_code_keeps_pages_without_a_code_block() -> None:
    raw = "### Page\n- Page Title: Example Domain\n"

    assert _strip_internal_code(raw) == raw


def test_trim_collapses_blank_runs_and_caps_length() -> None:
    assert _trim("a\n\n\n\nb", 100) == "a\n\nb"

    trimmed = _trim("x" * 50, 10)

    assert trimmed.startswith("x" * 10)
    assert "truncated 40 characters" in trimmed


def test_result_resolver_caps_the_text_the_model_receives() -> None:
    resolve = _make_result_resolver(20)
    context = mcp.MCPToolResultContext(
        tool_name="browser_snapshot",
        arguments={},
        result=_result(SimpleNamespace(type="text", text="y" * 500)),
    )

    resolved = resolve(context)

    assert len(resolved) < 100
    assert "truncated" in resolved


# --- the gate ------------------------------------------------------------------


def test_page_changes_need_a_recent_yes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = BrowserState()
    check = browser_check(state)

    assert check("browser_navigate", {"url": "https://example.com"}) is None
    assert check("browser_snapshot", {}) is None
    assert check("browser_click", {"ref": "e1"}) is not None

    now = 1000.0
    monkeypatch.setattr(browser.time, "monotonic", lambda: now)
    state.approved_at = now
    # One yes covers the several clicks and keystrokes an action takes.
    assert check("browser_click", {"ref": "e1"}) is None
    assert check("browser_type", {"ref": "e2", "text": "hi"}) is None

    monkeypatch.setattr(
        browser.time, "monotonic", lambda: now + BROWSER_APPROVAL_SECONDS + 1
    )
    assert check("browser_click", {"ref": "e1"}) is not None

    events = [
        json.loads(line)
        for line in (tmp_path / "approvals.jsonl").read_text().splitlines()
    ]
    assert [e["event"] for e in events] == ["refused", "refused"]
    assert events[0]["tool"] == "browser_click"


async def test_the_browser_server_refuses_before_reaching_playwright(
    tmp_path: Path,
) -> None:
    toolset = BrowserToolset(BrowserConfig(output_dir=tmp_path), BrowserState())

    with pytest.raises(ToolError, match="approval"):
        await toolset._mcp_server._call_tool("browser_click", {"ref": "e1"})


def test_audit_never_breaks_the_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("")
    # A path under a regular file cannot be created.
    monkeypatch.setenv("COFFY_AUDIT_LOG", str(blocker / "approvals.jsonl"))
    audit("proposed", action="anything")
