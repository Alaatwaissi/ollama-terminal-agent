# Local Ollama Terminal Agent

A single-file terminal chat agent (`agent.py`) that talks to a local [Ollama](https://ollama.com)
server and lets small local models run shell commands, read/write files, search the web, fetch
pages, and download files. Routing picks a model per task (`web`, `code`, `shell`, `chat`).

Runs on Termux (Android) and on Windows/macOS/Linux. The only Python dependency is `requests`.

## Setup

1. **Install Ollama** and pull a few small models:
   ```bash
   ollama pull qwen2.5:3b
   ollama pull qwen2.5-coder:3b
   ollama pull llama3.2:3b
   ```
   On Windows, install the Ollama app from ollama.com; it runs as a background service.

2. **Install the Python dependency:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Run it:**
   ```bash
   python agent.py
   ```
   If Ollama isn't already running, the agent tries to start `ollama serve` itself.

## Usage

Type requests in plain language. Inside the agent, `/help` lists all commands. Useful ones:

- `/models` — list installed models
- `/route auto|off|<name>` — auto-pick a model per task, or force `web`/`code`/`shell`/`chat`
- `/auto off|safe|all` — which commands run without asking (default `safe`)
- `/tools on|off` — let the model run commands and use files/web
- `/save` — save current settings to `agent_config.json`
- `/exit` — quit

## Configuration

Settings live in `agent_config.json`. Notable keys:

- `workspace` — folder for commands and relative file paths. **Not set by default, so it uses your
  home folder.** Set it to any folder you like, e.g. on Windows `"C:/Users/you/ai-projects"`.
- `auto_approve` — `off` (ask every time), `safe` (auto-run only read-only/install commands),
  or `all` (run everything without asking). Shipped as `safe`.
- `routes` — keyword→model routing. Point each route at models you actually have installed.

## Notes

- The bundled models are small (~3B) and sometimes make mistakes or stop mid-task; the agent has
  guardrails for the common ones but isn't fully reliable.
- `web_search` scrapes DuckDuckGo's HTML endpoint (no API key needed).
