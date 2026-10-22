# Unit tests for the Obsidian tools: configuration, vault selection, paths, the
# approval gate, the HTTP client against a fake of the Local REST API plugin, and
# the PDF reader's folder restriction. They never touch a real vault or send
# anything to Gemini, so they run anywhere, with or without Obsidian open.

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import obsidian
from browser import PROJECT_ROOT, BrowserState
from obsidian import (
    DEFAULT_CA_CERT,
    DEFAULT_PDF_COMMAND,
    DEFAULT_URL,
    ObsidianClient,
    ObsidianConfig,
    ObsidianError,
    PdfReader,
    VaultConfig,
    build_obsidian_tools,
    folder_path,
    note_path,
    pdf_path,
    take_approval,
)

API_KEY = "test-key"
TRADING = VaultConfig(id="trading", name="Trading Research", api_key="k1")
MTG = VaultConfig(id="mtg", name="MTG", api_key="k2", url="https://127.0.0.1:27125")
CAMS = VaultConfig(
    id="cams", name="CAMS Certification", api_key="k3", url="https://127.0.0.1:27126"
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a developer's local .env from leaking into the assertions."""
    for name in list(os.environ):
        if name.startswith(("OBSIDIAN_", "PDF_READER_")) or name == "GOOGLE_API_KEY":
            monkeypatch.delenv(name, raising=False)


# --- configuration -------------------------------------------------------------


def test_nothing_configured_means_no_vaults() -> None:
    config = ObsidianConfig.from_env()

    assert config.vaults == ()
    assert config.enabled is True
    assert build_obsidian_tools(config) == []


def test_single_vault_settings_still_work(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OBSIDIAN_API_KEY", "abc")
    monkeypatch.setenv("OBSIDIAN_URL", "http://127.0.0.1:27123/")
    monkeypatch.setenv("OBSIDIAN_CA_CERT", str(tmp_path / "ca.crt"))
    monkeypatch.setenv("OBSIDIAN_MAX_NOTE_CHARS", "100")
    config = ObsidianConfig.from_env()

    (vault,) = config.vaults
    assert vault.api_key == "abc"
    assert vault.url == "http://127.0.0.1:27123"
    assert vault.ca_cert == tmp_path / "ca.crt"
    assert config.max_note_chars == 100


def test_single_vault_defaults() -> None:
    vault = VaultConfig(id="default", name="Obsidian", api_key="k")
    assert vault.url == DEFAULT_URL
    assert vault.ca_cert == DEFAULT_CA_CERT


def test_several_vaults_are_read_by_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OBSIDIAN_VAULTS", "Trading, cams, broken-id, nokey")
    monkeypatch.setenv("OBSIDIAN_TRADING_API_KEY", "k1")
    monkeypatch.setenv("OBSIDIAN_TRADING_NAME", "Trading Research")
    monkeypatch.setenv("OBSIDIAN_TRADING_DIR", str(tmp_path))
    monkeypatch.setenv("OBSIDIAN_CAMS_API_KEY", "k3")
    monkeypatch.setenv("OBSIDIAN_CAMS_URL", "https://127.0.0.1:27126/")
    # A single-vault key is ignored once OBSIDIAN_VAULTS is set.
    monkeypatch.setenv("OBSIDIAN_API_KEY", "ignored")
    config = ObsidianConfig.from_env()

    assert [v.id for v in config.vaults] == ["trading", "cams"]
    trading, cams = config.vaults
    assert trading.name == "Trading Research"
    assert trading.url == DEFAULT_URL
    assert trading.folder == tmp_path
    assert trading.ca_cert == PROJECT_ROOT / "certs" / "obsidian-trading.crt"
    assert cams.name == "cams"
    assert cams.url == "https://127.0.0.1:27126"
    assert cams.folder is None


def test_misconfigured_vaults_are_logged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cert = tmp_path / "ok.crt"
    cert.write_text("")
    monkeypatch.setenv("OBSIDIAN_VAULTS", "trading,brief")
    monkeypatch.setenv("OBSIDIAN_TRADING_API_KEY", "k1")
    monkeypatch.setenv("OBSIDIAN_TRADING_CA_CERT", str(cert))
    monkeypatch.setenv("OBSIDIAN_BRIEF_API_KEY", "k4")
    monkeypatch.setenv("OBSIDIAN_BRIEF_CA_CERT", str(tmp_path / "missing.crt"))
    monkeypatch.setenv("OBSIDIAN_BRIEF_DIR", str(tmp_path / "nowhere"))
    with caplog.at_level("WARNING", logger="obsidian"):
        ObsidianConfig.from_env()

    messages = "\n".join(r.getMessage() for r in caplog.records)
    assert "'trading' and 'brief' share https://127.0.0.1:27124" in messages
    assert "missing.crt is missing" in messages
    assert "nowhere does not exist" in messages
    assert "ok.crt" not in messages


def test_well_configured_vaults_log_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cert = tmp_path / "ok.crt"
    cert.write_text("")
    monkeypatch.setenv("OBSIDIAN_VAULTS", "trading,brief")
    monkeypatch.setenv("OBSIDIAN_TRADING_API_KEY", "k1")
    monkeypatch.setenv("OBSIDIAN_TRADING_CA_CERT", str(cert))
    monkeypatch.setenv("OBSIDIAN_BRIEF_API_KEY", "k4")
    monkeypatch.setenv("OBSIDIAN_BRIEF_URL", "https://127.0.0.1:27127")
    monkeypatch.setenv("OBSIDIAN_BRIEF_CA_CERT", str(cert))
    monkeypatch.setenv("OBSIDIAN_BRIEF_DIR", str(tmp_path))
    with caplog.at_level("WARNING", logger="obsidian"):
        ObsidianConfig.from_env()

    assert caplog.records == []


def test_no_tools_when_disabled() -> None:
    config = ObsidianConfig(vaults=(TRADING,), enabled=False)
    assert build_obsidian_tools(config) == []


def test_tools_without_pdf_reading() -> None:
    tools = build_obsidian_tools(ObsidianConfig(vaults=(TRADING,)))
    names = {tool.info.name for tool in tools}

    assert names == {
        "obsidian_list_vaults",
        "obsidian_search",
        "obsidian_list_folder",
        "obsidian_read_note",
        "obsidian_create_note",
        "obsidian_append_to_note",
    }


def test_pdf_tool_needs_key_binary_and_folder(tmp_path: Path) -> None:
    binary = tmp_path / "pdf-reader"
    binary.write_text("")
    with_folder = VaultConfig(id="t", name="T", api_key="k", folder=tmp_path)

    def names(config: ObsidianConfig) -> set[str]:
        return {tool.info.name for tool in build_obsidian_tools(config)}

    ready = ObsidianConfig(vaults=(with_folder,), pdf_command=binary, pdf_api_key="g")
    assert "obsidian_read_pdf" in names(ready)
    for missing in (
        ObsidianConfig(vaults=(with_folder,), pdf_command=binary),
        ObsidianConfig(
            vaults=(with_folder,), pdf_command=tmp_path / "nope", pdf_api_key="g"
        ),
        ObsidianConfig(vaults=(TRADING,), pdf_command=binary, pdf_api_key="g"),
    ):
        assert "obsidian_read_pdf" not in names(missing)


# --- vault selection -----------------------------------------------------------


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("", "trading"),
        ("mtg", "mtg"),
        ("MTG", "mtg"),
        ("CAMS Certification", "cams"),
        ("cams", "cams"),
        ("the CAMS vault", "cams"),
        ("trading research", "trading"),
        ("my trading vault", "trading"),
    ],
)
def test_vaults_are_found_by_spoken_name(spoken: str, expected: str) -> None:
    config = ObsidianConfig(vaults=(TRADING, MTG, CAMS))
    assert config.find_vault(spoken).id == expected


def test_unknown_vault_lists_the_real_ones() -> None:
    config = ObsidianConfig(vaults=(TRADING, MTG, CAMS))
    with pytest.raises(ObsidianError, match="Trading Research, MTG, CAMS"):
        config.find_vault("recipes")


# --- paths ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("Ideas", "Ideas.md"),
        ("/Projects/Coffy/ideas.md", "Projects/Coffy/ideas.md"),
        ("Projects\\plan", "Projects/plan.md"),
        ("./Projects//plan", "Projects/plan.md"),
        ("Attachments/diagram.canvas", "Attachments/diagram.canvas"),
    ],
)
def test_note_paths_are_normalised(given: str, expected: str) -> None:
    assert note_path(given) == expected


@pytest.mark.parametrize("bad", ["", "  ", "../secrets", "a/../../b"])
def test_bad_note_paths_are_refused(bad: str) -> None:
    with pytest.raises(ObsidianError):
        note_path(bad)


def test_folder_paths() -> None:
    assert folder_path("") == ""
    assert folder_path("/Projects/") == "Projects/"
    with pytest.raises(ObsidianError):
        folder_path("../outside")


def test_pdf_paths() -> None:
    assert pdf_path("Study/AML handbook") == "Study/AML handbook.pdf"
    assert pdf_path("Study/Exam.PDF") == "Study/Exam.PDF"
    for bad in ("", "../x.pdf", "notes.md"):
        with pytest.raises(ObsidianError):
            pdf_path(bad)


# --- approval gate -------------------------------------------------------------


def test_an_action_needs_an_unspent_yes() -> None:
    state = BrowserState()
    assert take_approval(state) is False

    state.unspent_approval = True
    assert take_approval(state) is True
    # One yes covers one action.
    assert take_approval(state) is False


# --- client against a fake plugin ----------------------------------------------


@pytest.fixture
async def plugin() -> AsyncIterator[tuple[ObsidianClient, dict[str, str]]]:
    """A fake Local REST API serving an in-memory vault, and a client for it."""
    vault: dict[str, str] = {"Ideas.md": "# Ideas\nCoffy should brew tea."}

    def authorised(request: web.Request) -> bool:
        return request.headers.get("Authorization") == f"Bearer {API_KEY}"

    async def handle(request: web.Request) -> web.StreamResponse:
        if not authorised(request):
            return web.json_response({"errorCode": 40101}, status=401)
        path = request.match_info["path"]
        if request.method == "GET" and (path == "" or path.endswith("/")):
            entries = sorted(
                name[len(path) :] for name in vault if name.startswith(path)
            )
            if path and not entries:
                return web.json_response({"errorCode": 40400}, status=404)
            return web.json_response({"files": entries})
        if request.method == "GET":
            if path not in vault:
                return web.json_response({"errorCode": 40400}, status=404)
            return web.Response(text=vault[path], content_type="text/markdown")
        body = await request.text()
        if request.method == "PUT":
            if request.headers.get("If-None-Match") == "*" and path in vault:
                return web.json_response({"errorCode": 41200}, status=412)
            vault[path] = body
            return web.Response(status=204)
        if request.method == "POST":
            vault[path] = vault.get(path, "") + body
            return web.Response(status=204)
        return web.Response(status=405)

    async def search(request: web.Request) -> web.StreamResponse:
        if not authorised(request):
            return web.json_response({"errorCode": 40101}, status=401)
        query = request.query["query"].lower()
        return web.json_response(
            [
                {
                    "filename": name,
                    "score": 1,
                    "matches": [{"context": text, "match": {"start": 0, "end": 1}}],
                }
                for name, text in vault.items()
                if query in text.lower()
            ]
        )

    app = web.Application()
    app.router.add_post("/search/simple/", search)
    app.router.add_route("*", "/vault/{path:.*}", handle)
    server = TestServer(app)
    await server.start_server()
    url = str(server.make_url("")).rstrip("/")
    config = VaultConfig(id="test", name="Test", api_key=API_KEY, url=url)
    try:
        yield ObsidianClient(config, ObsidianConfig()), vault
    finally:
        await server.close()


async def test_reads_a_note(plugin) -> None:
    client, _ = plugin
    assert "brew tea" in await client.read_note("Ideas")


async def test_missing_note_is_reported(plugin) -> None:
    client, _ = plugin
    with pytest.raises(ObsidianError, match="no note"):
        await client.read_note("Nope")


async def test_creates_a_note_in_a_new_folder(plugin) -> None:
    client, vault = plugin
    created = await client.create_note("Projects/Coffy/plan", "# Plan")

    assert created == "Projects/Coffy/plan.md"
    assert vault["Projects/Coffy/plan.md"] == "# Plan"


async def test_create_never_overwrites(plugin) -> None:
    client, vault = plugin
    with pytest.raises(ObsidianError, match="already exists"):
        await client.create_note("Ideas", "replaced")
    assert "brew tea" in vault["Ideas.md"]


async def test_appends_on_a_new_line(plugin) -> None:
    client, vault = plugin
    await client.append_to_note("Ideas", "- buy biscuits")

    assert vault["Ideas.md"].endswith("Coffy should brew tea.\n- buy biscuits")


async def test_lists_and_searches(plugin) -> None:
    client, _ = plugin
    await client.create_note("Projects/a", "alpha")

    assert await client.list_folder("") == ["Ideas.md", "Projects/a.md"]
    assert await client.list_folder("Projects") == ["a.md"]
    results = await client.search("tea")
    assert [r["filename"] for r in results] == ["Ideas.md"]


async def test_wrong_key_names_the_vault(plugin) -> None:
    client, _ = plugin
    url = client._vault.url
    client._vault = VaultConfig(id="test", name="Test", api_key="wrong", url=url)
    with pytest.raises(ObsidianError, match="Test vault refused its API key"):
        await client.read_note("Ideas")


async def test_closed_vault_is_named() -> None:
    vault = VaultConfig(id="mtg", name="MTG", api_key="k", url="http://127.0.0.1:9")
    client = ObsidianClient(vault, ObsidianConfig())
    with pytest.raises(ObsidianError, match="cannot reach the MTG vault"):
        await client.read_note("Ideas")


# --- PDF reader ----------------------------------------------------------------


async def test_pdf_needs_a_vault_folder() -> None:
    reader = PdfReader(ObsidianConfig(vaults=(TRADING,), pdf_api_key="g"))
    with pytest.raises(ObsidianError, match="not set up to read PDFs"):
        await reader.read(TRADING, "a.pdf")


@pytest.mark.skipif(
    not DEFAULT_PDF_COMMAND.exists(), reason="pdf-reader binary not built"
)
async def test_real_pdf_reader_refuses_files_outside_the_vault(tmp_path: Path) -> None:
    """The Go server itself must refuse anything outside the vault folders.

    Every case here is refused before Gemini is called, so the dummy key is never
    used and nothing leaves the machine.
    """
    vault_dir = tmp_path / "vault"
    other = tmp_path / "private"
    (vault_dir / ".obsidian").mkdir(parents=True)
    other.mkdir()
    (other / "secret.pdf").write_bytes(b"%PDF-1.4\nsecret\n%%EOF\n")
    (vault_dir / ".obsidian" / "x.pdf").write_bytes(b"%PDF-1.4\nx\n%%EOF\n")
    (vault_dir / "fake.pdf").write_bytes(b"-----BEGIN OPENSSH PRIVATE KEY-----\n")
    # A link inside the vault that points outside it.
    (vault_dir / "link.pdf").symlink_to(other / "secret.pdf")

    vault = VaultConfig(id="v", name="V", api_key="k", folder=vault_dir)
    reader = PdfReader(ObsidianConfig(vaults=(vault,), pdf_api_key="dummy-not-used"))
    for path, message in [
        ("link.pdf", "not allowed"),
        (".obsidian/x.pdf", "not allowed"),
        ("fake.pdf", "not a real PDF"),
        ("missing.pdf", "no PDF by that name"),
    ]:
        with pytest.raises(ObsidianError, match=message):
            await reader.read(vault, path)


# --- PDF retries when Google is busy ---------------------------------------------

BUSY = (
    "Error calling Gemini API: Error 503, Message: This model is currently "
    "experiencing high demand. Spikes in demand are usually temporary. Please try "
    "again later., Status: UNAVAILABLE, Details: []"
)


@pytest.fixture
def scripted_reader(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A PdfReader whose server calls return scripted answers, without Gemini."""
    monkeypatch.setattr(obsidian, "PDF_RETRY_DELAY_SECONDS", 0)
    vault = VaultConfig(id="v", name="V", api_key="k", folder=tmp_path)

    def make(answers: list[tuple[str, bool]], primary: str = "gemini-3.5-flash"):
        reader = PdfReader(
            ObsidianConfig(vaults=(vault,), pdf_api_key="g", pdf_model=primary)
        )
        models: list[str | None] = []

        async def fake_call(_absolute: Path, model: str | None) -> tuple[str, bool]:
            models.append(model)
            return answers.pop(0)

        reader._call = fake_call  # type: ignore[method-assign]
        return reader, vault, models

    return make


async def test_busy_google_is_retried_then_succeeds(scripted_reader) -> None:
    reader, vault, models = scripted_reader([(BUSY, True), ("# Flashcards", False)])

    assert await reader.read(vault, "CAMS- Flashcards.pdf") == "# Flashcards"
    assert models == ["gemini-3.5-flash", "gemini-3.8-flash"]


async def test_busy_google_falls_back_to_other_models(scripted_reader) -> None:
    reader, vault, models = scripted_reader(
        [(BUSY, True), (BUSY, True), ("# Flashcards", False)],
        primary="gemini-3.8-flash",
    )

    assert await reader.read(vault, "a.pdf") == "# Flashcards"
    assert models == ["gemini-3.8-flash", "gemini-flash-latest", "gemini-3.8-flash"]


async def test_busy_everywhere_says_so(scripted_reader) -> None:
    reader, vault, models = scripted_reader([(BUSY, True)] * 4)

    with pytest.raises(ObsidianError, match="too busy right now"):
        await reader.read(vault, "a.pdf")
    assert models == [
        "gemini-3.5-flash",
        "gemini-3.8-flash",
        "gemini-flash-latest",
        "gemini-3.5-flash",
    ]


async def test_other_google_errors_are_not_called_a_permission_problem(
    scripted_reader,
) -> None:
    reader, vault, models = scripted_reader(
        [("Error calling Gemini API: Error 400, Message: bad document", True)]
    )

    with pytest.raises(ObsidianError, match="Google could not read that PDF"):
        await reader.read(vault, "a.pdf")
    assert models == ["gemini-3.5-flash"]


async def test_policy_refusals_are_not_retried(scripted_reader) -> None:
    reader, vault, models = scripted_reader(
        [("Cannot read PDF: file is outside the allowed folders", True)]
    )

    with pytest.raises(ObsidianError, match="not allowed"):
        await reader.read(vault, "a.pdf")
    assert models == ["gemini-3.5-flash"]
