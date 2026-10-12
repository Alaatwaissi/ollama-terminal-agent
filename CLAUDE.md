# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`agent.py` is a single-file terminal chat agent that talks to a local **Ollama** server and lets small local models run shell commands, read/write files, search the web, fetch pages, and download files. It runs on Termux (Android) and on Windows/macOS/Linux. Its settings live in `agent_config.json`; the only dependency is `requests`.

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
- **Approval**: `auto_approve` can be `off`, `safe`, or `all`. In `safe` mode, a command runs without asking only if it matches `SAFE_COMMAND` and contains no `SHELL_META` characters. `write_file` always counts as unsafe. The shipped config sets this to `safe`.

## Environment constraints

- This is Termux: install packages with `pkg install`, not `apt`/`sudo`.
- Files under `/storage/emulated/0` (the workspace) cannot be executed directly. Run them through an interpreter (`bash x.sh`, `python x.py`). The system prompt built in `chat()` tells the model this.
- Models are small (~3B: `qwen2.5-coder:3b`, `qwen2.5:3b`, `llama3.2:3b`). Prompt wording in `TOOLS_PROMPT` and tool error strings is written to steer these weak models, so keep it short and direct.
