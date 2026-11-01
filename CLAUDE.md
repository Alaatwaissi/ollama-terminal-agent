# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`agent.py` is a single-file terminal chat agent that runs shell commands, reads/writes files, searches the web, fetches pages, and downloads files, using FreeLLMAPI cloud models with local **Ollama** models as fallback. It runs on Termux (Android) and on Windows/macOS/Linux. Its settings live in `agent_config.json` (tracked: keep `freellmapi_key` empty; the key is read from `~/.config/freellmapi/key` or `$FREELLMAPI_KEY`); the only dependency is `requests`. `brain.py` (memory) and `phone_bridge.py` (Android control) are optional companions.

The agent's `workspace` (folder for commands and relative paths) comes from `agent_config.json`; when unset it defaults to the user's home folder.

## Running

```bash
python agent.py          # needs: pkg install ollama; pip install requests
```

- `agent.py` starts `ollama serve` in the background if it can't reach `ollama_url`.
- There are no tests, linter, or build step. To check a change, run `python -m py_compile agent.py` and then start the agent.
- Inside the agent, `/help` lists the slash commands. `/save` writes the in-memory config back to `agent_config.json`, overwriting it, and `/reload` re-reads it.

## Architecture (agent.py)

- **Config**: `load_config()` shallow-merges `agent_config.json` over `DEFAULT_CONFIG`, except `options`, which is merged key by key. `DEFAULT_ROUTES` is used only when the file has no `routes`. Once the file defines `routes`, editing `DEFAULT_ROUTES` in the code has no effect; edit the JSON instead.
- **Routing** (`choose_route` → `pick_model`): routes are `web`, `code`, `shell`, and `chat`; only `chat` has tools off. When keyword scores tie, the route listed first in the dict wins, so `web` comes first. Each user message is scored against each route's `keywords`, which are regexes matched with word boundaries, so escape special characters (e.g. `c\\+\\+`). If no keyword matches, the optional `router_model` classifies the message using Ollama structured output; if that also gives nothing, `default_route` is used. `pick_model` takes the first installed model among the route's model, its `fallbacks`, and `config["model"]`. When tools are on, it skips any model whose `/api/show` capabilities don't include `tools`.
- **Agent loop** (`chat`): this loop calls `/api/chat` with `TOOLS` for up to `max_steps` steps. `call_with_retries` retries on errors and on empty replies. If the model writes a tool call as JSON text instead of making a real call, `text_tool_calls` turns that text into tool calls. History keeps the full turn, including tool messages, and is cut to `max_history`.
- **Tools** (`run_tool`): `run_command`, `write_file`, `read_file`, `web_search`, `fetch_url`, and `download_file`. `web_search` scrapes `html.duckduckgo.com` (no API key) and `fetch_url` converts HTML to text with the stdlib `HTMLParser`; neither needs extra packages. Relative paths and the command cwd resolve against `workspace`. Models URL-encode the space in `AI agent` or write absolute workspace paths, so `resolve_path` unquotes paths and `relative_workspace_paths` rewrites commands to relative paths. If a reply announces a step ("I'll…") without a tool call, `chat` nudges the model up to twice. Argument parsing is lenient on purpose because small models send wrong shapes: list commands, and `file`/`filename`/`contents`/`text` as key aliases. If an exact command fails or times out, it is blocked from being run again in that turn.
- **FreeLLMAPI gateway**: with `"backend": "freellmapi"`, models named `auto`, `auto:smart`, `auto:fast`, `fusion` or `freellmapi/<id>` (`is_gateway`) go through `call_gateway` to the OpenAI-compatible server at `freellmapi_url`. `to_openai_messages` converts the Ollama-shaped history (adds tool call ids, drops orphan tool results). Routes use the `auto*` models with local models as `fallbacks`. If the gateway fails, or has no provider keys (then `gateway_models` lists nothing), the turn falls back to a local model. The gateway tools are `generate_image`, `generate_video`, `text_to_speech`, `transcribe_audio` and `describe_image`, and they save into `workspace/media/` by default. `web_search` tries the gateway's `/v1/search` first and falls back to DuckDuckGo.
- **Brain and phone tools**: `memory_search`, `remember`, `ask_expert` (brain.py) and `phone` (phone_bridge.py). Both modules are optional imports; the agent runs without them.
- **Approval**: `auto_approve` can be `off`, `safe`, or `all`. In `safe` mode, a command runs without asking only if it matches `SAFE_COMMAND` and contains no `SHELL_META` characters. `write_file` always counts as unsafe. The shipped config sets this to `all`.

## brain.py and phone_bridge.py

- **brain.py**: long-term memory in `~/.agent_brain/brain.db` (SQLite lessons/facts + FTS5 file index), daily session logs, and `python brain.py learn` for nightly learning by the local model.
- **phone_bridge.py**: `Phone().call("read_screen")` etc. talks to the Buddy app's MCP server on `127.0.0.1:8765` (token read from `~/.claude.json`) plus Termux:API helpers like `battery()`.

## Environment constraints

- This is Termux: install packages with `pkg install`, not `apt`/`sudo`.
- Files under `/storage/emulated/0` (the workspace) cannot be executed directly. Run them through an interpreter (`bash x.sh`, `python x.py`). The system prompt built in `chat()` tells the model this.
- Local fallback models are small (~3B: `qwen2.5-coder:3b`, `qwen2.5:3b`; `llama3.2:3b` is only a code default). Prompt wording in `TOOLS_PROMPT` and tool error strings is written to steer these weak models, so keep it short and direct.
