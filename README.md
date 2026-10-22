<a href="https://livekit.io/">
  <img src="./.github/assets/livekit-mark.png" alt="LiveKit logo" width="100" height="100">
</a>

# LiveKit Agents starter for Python

A starter project for building voice AI apps with [LiveKit Agents for Python](https://github.com/livekit/agents) and [LiveKit Cloud](https://cloud.livekit.io/).

The starter includes:

- A simple [voice AI assistant](https://docs.livekit.io/agents/start/voice-ai/) to extend and customize.
- A voice pipeline built on [LiveKit Inference](https://docs.livekit.io/agents/models/inference/), which gives you access to [models](https://docs.livekit.io/agents/models/) from top labs with no extra configuration:
  - The default LLM is Gemma 4 31B, an open-weight model [hosted by LiveKit](https://docs.livekit.io/agents/models/llm/livekit/) and tuned for voice AI.
  - The default TTS is [Fish Audio S2.1 Pro](https://docs.livekit.io/agents/models/tts/fishaudio/), an expressive and cost-effective voice.
  - More than 50 other models are available from OpenAI, Cartesia, Deepgram, and other providers.
  - [Realtime models](https://docs.livekit.io/agents/models/realtime/) such as OpenAI GPT-Live. Many other models are available through the [plugin ecosystem](https://docs.livekit.io/agents/models/#plugins).
- [Expressive mode](https://docs.livekit.io/agents/models/tts/expressive/), on by default, so your agent's voice carries emotion and pacing that fit the conversation.
- [Keyterms](https://docs.livekit.io/agents/models/stt/keyterms/), on by default, so speech recognition gets your names, brands, and jargon right, including names it picks up during the conversation, like a caller's.
- [LiveKit Turn Detector](https://docs.livekit.io/agents/logic/turns/turn-detector/), which knows when the user has finished speaking, in 14 languages.
- [Adaptive interruption handling](https://docs.livekit.io/agents/logic/turns/adaptive-interruption-handling/), which tells a real interruption from an "uh-huh" or background noise, so your agent doesn't stop talking when it shouldn't.
- [Background voice cancellation](https://docs.livekit.io/transport/media/noise-cancellation/).
- Session transcripts, traces, and recordings from LiveKit [Agent Observability](https://docs.livekit.io/testing/observability/).
- [Simulations](https://docs.livekit.io/testing/simulations/) that test full conversations with your agent, run in CI on every merge to `main`.
- A `Dockerfile` for [deploying to LiveKit Cloud](https://docs.livekit.io/deploy/agents/).

The starter works with any [custom web or mobile frontend](https://docs.livekit.io/frontends/) or with [telephony](https://docs.livekit.io/telephony/).

## Using coding agents

This project works with coding agents like [Claude Code](https://claude.com/product/claude-code), [Cursor](https://www.cursor.com/), and [Codex](https://openai.com/codex/).

LiveKit offers both a CLI and an [MCP server](https://docs.livekit.io/reference/developer-tools/docs-mcp/) for browsing and searching its documentation. Search returns short excerpts, so fetch the full page to read the details:

```console
lk docs search "testing my agent"
lk docs get-page /testing/unit-tests
```

The project also includes an [`AGENTS.md`](AGENTS.md) file, so your coding agent follows LiveKit's best practices for workflows, handoffs, and testing, and tries its changes with the [agent debugger](https://docs.livekit.io/testing/debugger/).

For more guidance at each stage of the work, from building and debugging to testing and deploying, install LiveKit's [agent skills](https://docs.livekit.io/intro/coding-agents/#agent-skills). The CLI installs them for the coding agents on your machine and adds the Docs MCP server to their config. `lk agent init` offers to do this when it creates your project, or you can run it yourself:

```console
lk skills install
```

Run `lk skills update` to get the latest skills, and commit `skills-lock.json` along with them. See the [coding agents guide](https://docs.livekit.io/intro/coding-agents/) for more details.

## Dev setup

Install the [LiveKit CLI](https://docs.livekit.io/intro/basics/cli/), version 2.18.8 or later:

- **macOS:** `brew install livekit-cli`
- **Linux:** `curl -sSL https://get.livekit.io/cli | bash`
- **Windows:** `winget install LiveKit.LiveKitCLI`

Check your version with `lk --version`. To update an existing install, see [Update the CLI](https://docs.livekit.io/reference/developer-tools/livekit-cli/#updates).

Then create a project from this template. The CLI clones the template and configures your environment:

```console
lk cloud auth
lk agent init my-agent --template agent-starter-python
```

<details>
<summary>Set up the project manually</summary>

Clone the repository and install dependencies into a virtual environment with [uv](https://docs.astral.sh/uv/):

```console
git clone https://github.com/livekit-examples/agent-starter-python.git
cd agent-starter-python
uv sync
```

Sign up for [LiveKit Cloud](https://cloud.livekit.io/), then copy `.env.example` to `.env.local` and fill it in. To have the CLI write your project's URL and API keys into the file instead, run:

```console
lk cloud auth
lk app env --write --destination .env.local
```

</details>

## Run the agent

The `lk agent console`, `lk agent dev`, and `lk agent debugger` commands run your agent on your own machine. To talk to it in your terminal:

```console
lk agent console
```

To connect it to LiveKit Cloud so a frontend, a phone call, or the [Agent Console](https://docs.livekit.io/testing/agent-console/) can reach it:

```console
lk agent dev
```

To let a coding agent or a script test it one text turn at a time, use the [agent debugger](https://docs.livekit.io/testing/debugger/). Each turn prints the agent's reply along with the tool calls and handoffs behind it:

```console
lk agent debugger start
lk agent debugger say "Hi, what can you do?"
lk agent debugger stop
```

In production, run the agent directly:

```console
uv run src/agent.py start
```

## Browser control

Coffy can drive a real browser on the machine it runs on, through Microsoft's [Playwright MCP](https://github.com/microsoft/playwright-mcp) server. `src/browser.py` launches it over stdio and exposes it to the agent with LiveKit's [`MCPToolset`](https://docs.livekit.io/agents/logic/tools/mcp/). By default the window is visible so you can watch it work.

This needs Node and a browser for Playwright, installed once:

```console
npx --yes playwright install chromium
```

What the agent may do, and what it must ask for first:

| Activity | Tools | Behaviour |
|----------|-------|-----------|
| Read, search, screenshot, inspect console and network | `browser_snapshot`, `browser_find`, `browser_wait_for`, `browser_take_screenshot`, `browser_console_messages`, `browser_network_requests`, `browser_network_request` | Runs freely |
| Move around: navigate, go back, resize, tabs | `browser_navigate`, `browser_navigate_back`, `browser_resize`, `browser_tabs` | Runs freely |
| Change a page or an account: click, type, fill a form, choose an option, press a key, drag, upload, accept a dialog | `browser_click`, `browser_type`, `browser_fill_form`, `browser_select_option`, `browser_press_key`, `browser_hover`, `browser_drag`, `browser_drop`, `browser_file_upload`, `browser_handle_dialog`, `browser_emulate_media`, `browser_close` | Must call `propose_action` first, which asks the user out loud and holds the floor until they answer, then `record_decision` records the answer |
| Run arbitrary JavaScript | `browser_evaluate`, `browser_run_code_unsafe` | Never exposed unless `BROWSER_ALLOW_UNSAFE_TOOLS=true` |

Two things to know about the approval gate:

- It is enforced by the prompt and by `propose_action` speaking the question itself, not by the tool runtime. Browser tools are MCP tools, and MCP tool calls cannot be intercepted in Python, so a distracted model could still act without asking. The scenarios in [`scenarios.browser.yaml`](scenarios.browser.yaml) pin the behaviour that matters, and the agent console shows every tool call.
- `propose_action` deliberately does not use a nested `AgentTask`, which the docs otherwise recommend for pausing for user input. Google's realtime model has no separate LLM inside a tool activity, so a task cannot speak for itself, and LiveKit logs that resuming function calls from chat context is unsupported for it.

Configuration lives in `.env.example`:

| Variable | Default | Meaning |
|----------|---------|---------|
| `BROWSER_HEADLESS` | `false` | Hide the window. Use `true` for simulations and servers. |
| `BROWSER_ISOLATED` | `true` | Throwaway profile per call: no cookies or logins survive. Set `false` to keep a profile in `BROWSER_OUTPUT_DIR/profile`. |
| `BROWSER_KIND` | `chromium` | `chromium`, `chrome`, `msedge`, `firefox`, or `webkit`. |
| `BROWSER_MCP_VERSION` | `latest` | Version of `@playwright/mcp` to run. |
| `BROWSER_TIMEOUT_SECONDS` | `120` | Timeout for one browser step. The MCP default of 5s is too short for page loads. |
| `BROWSER_MAX_RESULT_CHARS` | `8000` | Cap on the text a browser step hands back to the model. |
| `BROWSER_OUTPUT_DIR` | `./.browser-workspace` | Where screenshots, downloads, and page files land. Also the browser's file-access root. |
| `BROWSER_ALLOW_UNSAFE_TOOLS` | `false` | Exposes the two arbitrary-JavaScript tools. |

The agent runs one browser per call, in `--isolated` mode, so nothing carries over between calls. Images are never sent to the model (`--image-responses omit`): screenshots are written to `BROWSER_OUTPUT_DIR` for a human to look at instead.

Browser control is local-only for now. A deployed agent has no Node or browser in its container, so `Dockerfile` would need Chromium and its system libraries before this works on LiveKit Cloud.

## Claude Code

Coffy can also use [Claude Code](https://claude.com/product/claude-code) on the machine it runs on. `src/claude_code.py` launches `claude mcp serve` over stdio and exposes it with the same `MCPToolset` approach as the browser. Claude Code works in this project folder unless `CLAUDE_CODE_WORKDIR` says otherwise.

| Activity | Tools | Behaviour |
|----------|-------|-----------|
| Read files, fetch or search the web | `Read`, `WebFetch`, `WebSearch` | Runs freely |
| Change the computer: run a command, edit or write a file, hand a task to a Claude Code agent | `Bash`, `Edit`, `Write`, `Agent` | Must call `propose_action` first, the same gate as the browser |
| Everything else Claude Code offers (scheduling, worktrees, messaging, notifications) | | Never exposed |

The agent finds `claude` through `CLAUDE_CODE_COMMAND`, then `PATH`, then the copy bundled with the Claude Code VS Code extension. If none is found, it starts without these tools and logs a warning. The approval gate has the same limit as the browser's: it is enforced by the prompt, not by the tool runtime. Like browser control, this is local-only: the deployed container has no Claude Code.

## Frontends and telephony

Pair the agent with a prebuilt frontend starter, or add telephony:

| Platform | Link | Description |
|----------|----------|-------------|
| **Web** | [`livekit-examples/agent-starter-react`](https://github.com/livekit-examples/agent-starter-react) | Web voice AI assistant with React & Next.js |
| **iOS/macOS** | [`livekit-examples/agent-starter-swift`](https://github.com/livekit-examples/agent-starter-swift) | Native iOS, macOS, and visionOS voice AI assistant |
| **Flutter** | [`livekit-examples/agent-starter-flutter`](https://github.com/livekit-examples/agent-starter-flutter) | Cross-platform voice AI assistant app |
| **React Native** | [`livekit-examples/voice-assistant-react-native`](https://github.com/livekit-examples/voice-assistant-react-native) | Native mobile app with React Native & Expo |
| **Android** | [`livekit-examples/agent-starter-android`](https://github.com/livekit-examples/agent-starter-android) | Native Android app with Kotlin & Jetpack Compose |
| **Web Embed** | [`livekit-examples/agent-starter-embed`](https://github.com/livekit-examples/agent-starter-embed) | Voice AI widget for any website |
| **Telephony** | [Documentation](https://docs.livekit.io/telephony/) | Add inbound or outbound calling to your agent |

For more options, see the [frontend guide](https://docs.livekit.io/frontends/).

## Testing and debugging

Simulations run full multi-turn conversations between a simulated user and your agent on LiveKit Cloud, then judge each transcript. The scenarios live in [`scenarios.yaml`](scenarios.yaml). Run them locally with the CLI:

```console
lk agent simulate text --scenarios scenarios.yaml
```

The `Simulations` workflow in [`.github/workflows/simulations.yml`](.github/workflows/simulations.yml) runs the same file on every merge to `main`, and on demand from the Actions tab. It doesn't run on every pull request push because each run uses real inference. See the [simulations guide](https://docs.livekit.io/testing/simulations/) for how to write scenarios and read results.

The browser scenarios live in a separate file, because they drive a real Chromium and need Node and a browser installed:

```console
BROWSER_HEADLESS=true lk agent simulate text --scenarios scenarios.browser.yaml --yes
```

They are not part of the `Simulations` workflow, so CI stays browser-free. Run them locally before shipping a change to `src/browser.py`.

To check a change turn by turn without a live session, use the [agent debugger](https://docs.livekit.io/testing/debugger/) shown in [Run the agent](#run-the-agent).

To debug a running agent, open it in the [Agent Console](https://docs.livekit.io/testing/agent-console/). It shows events, tool calls, and model timing as you talk to the agent. To stream logs from a deployed agent, run `lk agent logs`.

## Using this template for your own project

After you create your own project from this template:

- **Commit `uv.lock`.** The template doesn't track it, but your project should, for reproducible builds. If you deploy to LiveKit Cloud, commit `livekit.toml` too.
- **Add repository secrets.** Add `LIVEKIT_URL`, `LIVEKIT_API_KEY`, and `LIVEKIT_API_SECRET` as [repository secrets](https://docs.github.com/en/actions/how-tos/writing-workflows/choosing-what-your-workflow-does/using-secrets-in-github-actions) so the simulations can run in CI.

## Deploying to production

To deploy the agent to LiveKit Cloud or another environment with the included `Dockerfile`, see the [deployment guide](https://docs.livekit.io/deploy/agents/).

## Self-hosted LiveKit

You can self-host LiveKit instead of using LiveKit Cloud. See the [self-hosting guide](https://docs.livekit.io/transport/self-hosting/local/). If you self-host, use [model plugins](https://docs.livekit.io/agents/models/#plugins) instead of LiveKit Inference, and remove the [LiveKit Cloud noise cancellation](https://docs.livekit.io/transport/media/noise-cancellation/) plugin.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.
