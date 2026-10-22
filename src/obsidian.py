"""Obsidian for Coffy, through the Local REST API plugin.

The plugin (https://github.com/coddingtonbear/obsidian-local-rest-api) serves an
open vault over HTTPS while Obsidian is running. Every vault runs its own copy of
the plugin with its own port, API key and certificate, so Coffy knows each vault by
a short id, configured in ``.env.local``:

    OBSIDIAN_VAULTS=trading,mtg,cams
    OBSIDIAN_TRADING_NAME=Trading Research
    OBSIDIAN_TRADING_API_KEY=...
    OBSIDIAN_TRADING_URL=https://127.0.0.1:27124
    OBSIDIAN_TRADING_DIR=/Users/me/Obsidian Documents/Securities/Trading Research

The certificate defaults to ``certs/obsidian-<id>.crt``. With no ``OBSIDIAN_VAULTS``,
the single-vault settings ``OBSIDIAN_API_KEY``/``OBSIDIAN_URL`` still work.

The policy mirrors the browser's and Claude Code's:

* Searching, listing folders, and reading notes change nothing, so they need no
  approval.
* Creating a note or adding to one changes the vault, and reading a PDF uploads it
  to Google's Gemini API. The agent is instructed to call ``propose_action`` first,
  and this is also enforced here: each of them is refused unless the user's latest
  answer was a yes that nothing else has used yet.

PDFs are read by the pdf-reader MCP server (../pdf-reader-mcp), started per request
with only the configured vault folders allowed, so it cannot read anything else on
the computer. A vault without ``OBSIDIAN_<ID>_DIR`` cannot have its PDFs read.

The plugin creates any missing folders when a note is written, so a note at
``Projects/Coffy/ideas.md`` creates ``Projects/Coffy`` along the way. It has no way
to create an empty folder on its own.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp
from livekit.agents import RunContext, function_tool

from browser import (
    _NOT_APPROVED,
    PROJECT_ROOT,
    BrowserState,
    _env_bool,
    _state,
    take_approval,
)

logger = logging.getLogger("obsidian")

DEFAULT_URL = "https://127.0.0.1:27124"
# The single-vault certificate, downloaded from
# https://127.0.0.1:27124/obsidian-local-rest-api.crt. It only changes if
# "Reset all crypto" is pressed in the plugin's settings.
DEFAULT_CA_CERT = PROJECT_ROOT / "certs" / "obsidian-local-rest-api.crt"
DEFAULT_PDF_COMMAND = PROJECT_ROOT.parent / "pdf-reader-mcp" / "pdf-reader"
# gemini-3.5-flash first: in October 2026 it answered a small PDF in about three
# seconds, while gemini-3.8-flash (the pdf-reader's own default) took close to a
# minute or answered 503 "high demand". The newer models remain as fallbacks.
DEFAULT_PDF_MODEL = "gemini-3.5-flash"
DEFAULT_PDF_FALLBACK_MODELS = ("gemini-3.8-flash", "gemini-flash-latest")
# How long to wait before asking Google again after it said it was busy.
PDF_RETRY_DELAY_SECONDS = 3.0

# Google's ways of saying "busy, try later", as the pdf-reader passes them on.
_GEMINI_BUSY = re.compile(
    r"\b(429|503)\b|UNAVAILABLE|RESOURCE_EXHAUSTED|high demand|overloaded",
    re.IGNORECASE,
)

_VAULT_ID = re.compile(r"^[a-z][a-z0-9_]*$")


class ObsidianError(Exception):
    """A failure worth telling the user about, already phrased for speech."""


@dataclass(frozen=True)
class VaultConfig:
    """One vault: where its plugin listens and how to authenticate to it."""

    id: str
    name: str
    api_key: str
    url: str = DEFAULT_URL
    ca_cert: Path | None = DEFAULT_CA_CERT
    # The vault's folder on disk. Only needed to read its PDFs.
    folder: Path | None = None

    @classmethod
    def from_env(cls, vault_id: str) -> VaultConfig | None:
        prefix = f"OBSIDIAN_{vault_id.upper()}_"
        api_key = os.getenv(prefix + "API_KEY")
        if not api_key:
            logger.warning(
                "obsidian vault %r skipped: %sAPI_KEY is not set", vault_id, prefix
            )
            return None
        ca_cert = os.getenv(prefix + "CA_CERT")
        folder = os.getenv(prefix + "DIR")
        return cls(
            id=vault_id,
            name=os.getenv(prefix + "NAME") or vault_id,
            api_key=api_key,
            url=os.getenv(prefix + "URL", DEFAULT_URL).rstrip("/"),
            ca_cert=Path(ca_cert)
            if ca_cert
            else PROJECT_ROOT / "certs" / f"obsidian-{vault_id}.crt",
            folder=Path(folder).expanduser() if folder else None,
        )


def _warn_about_misconfigured_vaults(vaults: list[VaultConfig]) -> None:
    """Log the mistakes that otherwise only show up as a vault Coffy can't reach.

    Two vaults on one port means only one plugin gets it, and the other vault's key
    is then sent to the wrong vault. A missing certificate fails every HTTPS request.
    """
    seen: dict[str, str] = {}
    for vault in vaults:
        if vault.url in seen:
            logger.warning(
                "obsidian vaults %r and %r share %s: give each plugin its own port",
                seen[vault.url],
                vault.id,
                vault.url,
            )
        seen.setdefault(vault.url, vault.id)
        if (
            vault.url.startswith("https://")
            and vault.ca_cert is not None
            and not vault.ca_cert.exists()
        ):
            logger.warning(
                "obsidian vault %r: certificate %s is missing; download it from "
                "%s/obsidian-local-rest-api.crt",
                vault.id,
                vault.ca_cert,
                vault.url,
            )
        if vault.folder is not None and not vault.folder.is_dir():
            logger.warning(
                "obsidian vault %r: folder %s does not exist", vault.id, vault.folder
            )


@dataclass(frozen=True)
class ObsidianConfig:
    """Runtime configuration for the Obsidian tools, read from the environment."""

    vaults: tuple[VaultConfig, ...] = ()
    enabled: bool = True
    timeout_seconds: float = 10.0
    max_note_chars: int = 6000
    max_search_results: int = 5
    pdf_command: Path | None = DEFAULT_PDF_COMMAND
    pdf_api_key: str | None = None
    pdf_model: str = DEFAULT_PDF_MODEL
    # Tried in turn when Google says the model is overloaded or rate-limited.
    pdf_fallback_models: tuple[str, ...] = DEFAULT_PDF_FALLBACK_MODELS
    pdf_timeout_seconds: float = 150.0

    @classmethod
    def from_env(cls) -> ObsidianConfig:
        ids = [
            v.strip().lower()
            for v in os.getenv("OBSIDIAN_VAULTS", "").split(",")
            if v.strip()
        ]
        vaults: list[VaultConfig] = []
        if ids:
            for vault_id in ids:
                if not _VAULT_ID.match(vault_id):
                    logger.warning(
                        "obsidian vault id %r skipped: use letters, digits and _",
                        vault_id,
                    )
                    continue
                vault = VaultConfig.from_env(vault_id)
                if vault:
                    vaults.append(vault)
            _warn_about_misconfigured_vaults(vaults)
        elif os.getenv("OBSIDIAN_API_KEY"):
            ca_cert = os.getenv("OBSIDIAN_CA_CERT")
            vaults.append(
                VaultConfig(
                    id="default",
                    name="Obsidian",
                    api_key=os.environ["OBSIDIAN_API_KEY"],
                    url=os.getenv("OBSIDIAN_URL", DEFAULT_URL).rstrip("/"),
                    ca_cert=Path(ca_cert) if ca_cert else DEFAULT_CA_CERT,
                    folder=Path(os.environ["OBSIDIAN_DIR"]).expanduser()
                    if os.getenv("OBSIDIAN_DIR")
                    else None,
                )
            )
        pdf_command = os.getenv("PDF_READER_COMMAND")
        return cls(
            vaults=tuple(vaults),
            enabled=_env_bool("OBSIDIAN_ENABLED", True),
            timeout_seconds=float(os.getenv("OBSIDIAN_TIMEOUT_SECONDS", "10")),
            max_note_chars=int(os.getenv("OBSIDIAN_MAX_NOTE_CHARS", "6000")),
            pdf_command=Path(pdf_command) if pdf_command else DEFAULT_PDF_COMMAND,
            pdf_api_key=os.getenv("GOOGLE_API_KEY") or None,
            pdf_model=os.getenv("PDF_READER_MODEL") or DEFAULT_PDF_MODEL,
            pdf_fallback_models=tuple(
                m.strip()
                for m in os.getenv(
                    "PDF_READER_FALLBACK_MODELS", ",".join(DEFAULT_PDF_FALLBACK_MODELS)
                ).split(",")
                if m.strip()
            ),
            pdf_timeout_seconds=float(os.getenv("PDF_READER_TIMEOUT_SECONDS", "150")),
        )

    def find_vault(self, wanted: str) -> VaultConfig:
        """The vault the user means, by id or name. Empty means the first one."""
        if not self.vaults:
            raise ObsidianError("No Obsidian vaults are set up.")
        wanted = wanted.strip().lower()
        if not wanted:
            return self.vaults[0]
        for vault in self.vaults:
            if wanted in (vault.id, vault.name.lower()):
                return vault
        # Spoken names are loose: "the CAMS vault" or "trading" should still land.
        words = set(re.findall(r"[a-z0-9]+", wanted)) - {"vault", "the", "my"}
        for vault in self.vaults:
            names = {vault.id, *re.findall(r"[a-z0-9]+", vault.name.lower())}
            if words and words <= names:
                return vault
        raise ObsidianError(
            "I do not know that vault. The vaults I can reach are "
            + ", ".join(v.name for v in self.vaults)
            + "."
        )

    @property
    def pdf_folders(self) -> list[Path]:
        return [v.folder for v in self.vaults if v.folder is not None]

    @property
    def pdf_ready(self) -> bool:
        return bool(
            self.pdf_api_key
            and self.pdf_command
            and self.pdf_command.exists()
            and self.pdf_folders
        )


def _clean(path: str) -> list[str]:
    cleaned = path.strip().strip("/").replace("\\", "/")
    parts = [p for p in cleaned.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise ObsidianError("That location is outside the vault.")
    return parts


def note_path(path: str) -> str:
    """A vault-relative note path: no leading slash, ``.md`` added if missing."""
    parts = _clean(path)
    if not parts:
        raise ObsidianError("I need a name for the note.")
    if "." not in parts[-1]:
        parts[-1] += ".md"
    return "/".join(parts)


def pdf_path(path: str) -> str:
    """A vault-relative PDF path, ``.pdf`` added if there is no extension."""
    parts = _clean(path)
    if not parts:
        raise ObsidianError("I need the name of the PDF.")
    if "." not in parts[-1]:
        parts[-1] += ".pdf"
    if not parts[-1].lower().endswith(".pdf"):
        raise ObsidianError("That file is not a PDF.")
    return "/".join(parts)


def folder_path(path: str) -> str:
    """A vault-relative folder path with a trailing slash, or empty for the root."""
    parts = _clean(path)
    return "/".join(parts) + "/" if parts else ""


def _encode(path: str) -> str:
    # Encode each segment on its own so a slash stays a folder boundary.
    return "/".join(quote(segment, safe="") for segment in path.split("/"))


def _trim(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[Truncated: {len(text) - limit} more characters.]"


class ObsidianClient:
    """A small client for the endpoints Coffy uses, against one vault."""

    def __init__(self, vault: VaultConfig, config: ObsidianConfig) -> None:
        if not vault.api_key:
            raise ValueError("no Obsidian API key configured")
        self._vault = vault
        self._config = config

    def _ssl(self) -> ssl.SSLContext | bool:
        if not self._vault.url.startswith("https://"):
            return False
        ca = self._vault.ca_cert
        if ca is not None and ca.exists():
            return ssl.create_default_context(cafile=str(ca))
        return ssl.create_default_context()

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        body: str | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> tuple[int, Any]:
        all_headers = {"Authorization": f"Bearer {self._vault.api_key}"}
        all_headers.update(headers or {})
        timeout = aiohttp.ClientTimeout(total=self._config.timeout_seconds)
        name = self._vault.name
        try:
            async with (
                aiohttp.ClientSession(timeout=timeout) as http,
                http.request(
                    method,
                    f"{self._vault.url}{endpoint}",
                    data=body.encode() if body is not None else None,
                    headers=all_headers,
                    params=params,
                    ssl=self._ssl(),
                ) as response,
            ):
                if response.content_type == "application/json":
                    payload: Any = await response.json()
                else:
                    payload = await response.text()
                status = response.status
        except aiohttp.ClientConnectorCertificateError as e:
            logger.error("obsidian certificate rejected for %s: %s", self._vault.id, e)
            raise ObsidianError(
                f"The {name} vault's security certificate was not accepted. "
                "Its certificate may have been reset."
            ) from e
        except (aiohttp.ClientConnectionError, TimeoutError) as e:
            logger.warning("obsidian vault %s unreachable: %s", self._vault.id, e)
            raise ObsidianError(
                f"I cannot reach the {name} vault. It may be closed in Obsidian, or "
                "its Local REST API plugin may be off."
            ) from e

        if status == 401:
            raise ObsidianError(
                f"The {name} vault refused its API key. It may have been changed in "
                "the plugin's settings."
            )
        if status == 429:
            raise ObsidianError(f"The {name} vault is refusing requests for a minute.")
        return status, payload

    async def search(self, query: str) -> list[dict[str, Any]]:
        status, payload = await self._request(
            "POST",
            "/search/simple/",
            params={
                "query": query,
                "contextLength": "80",
                "maxResults": str(self._config.max_search_results),
                "maxMatchesPerFile": "2",
            },
        )
        if status != 200 or not isinstance(payload, list):
            raise ObsidianError("The search did not work.")
        return payload

    async def list_folder(self, path: str) -> list[str]:
        folder = folder_path(path)
        status, payload = await self._request("GET", f"/vault/{_encode(folder)}")
        if status == 404:
            raise ObsidianError("There is no folder by that name.")
        if status != 200 or not isinstance(payload, dict):
            raise ObsidianError("I could not list that folder.")
        return list(payload.get("files", []))

    async def read_note(self, path: str) -> str:
        note = note_path(path)
        status, payload = await self._request(
            "GET", f"/vault/{_encode(note)}", headers={"Accept": "text/markdown"}
        )
        if status == 404:
            raise ObsidianError("There is no note by that name.")
        if status != 200:
            raise ObsidianError("I could not open that note.")
        return _trim(str(payload), self._config.max_note_chars)

    async def create_note(self, path: str, content: str) -> str:
        note = note_path(path)
        status, _ = await self._request(
            "PUT",
            f"/vault/{_encode(note)}",
            body=content,
            # Never overwrite: an existing note answers 412 instead.
            headers={"Content-Type": "text/markdown", "If-None-Match": "*"},
        )
        if status == 412:
            raise ObsidianError(
                "A note by that name already exists. I can add to it instead."
            )
        if status not in (200, 201, 204):
            raise ObsidianError("I could not create the note.")
        return note

    async def append_to_note(self, path: str, content: str) -> str:
        note = note_path(path)
        status, _ = await self._request(
            "POST",
            f"/vault/{_encode(note)}",
            body=content if content.startswith("\n") else "\n" + content,
            headers={"Content-Type": "text/markdown"},
        )
        if status not in (200, 204):
            raise ObsidianError("I could not add to the note.")
        return note


class PdfReader:
    """Reads a PDF through the pdf-reader MCP server, one process per request.

    The server is started with only the vault folders allowed and only the
    environment it needs, so it cannot read, or upload, anything else.
    """

    def __init__(self, config: ObsidianConfig) -> None:
        self._config = config

    def _environment(self, model: str | None) -> dict[str, str]:
        from mcp.client.stdio import get_default_environment

        env = {
            **get_default_environment(),
            "GEMINI_API_KEY": self._config.pdf_api_key or "",
            "PDF_READER_ALLOWED_DIRS": os.pathsep.join(
                str(f) for f in self._config.pdf_folders
            ),
        }
        if model:
            env["GEMINI_MODEL"] = model
        return env

    def _attempts(self) -> list[str]:
        """Models to try, in order: the main one, each fallback, then the main one
        again after the pauses in between, since Google's "high demand" answers
        often pass within seconds.
        """
        primary = self._config.pdf_model
        fallbacks = [m for m in self._config.pdf_fallback_models if m != primary]
        return [primary, *fallbacks, primary]

    async def read(self, vault: VaultConfig, path: str) -> str:
        if vault.folder is None:
            raise ObsidianError(
                f"I am not set up to read PDFs in the {vault.name} vault."
            )
        absolute = vault.folder.joinpath(*pdf_path(path).split("/"))
        try:
            return await asyncio.wait_for(
                self._read_with_retries(absolute, vault, path),
                self._config.pdf_timeout_seconds,
            )
        except TimeoutError as e:
            raise ObsidianError(
                "Reading that PDF took too long. Google's service may be busy; "
                "try again in a minute."
            ) from e

    async def _read_with_retries(
        self, absolute: Path, vault: VaultConfig, path: str
    ) -> str:
        attempts = self._attempts()
        for number, model in enumerate(attempts, start=1):
            try:
                text, is_error = await self._call(absolute, model)
            except Exception as e:
                logger.error("pdf reader failed: %s", e)
                raise ObsidianError("The PDF reader is not working right now.") from e
            if not is_error:
                if number > 1:
                    logger.info(
                        "pdf read on attempt %d with %s",
                        number,
                        model or "default model",
                    )
                return _trim(text, self._config.max_note_chars)

            logger.info(
                "pdf reader error for %s in %s (attempt %d, %s): %s",
                path,
                vault.id,
                number,
                model or "default model",
                text,
            )
            if not text.startswith("Error calling Gemini API"):
                raise _policy_refusal(text)
            if not _GEMINI_BUSY.search(text):
                raise ObsidianError("Google could not read that PDF.")
            if number < len(attempts):
                await asyncio.sleep(PDF_RETRY_DELAY_SECONDS)

        raise ObsidianError(
            "Google's PDF service is too busy right now. Try again in a minute."
        )

    async def _call(self, absolute: Path, model: str | None) -> tuple[str, bool]:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        params = StdioServerParameters(
            command=str(self._config.pdf_command), env=self._environment(model)
        )
        with open(os.devnull, "w") as errlog:
            async with (
                stdio_client(params, errlog=errlog) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                result = await session.call_tool(
                    "pdf_reader", {"file_path": str(absolute)}
                )
        content = getattr(result, "content", None) or []
        text = "\n".join(
            getattr(item, "text", "")
            for item in content
            if getattr(item, "type", "") == "text"
        )
        return text, bool(
            getattr(result, "isError", False) or getattr(result, "is_error", False)
        )


def _policy_refusal(text: str) -> ObsidianError:
    """The pdf-reader's own refusal ("Cannot read PDF: ..."), phrased for speech."""
    if "not found" in text:
        return ObsidianError("There is no PDF by that name.")
    if "larger than" in text:
        return ObsidianError("That PDF is too large for me to read.")
    if "not a PDF" in text:
        return ObsidianError("That file is not a real PDF.")
    if "empty" in text:
        return ObsidianError("That PDF is empty.")
    return ObsidianError("I am not allowed to read that file.")


def build_obsidian_tools(config: ObsidianConfig | None = None) -> list[Any]:
    """The Obsidian tools, or nothing if they are disabled or no vault is set up.

    The approval gate (``propose_action`` and ``record_decision``) is shared with
    the browser and comes from ``build_browser_tools``.
    """
    config = config or ObsidianConfig.from_env()
    if not config.enabled:
        logger.info("obsidian tools disabled by OBSIDIAN_ENABLED")
        return []
    if not config.vaults:
        logger.warning(
            "obsidian tools unavailable: set OBSIDIAN_VAULTS and an API key per "
            "vault in .env.local (see .env.example)"
        )
        return []

    clients = {v.id: ObsidianClient(v, config) for v in config.vaults}
    logger.info(
        "obsidian vaults: %s", ", ".join(f"{v.id} at {v.url}" for v in config.vaults)
    )

    def client_for(vault: str) -> tuple[VaultConfig, ObsidianClient]:
        chosen = config.find_vault(vault)
        return chosen, clients[chosen.id]

    @function_tool()
    async def obsidian_list_vaults(ctx: RunContext[BrowserState]) -> str:
        """List the user's Obsidian vaults that you can reach.

        Use this when the user does not say which vault they mean and it matters,
        or asks what vaults they have.
        """
        lines = []
        for v in config.vaults:
            pdfs = "can read PDFs" if v.folder and config.pdf_ready else "no PDFs"
            lines.append(f"{v.name} (id {v.id}, {pdfs})")
        return "\n".join(lines)

    @function_tool()
    async def obsidian_search(
        ctx: RunContext[BrowserState], query: str, vault: str = ""
    ) -> str:
        """Search one of the user's Obsidian vaults for words or a phrase.

        Use this to find a note before reading it when the user does not give its
        exact name.

        Args:
            query: The words to look for, for example "meeting with Sam".
            vault: Which vault, by name or id. Leave empty for the default vault.
        """
        try:
            _, client = client_for(vault)
            results = await client.search(query)
        except ObsidianError as e:
            return str(e)
        if not results:
            return f"No notes mention {query!r}."
        lines = []
        for result in results:
            snippets = [
                m.get("context", "").strip().replace("\n", " ")
                for m in result.get("matches", [])
            ]
            lines.append(f"{result.get('filename')}: {' … '.join(snippets)}")
        return "\n".join(lines)

    @function_tool()
    async def obsidian_list_folder(
        ctx: RunContext[BrowserState], folder: str = "", vault: str = ""
    ) -> str:
        """List the notes, PDFs and subfolders in a folder of an Obsidian vault.

        Args:
            folder: The folder inside the vault, for example "Projects". Leave
                empty for the top of the vault.
            vault: Which vault, by name or id. Leave empty for the default vault.
        """
        try:
            _, client = client_for(vault)
            entries = await client.list_folder(folder)
        except ObsidianError as e:
            return str(e)
        if not entries:
            return "That folder is empty."
        # Subfolders end with a slash.
        return "\n".join(entries)

    @function_tool()
    async def obsidian_read_note(
        ctx: RunContext[BrowserState], path: str, vault: str = ""
    ) -> str:
        """Read a note from one of the user's Obsidian vaults.

        Args:
            path: The note's location in the vault, for example
                "Projects/Coffy ideas.md". The .md ending may be left off.
            vault: Which vault, by name or id. Leave empty for the default vault.
        """
        try:
            _, client = client_for(vault)
            return await client.read_note(path)
        except ObsidianError as e:
            return str(e)

    @function_tool()
    async def obsidian_create_note(
        ctx: RunContext[BrowserState], path: str, content: str, vault: str = ""
    ) -> str:
        """Create a new note in one of the user's Obsidian vaults.

        Any folders in the path that do not exist yet are created with it, so this
        is also how to make a new folder. It never overwrites an existing note.
        Requires the user's approval through propose_action first.

        Args:
            path: Where to put the note, for example "Projects/Coffy/ideas.md".
            content: The note's text, in Markdown.
            vault: Which vault, by name or id. Leave empty for the default vault.
        """
        if not take_approval(_state(ctx)):
            return _NOT_APPROVED
        try:
            chosen, client = client_for(vault)
            note = await client.create_note(path, content)
        except ObsidianError as e:
            return str(e)
        logger.info("obsidian note created in %s: %s", chosen.id, note)
        return f"Created the note {note} in {chosen.name}."

    @function_tool()
    async def obsidian_append_to_note(
        ctx: RunContext[BrowserState], path: str, content: str, vault: str = ""
    ) -> str:
        """Add text to the end of a note in one of the user's Obsidian vaults.

        Creates the note if it does not exist yet. Requires the user's approval
        through propose_action first.

        Args:
            path: The note's location, for example "Daily/2026-10-20.md".
            content: The text to add, in Markdown.
            vault: Which vault, by name or id. Leave empty for the default vault.
        """
        if not take_approval(_state(ctx)):
            return _NOT_APPROVED
        try:
            chosen, client = client_for(vault)
            note = await client.append_to_note(path, content)
        except ObsidianError as e:
            return str(e)
        logger.info("obsidian note appended in %s: %s", chosen.id, note)
        return f"Added to the note {note} in {chosen.name}."

    tools: list[Any] = [
        obsidian_list_vaults,
        obsidian_search,
        obsidian_list_folder,
        obsidian_read_note,
        obsidian_create_note,
        obsidian_append_to_note,
    ]

    if not config.pdf_ready:
        logger.info(
            "obsidian PDF reading off: needs GOOGLE_API_KEY, the pdf-reader binary "
            "at %s, and OBSIDIAN_<ID>_DIR for at least one vault",
            config.pdf_command,
        )
        return tools

    pdf_reader = PdfReader(config)

    @function_tool()
    async def obsidian_read_pdf(
        ctx: RunContext[BrowserState], path: str, vault: str = ""
    ) -> str:
        """Read a PDF stored in one of the user's Obsidian vaults.

        This uploads the PDF to Google's Gemini service to turn it into text, so it
        requires the user's approval through propose_action first, naming the PDF.
        Find the file first with obsidian_list_folder or obsidian_search.

        Args:
            path: The PDF's location in the vault, for example
                "Study/AML handbook.pdf".
            vault: Which vault, by name or id. Leave empty for the default vault.
        """
        if not take_approval(_state(ctx)):
            return _NOT_APPROVED
        try:
            chosen = config.find_vault(vault)
            text = await pdf_reader.read(chosen, path)
        except ObsidianError as e:
            return str(e)
        logger.info("obsidian pdf read in %s: %s", chosen.id, path)
        return text

    tools.append(obsidian_read_pdf)
    return tools
