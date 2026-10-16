"""Long-term memory and daily learning for the offline agent (agent.py).

Everything is stored locally in ~/.agent_brain:
  brain.db        SQLite: lessons/facts (what the agent learned) and a full-text
                  index of your files (FTS5, so search works offline)
  sessions/       one JSONL log per day of what you asked and how it went
  reports/        what each nightly learning run did
  settings.json   folders to index, topics to follow, models to use

Learning happens in `python brain.py learn` (scheduled nightly):
  1. the local model reviews the day's sessions and writes lessons and facts
  2. your files are re-indexed (new and changed only)
  3. chat notifications (WhatsApp, Messenger, Facebook, Telegram, captured every
     2 hours by `brain.py capture`) and exported chats in Download/Chat Exports are
     turned into facts and to-dos by the local model. They never leave the phone.
  4. online only: news on your follow topics is read and summarized
  5. online only, with ANTHROPIC_API_KEY: Claude reviews the failures and
     writes corrective lessons (the "teacher")

Commands: python brain.py learn | capture | index | search <words> | lessons | remember <text>
          | follow <topic> | status
"""
import datetime
import json
import os
import re
import sqlite3
import subprocess
import sys

import requests

BRAIN_DIR = os.path.expanduser("~/.agent_brain")
DB_PATH = os.path.join(BRAIN_DIR, "brain.db")
SETTINGS_PATH = os.path.join(BRAIN_DIR, "settings.json")
SESSIONS_DIR = os.path.join(BRAIN_DIR, "sessions")
REPORTS_DIR = os.path.join(BRAIN_DIR, "reports")
STORAGE = "/storage/emulated/0"

DEFAULT_SETTINGS = {
    "ollama_url": "http://127.0.0.1:11434",
    "learn_model": "qwen2.5:3b",
    "teacher_model": "claude-haiku-5-5",
    "use_teacher": True,
    "gemini_model": "gemini-3.5-flash",
    # Tried in order when the main model is busy (503) or rate-limited (429).
    "gemini_fallbacks": ["gemini-3.5-flash-lite", "gemini-flash-latest", "gemini-flash-lite-latest"],
    "index_folders": [
        f"{STORAGE}/Download/AI agent",
        f"{STORAGE}/Download/Projects",
        f"{STORAGE}/Download/Trading",
        f"{STORAGE}/Download/Medical & Study",
        f"{STORAGE}/Download/Bank & Finance",
        f"{STORAGE}/Documents",
    ],
    "index_extensions": [".md", ".txt", ".py", ".json", ".csv", ".pdf", ".html", ".pine", ".mq5"],
    "max_file_mb": 60,
    "pdf_max_pages": 300,
    "follow_topics": [],
    "context_chars": 1800,
}

CHUNK = 900
SKIP_DIRS = {"node_modules", ".git", "__pycache__", "venv", ".venv", "runs", "logs", "data"}


# ---------- storage ----------

def settings():
    data = json.loads(json.dumps(DEFAULT_SETTINGS))
    if os.path.exists(SETTINGS_PATH):
        with open(SETTINGS_PATH) as f:
            data.update(json.load(f))
    return data


def save_settings(data):
    os.makedirs(BRAIN_DIR, exist_ok=True)
    with open(SETTINGS_PATH, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def db():
    os.makedirs(BRAIN_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS memories(
            id INTEGER PRIMARY KEY, kind TEXT, text TEXT, source TEXT,
            created TEXT, hits INTEGER DEFAULT 0);
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(text, mem_id UNINDEXED);
        CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, mtime REAL, chunks INTEGER);
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(text, path UNINDEXED);
        CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT);
    """)
    return conn


def fts_query(text):
    # Turn free text into an FTS5 OR-query of quoted words (safe for any input).
    words = [w for w in re.findall(r"\w+", text.lower()) if len(w) > 2][:12]
    return " OR ".join(f'"{w}"' for w in words)


# ---------- memories ----------

def remember(text, kind="fact", source="user"):
    text = " ".join(text.split())
    if not text:
        return "Nothing to remember."
    conn = db()
    q = fts_query(text)
    if q:
        for (old,) in conn.execute("SELECT text FROM memories_fts WHERE memories_fts MATCH ? LIMIT 5", (q,)):
            if _similar(old, text):
                return f"Already known: {old}"
    cur = conn.execute("INSERT INTO memories(kind, text, source, created) VALUES (?,?,?,?)",
                       (kind, text, source, datetime.date.today().isoformat()))
    conn.execute("INSERT INTO memories_fts(text, mem_id) VALUES (?,?)", (text, cur.lastrowid))
    conn.commit()
    return f"Remembered ({kind}): {text}"


def _similar(a, b):
    wa, wb = set(re.findall(r"\w+", a.lower())), set(re.findall(r"\w+", b.lower()))
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) > 0.7


def recall(query, limit=5):
    conn = db()
    q = fts_query(query)
    rows = []
    if q:
        rows = conn.execute(
            "SELECT m.id, m.kind, m.text FROM memories_fts f JOIN memories m ON m.id = f.mem_id "
            "WHERE memories_fts MATCH ? AND m.kind != 'news' ORDER BY rank LIMIT ?", (q, limit)).fetchall()
    # Preferences always apply, even when no word matches.
    prefs = conn.execute("SELECT id, kind, text FROM memories WHERE kind = 'preference' "
                         "ORDER BY hits DESC, id DESC LIMIT 4").fetchall()
    seen, out = set(), []
    for row in prefs + rows:
        if row[0] not in seen:
            seen.add(row[0])
            out.append(row)
    if out:
        conn.executemany("UPDATE memories SET hits = hits + 1 WHERE id = ?", [(r[0],) for r in out])
        conn.commit()
    return [(kind, text) for _, kind, text in out]


def search_files(query, limit=4):
    q = fts_query(query)
    if not q:
        return []
    rows = db().execute("SELECT path, snippet(chunks, 0, '', '', ' … ', 40) FROM chunks "
                        "WHERE chunks MATCH ? ORDER BY rank LIMIT ?", (q, limit)).fetchall()
    return [(p.replace(STORAGE + "/", ""), s) for p, s in rows]


def news(limit=3):
    return [t for (t,) in db().execute(
        "SELECT text FROM memories WHERE kind = 'news' ORDER BY id DESC LIMIT ?", (limit,))]


def context_for(user_input):
    """Short block of relevant memory for the system prompt (kept small for 3B models)."""
    budget = settings()["context_chars"]
    parts = []
    mems = recall(user_input)
    if mems:
        parts.append("Things you learned before (follow them):\n" +
                     "\n".join(f"- [{k}] {t}" for k, t in mems))
    files = search_files(user_input, 3)
    if files:
        parts.append("Possibly relevant passages from the user's files:\n" +
                     "\n".join(f"- {p}: {s}" for p, s in files))
    text = "\n\n".join(parts)
    return text[:budget]


# ---------- session log ----------

def log_turn(user_input, turn, model, route):
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    tools, failures, answer = [], [], ""
    for msg in turn:
        if msg.get("role") == "assistant":
            answer = msg.get("content") or answer
            for call in msg.get("tool_calls") or []:
                fn = call.get("function", {})
                tools.append(f"{fn.get('name')}({json.dumps(fn.get('arguments'), ensure_ascii=False)[:160]})")
        elif msg.get("role") == "tool" and str(msg.get("content", "")).startswith(
                ("FAILED", "exit code 1", "Bad tool call", "This exact command already failed")) \
                or (msg.get("role") == "tool" and "(FAILED" in str(msg.get("content", ""))):
            failures.append(str(msg["content"])[:300])
    entry = {"time": datetime.datetime.now().isoformat(timespec="seconds"), "route": route,
             "model": model, "user": user_input[:1000], "answer": answer[:1500],
             "tools": tools[:12], "failures": failures[:6]}
    path = os.path.join(SESSIONS_DIR, datetime.date.today().isoformat() + ".jsonl")
    with open(path, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def unreviewed_sessions(conn):
    done = set(json.loads((conn.execute("SELECT value FROM state WHERE key='reviewed'").fetchone()
                           or ["[]"])[0]))
    today = datetime.date.today().isoformat() + ".jsonl"
    files = sorted(f for f in os.listdir(SESSIONS_DIR) if f.endswith(".jsonl")) \
        if os.path.isdir(SESSIONS_DIR) else []
    # Today's log is still growing; review it tomorrow unless it's the only one.
    return [f for f in files if f not in done and f != today], done


# ---------- file index ----------

def _extract(path, cfg):
    if path.lower().endswith(".pdf"):
        try:
            out = subprocess.run(["pdftotext", "-l", str(cfg["pdf_max_pages"]), "-enc", "UTF-8",
                                  path, "-"], capture_output=True, text=True, timeout=180)
            return out.stdout
        except (OSError, subprocess.TimeoutExpired):
            return ""
    try:
        with open(path, errors="replace") as f:
            return f.read(2_000_000)
    except OSError:
        return ""


def index_files(verbose=True):
    cfg = settings()
    conn = db()
    known = dict(conn.execute("SELECT path, mtime FROM files"))
    seen, added, chunks_total = set(), 0, 0
    for folder in cfg["index_folders"]:
        for root, dirs, names in os.walk(folder):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            for name in names:
                path = os.path.join(root, name)
                if name.startswith(".") or os.path.splitext(name)[1].lower() not in cfg["index_extensions"]:
                    continue
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                if st.st_size > cfg["max_file_mb"] * 2**20:
                    continue
                seen.add(path)
                if known.get(path) == st.st_mtime:
                    continue
                text = re.sub(r"\s+", " ", _extract(path, cfg)).strip()
                conn.execute("DELETE FROM chunks WHERE path = ?", (path,))
                pieces = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK - 100)] if text else []
                conn.executemany("INSERT INTO chunks(text, path) VALUES (?,?)",
                                 [(f"{name}: {p}", path) for p in pieces])
                conn.execute("INSERT OR REPLACE INTO files VALUES (?,?,?)", (path, st.st_mtime, len(pieces)))
                added += 1
                chunks_total += len(pieces)
                if verbose:
                    print(f"  indexed {path.replace(STORAGE + '/', '')} ({len(pieces)} chunks)")
                conn.commit()
    removed = [p for p in known if p not in seen]
    for p in removed:
        conn.execute("DELETE FROM chunks WHERE path = ?", (p,))
        conn.execute("DELETE FROM files WHERE path = ?", (p,))
    conn.commit()
    return f"{added} files (re)indexed, {chunks_total} chunks, {len(removed)} removed"


# ---------- models ----------

def ask_local(prompt, schema=None, model=None, temperature=0.2):
    cfg = settings()
    body = {"model": model or cfg["learn_model"], "prompt": prompt, "stream": False,
            "options": {"temperature": temperature, "num_ctx": 8192}}
    if schema:
        body["format"] = schema
    r = requests.post(f"{cfg['ollama_url']}/api/generate", json=body, timeout=900)
    r.raise_for_status()
    return r.json().get("response", "")


def _env_key(name, prefix):
    """An API key from the environment, else from ~/.agent_brain/env."""
    key = os.environ.get(name, "")
    if not key:
        try:
            with open(os.path.join(BRAIN_DIR, "env")) as f:
                for line in f:
                    m = re.match(rf"\s*(?:export\s+)?{name}=['\"]?([^'\"\s]+)", line)
                    if m:
                        key = m.group(1)
        except OSError:
            pass
    return key if key.startswith(prefix) and " " not in key else ""


def api_key():
    return _env_key("ANTHROPIC_API_KEY", "sk-")


def gemini_key():
    return _env_key("GEMINI_API_KEY", ("AI", "AQ."))


def online():
    try:
        requests.head("https://www.google.com", timeout=4)
        return True
    except requests.RequestException:
        return False


EXPERT_SYSTEM = ("You are an expert helping a small local AI agent on an Android phone (Termux). "
                 "Answer precisely and briefly; give working commands/code.")


def _ask_claude(key, prompt):
    r = requests.post("https://api.anthropic.com/v1/messages", timeout=120, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": settings()["teacher_model"], "max_tokens": 1200, "system": EXPERT_SYSTEM,
              "messages": [{"role": "user", "content": prompt}]})
    r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json().get("content", []))


def _ask_gemini(key, prompt):
    cfg = settings()
    for model in [cfg["gemini_model"]] + cfg.get("gemini_fallbacks", []):
        r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                          timeout=120, headers={"x-goog-api-key": key, "content-type": "application/json"},
                          json={"systemInstruction": {"parts": [{"text": EXPERT_SYSTEM}]},
                                "contents": [{"role": "user", "parts": [{"text": prompt}]}]})
        if r.status_code in (429, 503):  # busy or rate-limited: try the next model
            continue
        r.raise_for_status()
        parts = (r.json().get("candidates") or [{}])[0].get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts)
    r.raise_for_status()
    return ""


def experts():
    """Available online experts, in order of preference."""
    found = []
    if api_key():
        found.append(("claude", settings()["teacher_model"], api_key(), _ask_claude))
    if gemini_key():
        found.append(("gemini", settings()["gemini_model"], gemini_key(), _ask_gemini))
    return found


def ask_expert(question, context=""):
    """Ask Claude, else Gemini (online), and remember the answer for offline use."""
    available = experts()
    if not available:
        return ("FAILED: no online expert is set up (add ANTHROPIC_API_KEY or GEMINI_API_KEY to "
                "~/.agent_brain/env). Answer with what you know.")
    if not online():
        return "FAILED: offline right now. Answer with what you know, or try again when online."
    prompt = f"{context}\n\nQuestion: {question}".strip()
    errors = []
    for name, model, key, call in available:
        try:
            answer = call(key, prompt)
        except (requests.RequestException, ValueError, IndexError) as e:
            status = getattr(getattr(e, "response", None), "status_code", "")
            errors.append(f"{name} failed {status}".strip())
            continue
        if answer.strip():
            remember(f"Q: {question[:200]} -> A: {answer[:600]}", kind="expert", source=model)
            return answer
    return f"FAILED: {'; '.join(errors)}. Answer with what you know."


# ---------- daily learning ----------

LESSON_SCHEMA = {"type": "object", "properties": {
    "lessons": {"type": "array", "items": {"type": "string"}},
    "facts": {"type": "array", "items": {"type": "string"}},
    "preferences": {"type": "array", "items": {"type": "string"}}},
    "required": ["lessons", "facts", "preferences"]}


def review_sessions(report):
    conn = db()
    files, done = unreviewed_sessions(conn)
    failures_all = []
    for name in files:
        with open(os.path.join(SESSIONS_DIR, name)) as f:
            entries = [json.loads(l) for l in f if l.strip()]
        failures_all += [e for e in entries if e.get("failures")]
        for i in range(0, len(entries), 8):
            batch = entries[i:i + 8]
            transcript = "\n\n".join(
                f"USER: {e['user']}\nTOOLS: {'; '.join(e['tools']) or 'none'}\n"
                f"FAILURES: {' | '.join(e['failures']) or 'none'}\nANSWER: {e['answer'][:500]}"
                for e in batch)
            prompt = ("You are reviewing your own past work as a phone assistant (Android, Termux) "
                      "to get better. From these sessions write:\n"
                      "- lessons: short rules that would avoid the failures or speed up similar tasks "
                      "next time (e.g. 'Use pkg, not apt, to install X').\n"
                      "- facts: stable facts about the user, their projects, files or devices.\n"
                      "- preferences: how the user likes things done.\n"
                      "Only include things clearly supported by the sessions. Each item one sentence. "
                      "Empty lists are fine.\n\n" + transcript)
            try:
                data = json.loads(ask_local(prompt, LESSON_SCHEMA))
            except (requests.RequestException, ValueError) as e:
                report.append(f"- review of {name} failed: {e}")
                continue
            for kind, key in (("lesson", "lessons"), ("fact", "facts"), ("preference", "preferences")):
                for item in data.get(key, [])[:6]:
                    result = remember(item, kind, source=f"review {name}")
                    if result.startswith("Remembered"):
                        report.append(f"- {result}")
        done.add(name)
    conn.execute("INSERT OR REPLACE INTO state VALUES ('reviewed', ?)", (json.dumps(sorted(done)),))
    conn.commit()
    return failures_all


def teacher_review(failures, report):
    if not failures or not settings()["use_teacher"] or not experts():
        return
    text = "\n\n".join(f"TASK: {e['user']}\nTOOLS: {'; '.join(e['tools'])}\nERRORS: {' | '.join(e['failures'])}"
                       for e in failures[:10])
    answer = ask_expert("These are tasks a small local agent failed at on Android/Termux. For each, "
                        "write one short corrective rule it should follow next time. Reply with one "
                        "rule per line starting with '- '.", text)
    if answer.startswith("FAILED"):
        report.append(f"- teacher skipped: {answer}")
        return
    for line in answer.splitlines():
        if line.strip().startswith("- "):
            result = remember(line.strip()[2:], "lesson", source="teacher")
            if result.startswith("Remembered"):
                report.append(f"- teacher {result}")


SOCIAL_APPS = {"com.whatsapp": "WhatsApp", "com.whatsapp.w4b": "WhatsApp Business",
               "com.facebook.orca": "Messenger", "com.facebook.katana": "Facebook",
               "org.telegram.messenger": "Telegram", "com.instagram.android": "Instagram"}
SOCIAL_DIR = os.path.join(BRAIN_DIR, "social")


def capture_notifications():
    """Save new chat/social notifications (run every couple of hours). Stays on the phone."""
    try:
        out = subprocess.run(["termux-notification-list"], capture_output=True, text=True, timeout=30)
        items = json.loads(out.stdout or "[]")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0
    conn = db()
    seen = set(json.loads((conn.execute("SELECT value FROM state WHERE key='seen_notes'").fetchone()
                           or ["[]"])[0]))
    os.makedirs(SOCIAL_DIR, exist_ok=True)
    path = os.path.join(SOCIAL_DIR, datetime.date.today().isoformat() + ".jsonl")
    new = 0
    with open(path, "a") as f:
        for n in items:
            app = SOCIAL_APPS.get(n.get("packageName", ""))
            text = (n.get("content") or "").strip()
            if not app or not text:
                continue
            key = f"{n.get('packageName')}|{n.get('title')}|{text}"[:300]
            if key in seen:
                continue
            seen.add(key)
            f.write(json.dumps({"time": n.get("when"), "app": app, "from": n.get("title", ""),
                                "text": text[:800]}, ensure_ascii=False) + "\n")
            new += 1
    conn.execute("INSERT OR REPLACE INTO state VALUES ('seen_notes', ?)", (json.dumps(sorted(seen)[-3000:]),))
    conn.commit()
    return new


SOCIAL_SCHEMA = {"type": "object", "properties": {
    "facts": {"type": "array", "items": {"type": "string"}},
    "todos": {"type": "array", "items": {"type": "string"}}},
    "required": ["facts", "todos"]}


def _learn_from_messages(lines, label, report):
    for i in range(0, len(lines), 60):
        chunk = "\n".join(lines[i:i + 60])
        prompt = ("These are chat messages from the user's phone (" + label + "). Extract:\n"
                  "- facts: lasting, useful facts (who people are to the user, ongoing plans, "
                  "projects, places, dates). Write people by the name shown.\n"
                  "- todos: things the user promised or needs to do, with dates if given.\n"
                  "Skip small talk, greetings and anything uncertain. One sentence each; empty lists are fine.\n\n"
                  + chunk)
        try:
            data = json.loads(ask_local(prompt, SOCIAL_SCHEMA))
        except (requests.RequestException, ValueError) as e:
            report.append(f"- {label}: review failed ({e})")
            return
        for kind, key in (("social", "facts"), ("todo", "todos")):
            for item in data.get(key, [])[:8]:
                result = remember(item, kind, source=label)
                if result.startswith("Remembered"):
                    report.append(f"- {result}")


def social_digest(report):
    capture_notifications()
    conn = db()
    done = set(json.loads((conn.execute("SELECT value FROM state WHERE key='social_done'").fetchone()
                           or ["[]"])[0]))
    today = datetime.date.today().isoformat() + ".jsonl"
    names = sorted(f for f in os.listdir(SOCIAL_DIR) if f.endswith(".jsonl")) if os.path.isdir(SOCIAL_DIR) else []
    for name in names:
        if name in done or name == today:
            continue
        with open(os.path.join(SOCIAL_DIR, name)) as f:
            msgs = [json.loads(l) for l in f if l.strip()]
        lines = [f"[{m['app']}] {m['from']}: {m['text']}" for m in msgs]
        if lines:
            _learn_from_messages(lines, f"notifications {name[:10]}", report)
        done.add(name)
    # Exported chats (WhatsApp: chat > More > Export chat) saved in Download/Chat Exports.
    exports = os.path.join(STORAGE, "Download", "Chat Exports")
    os.makedirs(exports, exist_ok=True)
    for name in sorted(os.listdir(exports)):
        path = os.path.join(exports, name)
        key = f"export:{name}:{int(os.path.getmtime(path))}"
        if key in done or not name.lower().endswith((".txt", ".json")):
            continue
        with open(path, errors="replace") as f:
            lines = [l.strip() for l in f if l.strip()][-1200:]  # the most recent part
        _learn_from_messages(lines, f"chat export {name}", report)
        done.add(key)
    conn.execute("INSERT OR REPLACE INTO state VALUES ('social_done', ?)", (json.dumps(sorted(done)),))
    conn.commit()


def follow_news(report):
    topics = settings()["follow_topics"]
    if not topics or not online():
        return
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import agent  # reuse the agent's DuckDuckGo search and page reader
    config = agent.load_config()
    for topic in topics:
        results = agent.web_search(config, f"{topic} latest news")
        urls = re.findall(r"^\s+(https?://\S+)", results, re.M)[:2]
        pages = "\n\n".join(agent.fetch_url(config, u)[:3000] for u in urls)
        if not pages.strip():
            continue
        try:
            summary = ask_local(f"Summarize the most important new information about '{topic}' "
                                f"in 3 short bullet points with dates if given:\n\n{pages}")
        except requests.RequestException:
            continue
        remember(f"{datetime.date.today()} {topic}: {' '.join(summary.split())[:700]}", "news",
                 source=", ".join(urls))
        report.append(f"- news on {topic} saved")
    # Keep news fresh: drop items older than 14 days.
    cutoff = (datetime.date.today() - datetime.timedelta(days=14)).isoformat()
    conn = db()
    old = [i for (i,) in conn.execute("SELECT id FROM memories WHERE kind='news' AND created < ?", (cutoff,))]
    for i in old:
        conn.execute("DELETE FROM memories WHERE id = ?", (i,))
        conn.execute("DELETE FROM memories_fts WHERE mem_id = ?", (i,))
    conn.commit()


def learn():
    start = datetime.datetime.now()
    report = [f"# Learning run {start:%Y-%m-%d %H:%M}", ""]
    failures = review_sessions(report)
    report.append(f"- files: {index_files(verbose=False)}")
    social_digest(report)
    try:  # refresh Alaa's writing voice for the WhatsApp stand-in (stand_in.py)
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import stand_in
        report.append(f"- voice: {stand_in.learn_voice()}")
    except Exception as e:  # the stand-in is optional
        report.append(f"- voice refresh skipped: {e}")
    follow_news(report)
    teacher_review(failures, report)
    total = db().execute("SELECT kind, COUNT(*) FROM memories GROUP BY kind").fetchall()
    report.append(f"- memory now: {', '.join(f'{k} {n}' for k, n in total) or 'empty'}")
    report.append(f"- took {(datetime.datetime.now() - start).seconds}s")
    os.makedirs(REPORTS_DIR, exist_ok=True)
    path = os.path.join(REPORTS_DIR, f"{start:%Y-%m-%d}.md")
    with open(path, "w") as f:
        f.write("\n".join(report) + "\n")
    subprocess.run(["termux-notification", "--title", "Agent learned today",
                    "--content", f"{len([r for r in report if 'Remembered' in r])} new memories. "
                                 f"Report: {path}"], capture_output=True, timeout=20)
    return "\n".join(report)


def status():
    conn = db()
    mems = conn.execute("SELECT kind, COUNT(*) FROM memories GROUP BY kind").fetchall()
    files, chunks = conn.execute("SELECT COUNT(*), COALESCE(SUM(chunks),0) FROM files").fetchone()
    return (f"Memories: {', '.join(f'{k} {n}' for k, n in mems) or 'none'}\n"
            f"Indexed: {files} files, {chunks} chunks\n"
            f"Follow topics: {', '.join(settings()['follow_topics']) or 'none'}\n"
            f"Online experts: {', '.join(f'{n} ({m})' for n, m, _, _ in experts()) or 'none (add keys to ~/.agent_brain/env)'}")


def main(argv):
    cmd, rest = (argv[0] if argv else "status"), " ".join(argv[1:])
    if cmd == "learn":
        print(learn())
    elif cmd == "capture":
        print(f"{capture_notifications()} new chat notifications saved")
    elif cmd == "index":
        print(index_files())
    elif cmd == "search":
        for p, s in search_files(rest, 8):
            print(f"{p}\n   {s}\n")
        for k, t in recall(rest):
            print(f"[{k}] {t}")
    elif cmd == "lessons":
        for kind, text, source in db().execute(
                "SELECT kind, text, source FROM memories ORDER BY id DESC LIMIT 40"):
            print(f"[{kind}] {text}  ({source})")
    elif cmd == "remember":
        print(remember(rest, "fact"))
    elif cmd == "key" and rest in ("gemini", "claude"):
        # Save the API key currently in the clipboard to ~/.agent_brain/env without printing it.
        name, prefix = ("GEMINI_API_KEY", ("AI", "AQ.")) if rest == "gemini" else ("ANTHROPIC_API_KEY", ("sk-",))
        key = subprocess.run(["termux-clipboard-get"], capture_output=True, text=True, timeout=20).stdout.strip()
        if not key.startswith(prefix) or " " in key:
            print(f"The clipboard doesn't hold a {rest} key (it should start with {' or '.join(prefix)}). Copy it and try again.")
            return
        path = os.path.join(BRAIN_DIR, "env")
        lines = open(path).read().splitlines() if os.path.exists(path) else []
        lines = [l for l in lines if not re.match(rf"\s*(export\s+)?{name}=", l)] + [f"export {name}={key}"]
        os.umask(0o077)
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.chmod(path, 0o600)
        subprocess.run(["termux-clipboard-set", ""], timeout=20)
        print(f"Saved your {rest} key (…{key[-4:]}) and cleared the clipboard.")
    elif cmd == "follow":
        s = settings()
        if rest and rest not in s["follow_topics"]:
            s["follow_topics"].append(rest)
            save_settings(s)
        print("Following:", ", ".join(s["follow_topics"]))
    else:
        print(status())


if __name__ == "__main__":
    main(sys.argv[1:])
