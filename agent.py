import base64
import html
import json
import mimetypes
import os
import re
import subprocess
import sys
import time
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, unquote, urlparse

import requests

try:  # long-term memory and daily learning (brain.py); the agent works without it
    import brain
except ImportError:
    brain = None
try:  # Buddy screen control + Termux:API (phone_bridge.py)
    from phone_bridge import Phone, RISKY_TOOLS
except ImportError:
    Phone = None
    RISKY_TOOLS = frozenset()

USER_AGENT = ("Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36")

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_config.json")

DEFAULT_CONFIG = {
    "ollama_url": "http://127.0.0.1:11434",
    "model": "llama3.2:3b",
    "system": "You are a helpful assistant.",
    "stream": True,
    "keep_history": True,
    "max_history": 20,
    "tools_enabled": True,
    "auto_approve": "off",
    "command_timeout": 300,
    "max_output_chars": 4000,
    "max_steps": 10,
    "max_retries": 2,
    "options": {},
    # FreeLLMAPI gateway (OpenAI-compatible, many free cloud models behind one key).
    # Its models are named auto, auto:smart, auto:fast, fusion or freellmapi/<id>;
    # "backend": "ollama" ignores them and uses only local models.
    "backend": "freellmapi",
    "freellmapi_url": "http://localhost:3001",
    "freellmapi_key": "",
    # Model routing: each message is matched to a route by keywords, and the
    # route's model (or the first installed fallback) handles it.
    "routing": True,
    "default_route": "shell",
    "router_model": None,
    "routes": {},
}

DEFAULT_ROUTES = {
    "web": {
        "description": "looking things up online: searching, news, prices, docs, websites",
        "model": "qwen2.5:3b",
        "fallbacks": ["llama3.2:3b", "qwen2.5-coder:3b"],
        "tools": True,
        "options": {"temperature": 0.3},
        "keywords": [
            "search", "google", "internet", "web", "online", "look up", "lookup", "browse",
            "website", "url", "https?", "latest", "news", "weather", "price", "current",
            "today", "recent", "find out", "documentation", "docs",
        ],
    },
    "code": {
        "description": "writing, fixing, explaining or running code and scripts",
        "model": "qwen2.5-coder:3b",
        "fallbacks": ["qwen2.5:3b", "llama3.2:3b"],
        "tools": True,
        "options": {"temperature": 0.2},
        "keywords": [
            "code", "script", "python", "javascript", "js", "html", "css", "bash script",
            "function", "class", "bug", "debug", "error", "traceback", "exception", "compile",
            "program", "app", "api", "regex", "refactor", "json", "sql", "rust", "java",
            "c\\+\\+", "node", "npm", "import", "syntax",
        ],
    },
    "shell": {
        "description": "doing things on the device: files, folders, installing, running commands",
        "model": "qwen2.5:3b",
        "fallbacks": ["llama3.2:3b", "qwen2.5-coder:3b"],
        "tools": True,
        "options": {"temperature": 0.3},
        "keywords": [
            "install", "run", "file", "files", "folder", "directory", "download", "termux",
            "pkg", "pip", "command", "terminal", "delete", "remove", "create", "make", "move",
            "copy", "rename", "git", "clone", "list", "check", "update", "upgrade", "disk",
            "storage", "process", "zip", "unzip", "convert", "find", "search", "open", "set up",
            "setup", "configure", "server", "start", "stop", "kill",
        ],
    },
    "chat": {
        "description": "conversation, questions, explanations, writing text, translation",
        "model": "llama3.2:3b",
        "fallbacks": ["qwen2.5:3b"],
        "tools": False,
        "options": {"temperature": 0.7},
        "keywords": [
            "hi", "hello", "hey", "thanks", "thank you", "explain", "what is", "what are",
            "who is", "why", "how does", "tell me", "story", "poem", "joke", "translate",
            "summarize", "summary", "meaning", "define", "opinion", "advice", "write an email",
        ],
    },
}

TOOLS_PROMPT = """
You can run shell commands on the user's device with the run_command tool.
The device is Android running Termux:
- Install system packages with `pkg install -y <name>` (no sudo, no apt-get).
- Install Python packages with `pip install <name>`. Command-line programs (tree, git,
  ffmpeg, curl, nodejs) come from pkg, never pip. Python libraries that fail to build
  with pip often exist in pkg as python-<name>, e.g. `pkg install -y python-pillow`.
- Check whether a program exists with `command -v <name>` before installing it.
- If a task needs a tool that is missing, install it, then continue the task.
- Always call a tool to act. A run_command command must be ONE string, e.g. "pkg install -y tree".
- To create or change a file, use write_file with the full file contents (never echo/cat/heredoc).
  It creates missing folders. To create an empty folder, use run_command with `mkdir -p <path>`.
- To look at a file (e.g. before fixing it), use read_file.
- You have internet access. When asked to look something up, or when you need facts
  you are unsure of (versions, docs, news), use web_search, then fetch_url on the best
  result to read it. Answer from what you read and mention the source URL.
- When a project needs a file from the internet (dataset, image, archive, script),
  use download_file. Install tools with pkg/pip; use `git clone <url>` for repositories.
- Read each command's output. If it failed, read the error, fix the cause
  (install what is missing, correct the command or code) and try again until it works.
- Run one command at a time. When the task is done, answer the user in plain text.
- Before a task about the user's projects or files, use memory_search to find what you
  already know. When the user tells you a lasting fact or preference, save it with remember.
- To use the phone's apps (WhatsApp, Settings, TradingView...), use the phone tool:
  first {"action": "read_screen"}, then tap/type_text/open_app with ids from that read.
  Before sending, posting, buying or deleting anything, ask the user first.
- If you are stuck or unsure after trying, use ask_expert (a stronger online AI).
- To make media use generate_image, generate_video or text_to_speech; they save the file
  and return its path. To understand a picture use describe_image; to turn a voice
  recording into text use transcribe_audio.
"""

GATEWAY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": "Create a picture from a text description and save it as a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "What the picture should show."},
                    "path": {"type": "string", "description": "Where to save it, e.g. \"my_app/logo.png\". Optional."},
                    "size": {"type": "string", "description": "e.g. \"1024x1024\". Optional."},
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_video",
            "description": "Create a short video (MP4) from a text description. Takes minutes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "What the video should show."},
                    "path": {"type": "string", "description": "Where to save it, e.g. \"clips/lake.mp4\". Optional."},
                    "duration": {"type": "integer", "description": "Seconds, e.g. 5. Optional."},
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "text_to_speech",
            "description": "Turn text into spoken audio and save it as a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The words to speak."},
                    "path": {"type": "string", "description": "Where to save it, e.g. \"audio/hello.mp3\". Optional."},
                    "voice": {"type": "string", "description": "Voice name, e.g. \"alloy\". Optional."},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "transcribe_audio",
            "description": "Turn an audio file (voice note, mp3, wav, m4a, ogg) into text.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "The audio file's path."}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "describe_image",
            "description": "Look at an image file or image URL and answer a question about it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Image file path or http(s) URL."},
                    "question": {"type": "string", "description": "What to find out. Optional."},
                },
                "required": ["path"],
            },
        },
    },
]

BRAIN_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "memory_search",
            "description": "Search what you learned before and the user's files (projects, PDFs, notes). Works offline.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Words to look for."}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": "Save a lasting fact, preference or lesson so you know it in future chats.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "One sentence to remember."},
                    "kind": {"type": "string", "enum": ["fact", "preference", "lesson"]},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_expert",
            "description": "Ask a stronger online AI (Claude) when you are stuck. Needs internet; costs a little.",
            "parameters": {
                "type": "object",
                "properties": {"question": {"type": "string", "description": "The full question with details."}},
                "required": ["question"],
            },
        },
    },
]

PHONE_TOOL = {
    "type": "function",
    "function": {
        "name": "phone",
        "description": "Use the phone screen through the Buddy app. Actions: read_screen, tap "
                       "(element), type_text (text), scroll (direction), press (button: back/home), "
                       "open_app (app), open_url (url), list_apps, wait (ms), battery, notifications.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "e.g. read_screen, tap, open_app"},
                "element": {"type": "integer", "description": "Element id from read_screen (for tap)."},
                "text": {"type": "string", "description": "Text to type (type_text)."},
                "app": {"type": "string", "description": "App name (open_app)."},
                "url": {"type": "string"},
                "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                "button": {"type": "string", "enum": ["back", "home", "recents", "notifications"]},
                "ms": {"type": "integer"},
            },
            "required": ["action"],
        },
    },
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a bash command in Termux and return its exit code and output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The bash command to run."},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a text file with the given contents. "
                           "Missing parent folders are created. Relative paths are inside the projects folder. "
                           "Not for downloads: use download_file to save something from a URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path, e.g. \"my_app/main.py\"."},
                    "content": {"type": "string", "description": "The complete file contents."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file and return its contents with line numbers. "
                           "Relative paths are inside the projects folder.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path, e.g. \"my_app/main.py\"."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the internet and return the top results (title, URL, snippet).",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to search for."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Open a web page and return its text, to read a search result or docs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The full URL, starting with http."},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "download_file",
            "description": "Download a file from a URL and save it. Relative paths are inside "
                           "the projects folder; a path ending in / keeps the file's own name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The file's URL."},
                    "path": {"type": "string", "description": "Where to save it, e.g. \"my_app/data/\"."},
                },
                "required": ["url", "path"],
            },
        },
    },
]
if brain:
    TOOLS += BRAIN_TOOLS
if Phone:
    TOOLS.append(PHONE_TOOL)
TOOLS += GATEWAY_TOOLS
TOOL_NAMES = {t["function"]["name"] for t in TOOLS}
GATEWAY_NAMES = {t["function"]["name"] for t in GATEWAY_TOOLS}

# Commands that /auto safe may run without asking: installs and read-only checks,
# with no chaining, pipes or redirects.
SAFE_COMMAND = re.compile(
    r"^(pkg (install|search|show|list-installed)|pip3? (install|show|list)|"
    r"python3? -m pip (install|show|list)|command -v|which|ls|pwd|cat|echo|"
    r"uname|whoami|df|free|\S+ --version)\b"
)
SHELL_META = re.compile(r"[;&|<>`$()\n]")

ANNOUNCES_ACTION = re.compile(
    r"\b(I'll|I will|let's|let me(?! know)|I am going to|I'm going to|next,? I)\b", re.I)
# If the reply also reads as a wrap-up or a report that the work is done, it is
# finishing, not stalling, so don't nudge it to keep acting.
CLOSING = re.compile(
    r"(feel free|let me know|if you (need|have)|further (assistance|questions|help)|"
    r"anything else|happy to help|you're welcome|hope (this|that)|"
    r"\b(done|completed?|finished|successful(ly)?|all set|here (is|are|'s))\b)", re.I)

HELP = """Commands:
  /help                 show this help
  /models               list installed models
  /model <name>         use one model for everything (turns routing off)
  /route auto|off|<name> auto-pick model per task, or force a route (web, code, shell, chat)
  /routes               show routes and the model each would use
  /backend freellmapi|ollama  use FreeLLMAPI cloud models (local ones as fallback) or only local
  /system <text>        set the system prompt
  /set <option> <value> set a model option (temperature, top_p, num_ctx, ...)
  /unset <option>       remove a model option
  /tools on|off         let the AI run commands, read/write files and use the internet
  /auto off|safe|all    which commands run without asking (default: off)
  /history on|off       toggle conversation memory
  /stream on|off        toggle streaming output
  /show                 show current settings
  /save                 save current settings to agent_config.json
  /reload               reload settings from agent_config.json
  /clear                forget the conversation
  /brain                what the agent has learned and indexed
  /learn                learn now (normally runs every night)
  /follow <topic>       read news on a topic daily when online
  /remember <text>      save a fact for future chats
  /recall <words>       search memory and your files
  /messages             show messages the phone receptionist took
  /messages <request>   ask the AI about them, e.g. /messages who should I call back first?
  /exit                 quit
"""


def load_config():
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            user = json.load(f)
        config.update({k: v for k, v in user.items() if k != "options"})
        config["options"].update(user.get("options", {}))
    if not config["routes"]:
        config["routes"] = json.loads(json.dumps(DEFAULT_ROUTES))
    return config


def save_config(config):
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")


def parse_value(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


class GatewayError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(f"FreeLLMAPI {status}: {message}")
        self.status = status


def is_gateway(model):
    return bool(model) and (model == "fusion" or model.split(":")[0] == "auto"
                            or model.startswith("freellmapi/"))


def gateway_on(config):
    return config.get("backend") == "freellmapi" and bool(config.get("freellmapi_url"))


FREELLMAPI_KEY_PATH = os.path.expanduser("~/.config/freellmapi/key")


def gateway(config, method, path, **kwargs):
    # The key lives outside agent_config.json so the config can be shared without it.
    key = config.get("freellmapi_key") or os.environ.get("FREELLMAPI_KEY", "")
    if not key and os.path.exists(FREELLMAPI_KEY_PATH):
        with open(FREELLMAPI_KEY_PATH) as f:
            key = f.read().strip()
    headers = {"Authorization": f"Bearer {key}", **kwargs.pop("headers", {})}
    url = config["freellmapi_url"].rstrip("/") + "/v1" + path
    r = requests.request(method, url, headers=headers, timeout=kwargs.pop("timeout", 600), **kwargs)
    if r.status_code >= 400:
        try:
            message = r.json()["error"]["message"]
        except (ValueError, KeyError, TypeError):
            message = r.text[:300]
        r.close()
        raise GatewayError(r.status_code, message)
    return r


def gateway_models(config):
    r = gateway(config, "GET", "/models?available=true", timeout=10)
    ids = [m["id"] for m in r.json().get("data", []) if m.get("available")]
    models = ["freellmapi/" + i for i in ids if i not in ("auto", "fusion")]
    if not models:  # "auto" is always listed, but can't answer without provider keys
        return []
    return ["auto", "auto:smart", "auto:fast", "auto:reliable"] + \
        (["fusion"] if "fusion" in ids else []) + models


def list_models(config):
    models = []
    if gateway_on(config):
        try:
            models += gateway_models(config)
        except (requests.RequestException, RuntimeError, ValueError) as e:
            print(f"[FreeLLMAPI unavailable: {e}]")
    try:
        r = requests.get(f"{config['ollama_url']}/api/tags", timeout=10)
        r.raise_for_status()
        models += [m["name"] for m in r.json().get("models", [])]
    except requests.RequestException:
        if not models:
            raise
    return models


def ensure_ollama(config):
    # Start `ollama serve` in the background if it isn't already running.
    try:
        requests.get(f"{config['ollama_url']}/api/version", timeout=3)
        return True
    except requests.ConnectionError:
        pass
    print("Ollama isn't running, starting it...")
    try:
        subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except FileNotFoundError:
        print("`ollama` is not installed. Run: pkg install ollama")
        return False
    for _ in range(20):
        time.sleep(0.5)
        try:
            requests.get(f"{config['ollama_url']}/api/version", timeout=3)
            return True
        except requests.ConnectionError:
            continue
    print("Ollama did not start. Try running `ollama serve` yourself.")
    return False


_model_info = {}


def model_info(config, model):
    if is_gateway(model):
        return {}
    if model not in _model_info:
        try:
            r = requests.post(f"{config['ollama_url']}/api/show", json={"model": model}, timeout=30)
            r.raise_for_status()
            _model_info[model] = r.json()
        except requests.RequestException:
            _model_info[model] = {}
    return _model_info[model]


def model_capabilities(config, model):
    if is_gateway(model):  # the gateway routes tool and image requests to models that can do them
        return {"completion", "tools", "vision"}
    info = model_info(config, model)
    if not info:
        return set()
    # Custom models built FROM a cloud model (e.g. alaa-minimax) report no capabilities.
    if not info.get("capabilities") and info.get("remote_model"):
        return {"completion", "tools"}
    return set(info.get("capabilities") or ["completion"])


def same_model(a, b):
    def full(name):
        return name if ":" in name else name + ":latest"
    return full(a) == full(b)


def pick_model(config, route, installed):
    # The route's model, else the first installed fallback that can do the job.
    candidates = [route.get("model")] + route.get("fallbacks", []) + [config["model"]]
    for model in candidates:
        if not model or not any(same_model(model, m) for m in installed):
            continue
        if route.get("tools") and config["tools_enabled"] \
                and "tools" not in model_capabilities(config, model):
            continue
        return model
    return config["model"]


def keyword_route(config, text):
    text = text.lower()
    best, best_score = None, 0
    for name, route in config["routes"].items():
        score = sum(1 for kw in route.get("keywords", [])
                    if re.search(rf"(?<!\w){kw}(?!\w)", text))
        if score > best_score:
            best, best_score = name, score
    return best


def llm_route(config, text):
    names = list(config["routes"])
    menu = "\n".join(f"- {n}: {config['routes'][n].get('description', '')}" for n in names)
    prompt = (f"Pick the category that best fits the user's request.\n{menu}\n\n"
              f"Request: {text}\n\nAnswer with JSON like {{\"route\": \"<category>\"}}.")
    try:
        r = requests.post(f"{config['ollama_url']}/api/generate", timeout=60, json={
            "model": config["router_model"],
            "prompt": prompt,
            "stream": False,
            "format": {"type": "object", "properties": {"route": {"enum": names}},
                       "required": ["route"]},
            "options": {"temperature": 0, "num_predict": 20},
        })
        r.raise_for_status()
        name = json.loads(r.json().get("response", "{}")).get("route")
        return name if name in names else None
    except (requests.RequestException, ValueError):
        return None


def choose_route(config, text):
    if config.get("forced_route") in config["routes"]:
        return config["forced_route"]
    name = keyword_route(config, text)
    if not name and config.get("router_model"):
        name = llm_route(config, text)
    if name not in config["routes"]:
        name = config["default_route"] if config["default_route"] in config["routes"] \
            else next(iter(config["routes"]))
    return name


def workspace_dir(config):
    path = os.path.expanduser(config.get("workspace") or "~")
    os.makedirs(path, exist_ok=True)
    return path


def is_safe(command):
    return bool(SAFE_COMMAND.match(command)) and not SHELL_META.search(command)


def approve(config, action, safe):
    mode = config["auto_approve"]
    if mode == "all" or (mode == "safe" and safe):
        print(f"\n[running] {action}")
        return True
    print(f"\n[AI wants to run] {action}")
    try:
        answer = input("Allow it? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    return answer in ("y", "yes")


def run_command(config, command):
    if not approve(config, f"$ {command}", is_safe(command)):
        return "The user declined to run this command."
    try:
        result = subprocess.run(
            command,
            shell=True,
            executable="bash",
            capture_output=True,
            text=True,
            timeout=config["command_timeout"],
            stdin=subprocess.DEVNULL,
            cwd=workspace_dir(config),
        )
        output = (result.stdout + result.stderr).strip()
        status = f"exit code {result.returncode}"
        if result.returncode != 0:
            status += (" (FAILED - read the error below, fix the cause, e.g. install a missing"
                       " package or correct the command, then try again)")
    except subprocess.TimeoutExpired as e:
        output = ((e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""))
        status = f"timed out after {config['command_timeout']}s"

    limit = config["max_output_chars"]
    if len(output) > limit:
        output = output[: limit // 2] + "\n...[output truncated]...\n" + output[-limit // 2:]
    print(f"[{status}]")
    if output:
        print(output if len(output) < 800 else output[:400] + "\n...\n" + output[-400:])
    return f"{status}\n{output}" if output else status


def to_openai_messages(messages):
    # History is kept in Ollama's shape; OpenAI-style APIs need tool call ids,
    # string arguments, and every tool result paired with its call.
    out, pending = [], []
    for i, m in enumerate(messages):
        if m["role"] == "tool":
            if not pending:  # its call was cut off by max_history
                continue
            call_id = pending.pop(0)
            out.append({"role": "tool", "tool_call_id": m.get("tool_call_id") or call_id,
                        "content": m.get("content") or ""})
            continue
        out += [{"role": "tool", "tool_call_id": p, "content": "(no result)"} for p in pending]
        pending = []
        if m["role"] == "assistant" and m.get("tool_calls"):
            calls = []
            for j, call in enumerate(m["tool_calls"]):
                fn = call.get("function", {})
                args = fn.get("arguments") or {}
                calls.append({"id": call.get("id") or f"call_{i}_{j}", "type": "function",
                              "function": {"name": fn.get("name", ""),
                                           "arguments": args if isinstance(args, str) else json.dumps(args)}})
            pending = [c["id"] for c in calls]
            out.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": calls})
        else:
            out.append({"role": m["role"], "content": m.get("content") or ""})
    out += [{"role": "tool", "tool_call_id": p, "content": "(no result)"} for p in pending]
    return out


def call_gateway(config, messages, model, options, use_tools):
    payload = {
        "model": model[len("freellmapi/"):] if model.startswith("freellmapi/") else model,
        "messages": to_openai_messages(messages),
        "stream": config["stream"],
    }
    for key in ("temperature", "top_p"):
        if key in options:
            payload[key] = options[key]
    if options.get("num_predict", -1) > 0:
        payload["max_tokens"] = options["num_predict"]
    if use_tools:
        payload["tools"] = TOOLS

    content, calls, printed = "", {}, False

    def show(piece):
        nonlocal printed
        if not printed:
            print("\nAI: ", end="", flush=True)
            printed = True
        print(piece, end="", flush=True)

    with gateway(config, "POST", "/chat/completions", json=payload, stream=config["stream"]) as r:
        routed = unquote(r.headers.get("X-Routed-Via", ""))
        if routed:
            print(f"[via {routed}]")
        if config["stream"]:
            for line in r.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if chunk.get("error"):
                    raise RuntimeError(chunk["error"].get("message", chunk["error"]))
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        show(delta["content"])
                        content += delta["content"]
                    for tc in delta.get("tool_calls") or []:
                        slot = calls.setdefault(tc.get("index", len(calls)),
                                                {"id": None, "name": "", "arguments": ""})
                        fn = tc.get("function") or {}
                        slot["id"] = tc.get("id") or slot["id"]
                        slot["name"] += fn.get("name") or ""
                        slot["arguments"] += fn.get("arguments") or ""
        else:
            message = ((r.json().get("choices") or [{}])[0]).get("message") or {}
            content = message.get("content") or ""
            if content:
                show(content)
            for n, tc in enumerate(message.get("tool_calls") or []):
                fn = tc.get("function") or {}
                calls[n] = {"id": tc.get("id"), "name": fn.get("name", ""),
                            "arguments": fn.get("arguments") or ""}
    if printed:
        print("\n")

    reply = {"role": "assistant", "content": content}
    if calls:
        reply["tool_calls"] = [
            {"id": c["id"] or f"call_{n}",
             "function": {"name": c["name"], "arguments": parse_value(c["arguments"]) if c["arguments"] else {}}}
            for n, c in sorted(calls.items())]
    return reply


def call_model(config, messages, model, options, use_tools):
    if is_gateway(model):
        return call_gateway(config, messages, model, options, use_tools)
    payload = {
        "model": model,
        "messages": messages,
        "stream": config["stream"],
        # Cloud models reject num_predict -1; omitting it means the same (no limit).
        "options": {k: v for k, v in options.items() if not (k == "num_predict" and v < 0)},
    }
    if use_tools:
        payload["tools"] = TOOLS
    url = f"{config['ollama_url']}/api/chat"

    content, tool_calls = "", []
    printed = False
    if config["stream"]:
        with requests.post(url, json=payload, stream=True, timeout=600) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                if "error" in chunk:
                    raise RuntimeError(chunk["error"])
                message = chunk.get("message", {})
                tool_calls.extend(message.get("tool_calls") or [])
                piece = message.get("content", "")
                if piece:
                    if not printed:
                        print("\nAI: ", end="", flush=True)
                        printed = True
                    print(piece, end="", flush=True)
                    content += piece
        if printed:
            print("\n")
    else:
        r = requests.post(url, json=payload, timeout=600)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(data["error"])
        message = data.get("message", {})
        content = message.get("content", "")
        tool_calls = message.get("tool_calls") or []
        if content:
            print(f"\nAI: {content}\n")

    reply = {"role": "assistant", "content": content}
    if tool_calls:
        reply["tool_calls"] = tool_calls
    return reply


def resolve_path(config, path):
    # Models sometimes URL-encode spaces ("AI%20agent"); undo that.
    path = unquote(path.strip())
    return os.path.join(workspace_dir(config), os.path.expanduser(path))


def relative_workspace_paths(config, command):
    # Commands already run in the workspace. Absolute paths to it break on the
    # space in its name (or get URL-encoded), so turn them into relative ones.
    workspace = config.get("workspace")
    if not workspace:
        return command
    workspace = os.path.expanduser(workspace).rstrip("/")
    for form in {workspace, quote(workspace)}:
        command = command.replace(form + "/", "")
        command = re.sub(re.escape(form) + r"(?![\w%-])", ".", command)
    return command


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
             "pre", "section", "article", "table"}

    def __init__(self):
        super().__init__()
        self.parts, self.skipping = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skipping += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skipping:
            self.skipping -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skipping:
            self.parts.append(data)

    def text(self):
        text = re.sub(r"[ \t\r\f\v]+", " ", "".join(self.parts))
        return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def html_to_text(page):
    parser = _TextExtractor()
    parser.feed(page)
    return parser.text()


def gateway_search(config, query):
    # FreeLLMAPI's search (Tavily, Exa, ...); None when it has no search keys.
    if not config.get("freellmapi_url"):
        return None
    try:
        data = gateway(config, "POST", "/search", timeout=60,
                       json={"query": query, "max_results": 8, "include_answer": True}).json()
    except (requests.RequestException, RuntimeError, ValueError):
        return None
    results = [f"{i}. {hit.get('title', '')}\n   {hit.get('url', '')}\n   {(hit.get('content') or '')[:300]}"
               for i, hit in enumerate(data.get("results") or [], 1)]
    if not results:
        return None
    print(f"[{len(results)} results via {data.get('provider', 'FreeLLMAPI')}]")
    answer = f"Answer: {data['answer']}\n\n" if data.get("answer") else ""
    return answer + "\n".join(results) + "\n\nUse fetch_url on a result to read the page."


def web_search(config, query):
    print(f"\n[searching] {query}")
    found = gateway_search(config, query)
    if found:
        return found
    try:
        r = requests.post("https://html.duckduckgo.com/html/", data={"q": query},
                          headers={"User-Agent": USER_AGENT}, timeout=20)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"[FAILED: {e}]")
        return f"FAILED to search: {e}. Check the internet connection, or try curl."
    links = re.findall(r'class="result__a" href="([^"]+)"[^>]*>(.*?)</a>', r.text, re.S)
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', r.text, re.S)
    results = []
    for i, (href, title) in enumerate(links[:8]):
        # Result links may go through a DuckDuckGo redirect: //duckduckgo.com/l/?uddg=<url>
        target = parse_qs(urlparse(html.unescape(href)).query).get("uddg", [html.unescape(href)])[0]
        if "duckduckgo.com/y.js" in target:  # ads
            continue
        snippet = html_to_text(snippets[i]) if i < len(snippets) else ""
        results.append(f"{len(results) + 1}. {html_to_text(title)}\n   {target}\n   {snippet}")
    print(f"[{len(results)} results]")
    if not results:
        return "No results. Try different search words."
    return "\n".join(results) + "\n\nUse fetch_url on a result to read the page."


def fetch_url(config, url):
    print(f"\n[fetching] {url}")
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"[FAILED: {e}]")
        return f"FAILED to fetch {url}: {e}"
    kind = r.headers.get("Content-Type", "")
    if "html" in kind:
        text = html_to_text(r.text)
    elif kind.startswith("text/") or "json" in kind or "xml" in kind:
        text = r.text
    else:
        return f"{url} is not a web page ({kind}). Use download_file to save it."
    limit = config["max_output_chars"] * 2
    if len(text) > limit:
        text = text[:limit] + "\n...[page truncated]..."
    print(f"[read {len(text)} chars]")
    return f"{url}:\n{text}" if text else f"{url} has no readable text."


def download_file(config, url, path):
    full = resolve_path(config, path)
    name = os.path.basename(unquote(urlparse(url).path)) or "download"
    # A path with no extension ("logo", "my_app/data") is meant as a folder
    # when the URL's file has one, even without a trailing slash.
    as_folder = path.endswith("/") or os.path.isdir(full) or \
        (not os.path.splitext(full)[1] and os.path.splitext(name)[1] and not os.path.isfile(full))
    if as_folder:
        full = os.path.join(full, name)
    if not approve(config, f"download {url} -> {full}", False):
        return "The user declined to download this file."
    try:
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with requests.get(url, headers={"User-Agent": USER_AGENT}, stream=True,
                          timeout=config["command_timeout"]) as r:
            r.raise_for_status()
            size = 0
            with open(full, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    f.write(chunk)
                    size += len(chunk)
    except (requests.RequestException, OSError) as e:
        print(f"[FAILED: {e}]")
        return f"FAILED to download {url}: {e}"
    print(f"[saved {size:,} bytes]")
    return f"Downloaded {size:,} bytes to {full}"


def write_file(config, path, content):
    full = resolve_path(config, path)
    lines = content.count("\n") + (0 if content.endswith("\n") or not content else 1)
    if not approve(config, f"write {full} ({lines} lines)", False):
        return "The user declined to write this file."
    try:
        os.makedirs(os.path.dirname(full), exist_ok=True)
        if content and not content.endswith("\n"):
            content += "\n"
        with open(full, "w") as f:
            f.write(content)
    except OSError as e:
        print(f"[FAILED: {e}]")
        return f"FAILED to write {full}: {e}"
    print(f"[wrote {lines} lines]")
    return f"Wrote {lines} lines to {full}"


def read_file(config, path):
    full = resolve_path(config, path)
    if not approve(config, f"read {full}", True):
        return "The user declined to read this file."
    try:
        if os.path.isdir(full):
            entries = sorted(os.listdir(full))
            print(f"[{len(entries)} entries]")
            return f"{full} is a folder containing: {', '.join(entries) or 'nothing'}"
        with open(full, "rb") as f:
            if b"\0" in f.read(4096):
                size = os.path.getsize(full)
                print(f"[binary, {size:,} bytes]")
                return f"{full} is a binary file of {size:,} bytes; it can't be shown as text."
        with open(full, errors="replace") as f:
            text = f.read()
    except OSError as e:
        print(f"[FAILED: {e}]")
        return f"FAILED to read {full}: {e}"
    numbered = "\n".join(f"{i:4}  {line}" for i, line in enumerate(text.splitlines(), 1))
    limit = config["max_output_chars"] * 2
    if len(numbered) > limit:
        numbered = numbered[:limit] + "\n...[file truncated]..."
    print(f"[read {text.count(chr(10))} lines]")
    return f"{full}:\n{numbered}" if numbered else f"{full} is empty."


AUDIO_EXT = {"audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav",
             "audio/ogg": ".ogg", "audio/opus": ".opus", "audio/aac": ".aac", "audio/flac": ".flac"}


def media_path(config, path, kind, ext):
    # No path, or a folder: name the file after its kind and the time.
    if not isinstance(path, str) or not path.strip():
        path = "media/"
    path = path.strip()
    if path.endswith("/"):
        path += f"{kind}_{time.strftime('%Y%m%d_%H%M%S')}{ext}"
    elif not os.path.splitext(path)[1]:
        path += ext
    return resolve_path(config, path)


def save_media(config, full, data):
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as f:
        f.write(data)
    print(f"[saved {len(data):,} bytes to {full}]")


def generate_image(config, prompt, path, size):
    if not approve(config, f"generate image: {prompt[:80]}", False):
        return "The user declined to generate this image."
    print("[generating image...]")
    body = {"model": "auto", "prompt": prompt}
    if isinstance(size, str) and size.strip():
        body["size"] = size.strip()
    try:
        data = gateway(config, "POST", "/images/generations", json=body, timeout=300).json()
        item = (data.get("data") or [{}])[0]
        url = item.get("url") or ""
        if item.get("b64_json"):
            image = base64.b64decode(item["b64_json"])
        elif url.startswith("data:"):
            image = base64.b64decode(url.split(",", 1)[1])
        elif url:
            r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=120)
            r.raise_for_status()
            image = r.content
        else:
            return "FAILED: the image service returned no image. Try again or change the prompt."
        full = media_path(config, path, "image", ".jpg" if image[:2] == b"\xff\xd8" else ".png")
        save_media(config, full, image)
    except (requests.RequestException, RuntimeError, ValueError, OSError) as e:
        print(f"[FAILED: {e}]")
        return f"FAILED to generate the image: {e}"
    return f"Saved the image to {full} (made by {data.get('provider', '?')}/{data.get('model', '?')})."


def generate_video(config, prompt, path, duration):
    if not approve(config, f"generate video: {prompt[:80]}", False):
        return "The user declined to generate this video."
    print("[generating video, this can take a few minutes...]")
    body = {"model": "auto", "prompt": prompt}
    if isinstance(duration, int) and duration > 0:
        body["duration"] = duration
    try:
        r = gateway(config, "POST", "/videos/generations", json=body, timeout=330)
        full = media_path(config, path, "video", ".mp4")
        save_media(config, full, r.content)
    except (requests.RequestException, RuntimeError, OSError) as e:
        print(f"[FAILED: {e}]")
        return f"FAILED to generate the video: {e}"
    return f"Saved the video to {full} (made by {r.headers.get('X-Provider', '?')})."


def text_to_speech(config, text, path, voice):
    if not approve(config, f"speak: {text[:80]}", False):
        return "The user declined to create this audio."
    print("[creating speech...]")
    body = {"model": "auto", "input": text}
    if isinstance(voice, str) and voice.strip():
        body["voice"] = voice.strip()
    try:
        r = gateway(config, "POST", "/audio/speech", json=body, timeout=180)
        kind = r.headers.get("Content-Type", "").split(";")[0].strip()
        full = media_path(config, path, "speech", AUDIO_EXT.get(kind, ".mp3"))
        save_media(config, full, r.content)
    except (requests.RequestException, RuntimeError, OSError) as e:
        print(f"[FAILED: {e}]")
        return f"FAILED to create the audio: {e}"
    return f"Saved the audio to {full}. Play it with: termux-media-player play \"{full}\""


def transcribe_audio(config, path):
    full = resolve_path(config, path)
    if not approve(config, f"transcribe {full}", True):
        return "The user declined to transcribe this file."
    print(f"\n[transcribing] {full}")
    try:
        with open(full, "rb") as f:
            r = gateway(config, "POST", "/audio/transcriptions", timeout=300,
                        files={"file": (os.path.basename(full), f)}, data={"model": "auto"})
        text = r.json().get("text", "")
    except (requests.RequestException, RuntimeError, ValueError, OSError) as e:
        print(f"[FAILED: {e}]")
        return f"FAILED to transcribe {full}: {e}"
    print(f"[{len(text)} chars]")
    return f"Transcript of {full}:\n{text}" if text.strip() else f"No speech found in {full}."


def describe_image(config, path, question):
    # Send the bytes, not the URL: providers that can't fetch a URL answer blind.
    try:
        if path.startswith(("http://", "https://")):
            r = requests.get(path, headers={"User-Agent": USER_AGENT}, timeout=60)
            r.raise_for_status()
            image = r.content
            kind = r.headers.get("Content-Type", "").split(";")[0] or mimetypes.guess_type(path)[0]
        else:
            full = resolve_path(config, path)
            with open(full, "rb") as f:
                image = f.read()
            kind = mimetypes.guess_type(full)[0]
    except (requests.RequestException, OSError) as e:
        return f"FAILED to get the image {path}: {e}"
    if not (kind or "").startswith("image/"):
        kind = "image/png"
    image_url = f"data:{kind};base64,{base64.b64encode(image).decode()}"
    print(f"\n[looking at] {path}")
    body = {"model": "auto", "stream": False, "messages": [{"role": "user", "content": [
        {"type": "text", "text": question or "Describe this image in detail."},
        {"type": "image_url", "image_url": {"url": image_url}},
    ]}]}
    try:
        data = gateway(config, "POST", "/chat/completions", json=body, timeout=180).json()
        answer = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    except (requests.RequestException, RuntimeError, ValueError) as e:
        print(f"[FAILED: {e}]")
        return f"FAILED to look at the image: {e}"
    return answer or "The vision model gave no answer."


_phone = None


def run_phone(config, args):
    global _phone
    if Phone is None:
        return "phone_bridge.py isn't available next to agent.py."
    _phone = _phone or Phone()
    action = str(args.get("action") or args.get("tool") or "").strip()
    if action == "battery":
        return _phone.battery()
    if action == "notifications":
        return _phone.notifications()[:config["max_output_chars"]]
    allowed = {"read_screen", "tap", "long_press", "type_text", "scroll", "swipe", "press",
               "open_app", "open_url", "list_apps", "wait", "screenshot"}
    if action not in allowed | RISKY_TOOLS:
        return f"Bad tool call. phone action must be one of: {', '.join(sorted(allowed))}, battery, notifications."
    params = {k: v for k, v in args.items() if k not in ("action", "tool") and v not in (None, "")}
    if action in RISKY_TOOLS and not approve(config, f"phone {action} {params}", False):
        return "The user declined this phone action."
    print(f"\n[phone] {action} {params or ''}")
    result = _phone.call(action, **params)
    limit = config["max_output_chars"]
    return result if len(result) <= limit else result[:limit] + "\n...[screen truncated]..."


def run_tool(config, name, args, failed):
    if not isinstance(args, dict):
        args = {}
    if name == "run_command":
        command = relative_workspace_paths(config, normalize_command(args.get("command")))
        if not command:
            return 'Bad tool call. run_command needs one string argument, e.g. {"command": "ls"}.'
        if command in failed:
            return ("This exact command already failed. Do not repeat it; "
                    "read the earlier error and try a different approach.")
        result = run_command(config, command)
        if "FAILED" in result or result.startswith("timed out"):
            failed.add(command)
        return result
    path = args.get("path") or args.get("file") or args.get("filename")
    if name == "write_file":
        content = args.get("content", args.get("contents", args.get("text")))
        if not isinstance(path, str) or not path.strip() or not isinstance(content, str):
            return 'Bad tool call. write_file needs {"path": "...", "content": "..."} as strings.'
        if not content.strip():
            return ("Bad tool call: content is empty. Put the full file text in content. "
                    "To save a file from a URL use download_file; for an empty file run `touch <path>`.")
        return write_file(config, path, content)
    if name == "read_file":
        if not isinstance(path, str) or not path.strip():
            return 'Bad tool call. read_file needs {"path": "..."}.'
        return read_file(config, path)
    if name == "web_search":
        query = args.get("query") or args.get("q") or args.get("search")
        if not isinstance(query, str) or not query.strip():
            return 'Bad tool call. web_search needs {"query": "..."}.'
        return web_search(config, query.strip())
    if name in ("fetch_url", "download_file"):
        url = args.get("url") or args.get("link")
        if not isinstance(url, str) or not url.strip().startswith(("http://", "https://")):
            return f'Bad tool call. {name} needs a full "url" starting with http:// or https://.'
        if name == "fetch_url":
            return fetch_url(config, url.strip())
        if not isinstance(path, str) or not path.strip():
            path = "./"
        return download_file(config, url.strip(), path)
    if name == "memory_search" and brain:
        query = args.get("query") or args.get("q") or args.get("text")
        if not isinstance(query, str) or not query.strip():
            return 'Bad tool call. memory_search needs {"query": "..."}.'
        print(f"\n[memory] {query}")
        found = [f"[{k}] {t}" for k, t in brain.recall(query)] + \
                [f"{p}: {s}" for p, s in brain.search_files(query, 6)]
        return "\n".join(found) if found else "Nothing found in memory or files. Try other words."
    if name == "remember" and brain:
        text = args.get("text") or args.get("fact") or args.get("content")
        if not isinstance(text, str) or not text.strip():
            return 'Bad tool call. remember needs {"text": "..."}.'
        kind = args.get("kind")
        if kind not in ("fact", "preference", "lesson"):
            kind = "fact"
        result = brain.remember(text, kind, source="chat")
        print(f"\n[{result}]")
        return result
    if name == "ask_expert" and brain:
        question = args.get("question") or args.get("query") or args.get("text")
        if not isinstance(question, str) or not question.strip():
            return 'Bad tool call. ask_expert needs {"question": "..."}.'
        print(f"\n[asking expert] {question[:80]}")
        return brain.ask_expert(question)
    if name == "phone" and Phone:
        return run_phone(config, args)
    if name in GATEWAY_NAMES:
        if not config.get("freellmapi_url"):
            return f"{name} needs FreeLLMAPI: set freellmapi_url and freellmapi_key in agent_config.json."
        text = args.get("prompt") or args.get("text") or args.get("description") or args.get("input")
        if name in ("transcribe_audio", "describe_image"):
            source = path or args.get("url") or args.get("image")
            if not isinstance(source, str) or not source.strip():
                return f'Bad tool call. {name} needs {{"path": "..."}}.'
            if name == "transcribe_audio":
                return transcribe_audio(config, source.strip())
            question = args.get("question") or args.get("prompt")
            return describe_image(config, source.strip(), question if isinstance(question, str) else "")
        if not isinstance(text, str) or not text.strip():
            key = "text" if name == "text_to_speech" else "prompt"
            return f'Bad tool call. {name} needs {{"{key}": "..."}}.'
        if name == "generate_image":
            return generate_image(config, text.strip(), path, args.get("size"))
        if name == "generate_video":
            duration = args.get("duration")
            if isinstance(duration, str) and duration.isdigit():
                duration = int(duration)
            return generate_video(config, text.strip(), path, duration)
        return text_to_speech(config, text.strip(), path, args.get("voice"))
    return f"Unknown tool {name!r}. Available tools: {', '.join(sorted(TOOL_NAMES))}."


def normalize_command(command):
    # Small models sometimes send ["pkg", "install", "x"] instead of a string.
    if isinstance(command, list):
        return " ".join(str(part) for part in command)
    return command if isinstance(command, str) else ""


def text_tool_calls(content):
    # Small models sometimes write the tool call as JSON text instead of calling it.
    calls = []
    decoder = json.JSONDecoder(strict=False)
    pos = content.find("{")
    while pos != -1:
        try:
            data, end = decoder.raw_decode(content, pos)
        except json.JSONDecodeError:
            pos = content.find("{", pos + 1)
            continue
        if isinstance(data, dict) and data.get("name") in TOOL_NAMES:
            args = data.get("parameters") or data.get("arguments") or {}
            calls.append({"function": {"name": data["name"], "arguments": args}})
        pos = content.find("{", end)
    return calls


def call_with_retries(config, messages, model, options, use_tools) -> dict:
    # Retry on errors and on empty replies, which small models sometimes give.
    last_error = None
    for attempt in range(config["max_retries"] + 1):
        try:
            reply = call_model(config, messages, model, options, use_tools)
        except requests.ConnectionError:
            raise
        except GatewayError as e:
            # Bad key or no usable provider: retrying won't help, fall back now.
            if e.status in (401, 403, 503):
                raise
            last_error = e
            print(f"\n[model error: {e}, retrying]")
            continue
        except (requests.RequestException, RuntimeError, ValueError) as e:
            last_error = e
            print(f"\n[model error: {e}, retrying]")
            continue
        if reply["content"].strip() or reply.get("tool_calls"):
            return reply
        print("\n[empty reply, retrying]")
        messages = messages + [{"role": "user", "content": "Please continue: answer or use the tool."}]
    if last_error:
        raise last_error
    return {"role": "assistant", "content": ""}


def chat(config, history, user_input):
    if config["routing"]:
        route_name = choose_route(config, user_input)
        route = config["routes"][route_name]
        model = pick_model(config, route, list_models(config))
        use_tools = config["tools_enabled"] and route.get("tools", True)
        print(f"[{route_name} -> {model}]")
    else:
        route_name = "fixed"
        route = {}
        model = config["model"]
        use_tools = config["tools_enabled"]
    options = {**config["options"], **route.get("options", {})}

    system = config["system"] or ""
    if use_tools:
        system = (system + "\n" + TOOLS_PROMPT).strip()
    workspace = config.get("workspace")
    if workspace:
        system += (f"\nThe user's projects folder is {workspace}. Commands and file tools already"
                   " work inside it, so use relative paths like my_app/main.py and"
                   " create new projects as subfolders of it."
                   " Files there cannot be executed directly (./script fails);"
                   " run them with an interpreter, e.g. `bash run.sh` or `python main.py`.")
    if brain:
        try:
            memory = brain.context_for(user_input)
        except Exception:  # memory must never break a chat
            memory = ""
        if memory:
            system += "\n\n" + memory

    def system_for(model):
        # A system message replaces the Modelfile SYSTEM, so keep the model's own one first.
        built_in = model_info(config, model).get("system", "").strip()
        return (built_in + "\n\n" + system).strip() if built_in else system

    turn = [{"role": "user", "content": user_input}]
    past = history[-config["max_history"]:] if config["keep_history"] else []
    failed = set()
    nudges = 0

    try:
        for _ in range(config["max_steps"]):
            sys_msg = system_for(model)
            messages = ([{"role": "system", "content": sys_msg}] if sys_msg else []) + past + turn
            try:
                reply = call_with_retries(config, messages, model, options, use_tools)
            except (requests.RequestException, RuntimeError, ValueError) as e:
                # Cloud models fail when offline or out of quota: fall back to a local one.
                if not (is_gateway(model) or model_info(config, model).get("remote_model")) or not route:
                    raise
                local = {**route, "model": None}
                fallback = pick_model(config, local, [m for m in list_models(config) if not is_gateway(m)])
                if same_model(fallback, model):
                    raise
                print(f"\n[{model} failed ({e}); using {fallback}]")
                model = fallback
                continue
            if use_tools and not reply.get("tool_calls"):
                calls = text_tool_calls(reply["content"])
                if calls:
                    reply["tool_calls"] = calls
            turn.append(reply)
            if not reply.get("tool_calls"):
                # Small models often announce a step ("I'll create...") and stop.
                content = reply["content"]
                if use_tools and nudges < 2 and ANNOUNCES_ACTION.search(content) \
                        and not CLOSING.search(content):
                    nudges += 1
                    print("[nudging the model to act]")
                    turn.append({"role": "user", "content": "Don't describe it, do it now: "
                                 "call the tool for the next step."})
                    continue
                break
            for call in reply["tool_calls"]:
                fn = call.get("function", {})
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    args = parse_value(args)
                result = run_tool(config, fn.get("name", ""), args, failed)
                message = {"role": "tool", "content": result, "tool_name": fn.get("name", "")}
                if call.get("id"):
                    message["tool_call_id"] = call["id"]
                turn.append(message)
        else:
            print(f"\n[Stopped after {config['max_steps']} steps. Raise max_steps in the config if needed.]\n")
    finally:
        history.extend(turn)
        if brain:
            try:
                brain.log_turn(user_input, turn, model, route_name)
            except Exception:
                pass


def receptionist_messages(config):
    # Written by receptionist/receptionist.py, one JSON object per line.
    path = os.path.join(workspace_dir(config), "receptionist", "messages.jsonl")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def format_message(m):
    urgent = "URGENT " if m.get("urgent") else ""
    number = f" ({m['callback_number']})" if m.get("callback_number") else ""
    return f"{m.get('time', '?')}  {urgent}{m.get('caller_name', 'Unknown')}{number}: {m.get('message', '')}"


def on_off(arg):
    return arg.strip().lower() in ("on", "true", "1", "yes")


def handle_command(line, config, history):
    cmd, _, arg = line[1:].partition(" ")
    arg = arg.strip()

    if cmd in ("exit", "quit"):
        sys.exit(0)
    elif cmd == "help":
        print(HELP)
    elif cmd == "models":
        for name in list_models(config):
            print(("* " if name == config["model"] else "  ") + name)
    elif cmd == "model" and arg:
        config["model"] = arg
        config["routing"] = False
        print(f"Model: {arg} (routing off; /route auto to turn it back on)")
    elif cmd == "route" and arg:
        if arg == "auto":
            config["routing"], config["forced_route"] = True, None
        elif arg == "off":
            config["routing"] = False
        elif arg in config["routes"]:
            config["routing"], config["forced_route"] = True, arg
        else:
            print(f"Unknown route. Choose: auto, off, {', '.join(config['routes'])}")
            return
        print(f"Routing: {arg}")
    elif cmd == "routes":
        installed = list_models(config)
        for name, route in config["routes"].items():
            print(f"  {name:6} -> {pick_model(config, route, installed):22} {route.get('description', '')}")
    elif cmd == "backend" and arg in ("freellmapi", "ollama"):
        config["backend"] = arg
        print(f"Backend: {arg}")
    elif cmd == "system":
        config["system"] = arg
        print("System prompt updated." if arg else "System prompt cleared.")
    elif cmd == "set" and arg:
        key, _, value = arg.partition(" ")
        config["options"][key] = parse_value(value.strip())
        print(f"{key} = {config['options'][key]}")
    elif cmd == "unset" and arg:
        config["options"].pop(arg, None)
        print(f"Removed {arg}")
    elif cmd == "tools" and arg:
        config["tools_enabled"] = on_off(arg)
        print(f"Tools: {'on' if config['tools_enabled'] else 'off'}")
    elif cmd == "auto" and arg in ("off", "safe", "all"):
        config["auto_approve"] = arg
        print(f"Auto-approve: {arg}")
    elif cmd == "history" and arg:
        config["keep_history"] = on_off(arg)
        print(f"History: {'on' if config['keep_history'] else 'off'}")
    elif cmd == "stream" and arg:
        config["stream"] = on_off(arg)
        print(f"Streaming: {'on' if config['stream'] else 'off'}")
    elif cmd == "show":
        print(json.dumps(config, indent=2))
    elif cmd == "save":
        save_config(config)
        print(f"Saved to {CONFIG_PATH}")
    elif cmd == "reload":
        config.clear()
        config.update(load_config())
        _model_info.clear()
        print("Reloaded config.")
    elif cmd == "messages":
        messages = receptionist_messages(config)
        if not messages:
            print("No phone messages yet.")
        elif not arg:
            for m in messages[-20:]:
                print(format_message(m))
            print(f"\n{len(messages)} message(s). Ask about them with /messages <request>.")
        else:
            listing = "\n".join(format_message(m) for m in messages[-50:])
            chat(config, history, f"Messages my phone receptionist took for me:\n{listing}\n\n{arg}")
    elif cmd == "clear":
        history.clear()
        print("Conversation cleared.")
    elif cmd in ("brain", "learn", "follow", "remember", "recall"):
        if not brain:
            print("brain.py isn't available next to agent.py.")
        elif cmd == "brain":
            print(brain.status())
        elif cmd == "learn":
            print("Learning from recent sessions, files and news (this can take a few minutes)...")
            print(brain.learn())
        elif cmd == "follow":
            brain.main(["follow", arg] if arg else ["follow"])
        elif cmd == "remember" and arg:
            print(brain.remember(arg, "fact"))
        elif cmd == "recall" and arg:
            brain.main(["search", arg])
        else:
            print("Unknown command. Type /help.")
    else:
        print("Unknown command. Type /help.")


def main():
    config = load_config()
    history = []
    ensure_ollama(config)

    print("--- Android AI Agent Active ---")
    routing = f"auto ({', '.join(config['routes'])})" if config["routing"] else f"off, {config['model']}"
    print(f"Routing: {routing}   Tools: {'on' if config['tools_enabled'] else 'off'}"
          f"   Auto-approve: {config['auto_approve']}")
    if gateway_on(config):
        try:
            usable = [m for m in gateway_models(config) if m.startswith("freellmapi/")]
            print(f"FreeLLMAPI: {config['freellmapi_url']} ({len(usable)} cloud models ready)")
            if not usable:
                print("  No provider keys yet: add some on the Keys page of the FreeLLMAPI dashboard."
                      " Local models are used until then.")
        except (requests.RequestException, RuntimeError, ValueError) as e:
            print(f"FreeLLMAPI not reachable ({e}). Start it with `npm run dev` in ~/freellmapi;"
                  " local models are used until then.")
    print(f"Projects folder: {workspace_dir(config)}")
    print("Type /help for commands, /exit to quit.\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            break

        try:
            if user_input.startswith("/"):
                handle_command(user_input, config, history)
            else:
                chat(config, history, user_input)
        except requests.ConnectionError:
            print(f"\nCan't reach Ollama at {config['ollama_url']}. Restarting it...\n")
            ensure_ollama(config)
        except KeyboardInterrupt:
            print("\n[Interrupted]\n")
        except Exception as e:
            print(f"\nError: {e}\n")


if __name__ == "__main__":
    main()
