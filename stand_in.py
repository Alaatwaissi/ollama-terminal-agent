"""Alaa's stand-in: drafts replies to routine WhatsApp messages in his voice.

Nothing is sent without approval. For each new WhatsApp message (read from the
notification bar through Termux:API) the local model decides whether it is routine.
Routine ones get a draft in Alaa's voice, shown in Buddy's bubble as
Approve / Deny / Change. Only an approved reply is typed and sent, after checking
that the open chat is the right contact. Approved and edited replies are kept as
voice examples, so the drafts sound more like him over time.

    python stand_in.py on        start watching (stop with Ctrl-C or `off`)
    python stand_in.py off       stop
    python stand_in.py voice     (re)learn the voice from Download/Chat Exports
    python stand_in.py test "<message>" [sender]   draft a reply without sending
    python stand_in.py status
"""
import datetime
import json
import os
import re
import subprocess
import sys
import time

import requests

sys.path.insert(0, os.path.expanduser("~"))
import brain  # noqa: E402
from phone_bridge import Phone  # noqa: E402

DIR = os.path.expanduser("~/.agent_brain")
VOICE_PATH = os.path.join(DIR, "voice.json")
STATE_PATH = os.path.join(DIR, "stand_in_state.json")
FLAG = os.path.join(DIR, "stand_in.on")
LOG = os.path.join(DIR, "stand_in.log")
EXPORTS = "/storage/emulated/0/Download/Chat Exports"
WHATSAPP = ("com.whatsapp", "com.whatsapp.w4b")
MY_NAMES = {"alaa", "alaa twaissi", "alaa jamil", "علاء", "you"}
POLL_SECONDS = 20
MAX_EXAMPLES = 60

ROUTINE_KINDS = {"greeting", "thanks", "congratulations", "scheduling", "acknowledgement"}
KINDS = sorted(ROUTINE_KINDS | {"medical", "money", "legal_official", "trading",
                                "emotional_or_conflict", "needs_information", "other"})
CLASSIFY_SCHEMA = {"type": "object", "properties": {"kind": {"enum": KINDS}}, "required": ["kind"]}
CLASSIFY_EXAMPLES = """صباح الخير دكتور كيفك -> greeting
Thanks a lot for yesterday -> thanks
مبروك على المشروع -> congratulations
Are we still meeting at 6? -> scheduling
وين صرت؟ -> scheduling
Ok noted -> acknowledgement
My daughter fell and her wrist hurts, what should I give her? -> medical
Can you send the 300 JD today? -> money
The bank needs the signed papers -> legal_official
Should I buy gold now? -> trading
Why didn't you answer me?! -> emotional_or_conflict
What's the price of the tickets for the village? -> needs_information"""
REPLY_SCHEMA = {"type": "object", "properties": {"reply": {"type": "string"}}, "required": ["reply"]}

# Starter examples of a short, friendly Jordanian texting style. They are replaced by
# Alaa's real replies as soon as exported chats or approved replies exist.
STARTER_EXAMPLES = [
    {"them": "صباح الخير دكتور كيفك؟", "me": "صباح النور، الحمدلله تمام. انت كيفك؟"},
    {"them": "وين صرت؟", "me": "بالطريق، ربع ساعة وبكون عندك"},
    {"them": "شكرا كتير على امبارح", "me": "العفو حبيبي، ولا يهمك 🙏"},
    {"them": "مبروك!", "me": "الله يبارك فيك، تسلم 🌹"},
    {"them": "بنلتقي الساعة 6؟", "me": "تمام، الساعة 6 بشوفك"},
    {"them": "Are we still on for tomorrow?", "me": "Yes, still on. See you tomorrow 👍"},
    {"them": "Thanks doctor!", "me": "You're welcome, anytime"},
    {"them": "Ok noted", "me": "👍"},
]
BANNED = re.compile(r"(assist|how can i help|as an ai|#|كيف يمكنني مساعدتك|بصفتي)", re.I)
ARABIC = re.compile(r"[؀-ۿ]")
LATIN = re.compile(r"[A-Za-z]")


def acceptable(message, reply):
    if not reply or len(reply) > 220 or BANNED.search(reply):
        return False
    if ARABIC.search(message) and not LATIN.search(message):
        # Arabic message: reply in Arabic, with no Latin letters stuck inside words.
        return bool(ARABIC.search(reply)) and not re.search(r"[؀-ۿ][A-Za-z]|[A-Za-z][؀-ۿ]", reply)
    if LATIN.search(message) and not ARABIC.search(message):
        return bool(LATIN.search(reply))
    return True


def log(text):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {text}"
    print(line)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save(path, data):
    os.makedirs(DIR, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


# ---------- voice ----------

EXPORT_LINE = re.compile(r"^\[?\d{1,4}[/.-]\d{1,2}[/.-]\d{2,4},? [\d:]+(?:\s?[APap][Mm])?\]? ?-? ?([^:]+): (.+)$")


def learn_voice():
    """Collect Alaa's own messages from exported WhatsApp chats as style examples."""
    voice = load(VOICE_PATH, {"examples": [], "style": ""})
    found = []
    if os.path.isdir(EXPORTS):
        for name in sorted(os.listdir(EXPORTS)):
            if not name.lower().endswith(".txt"):
                continue
            with open(os.path.join(EXPORTS, name), errors="replace") as f:
                lines = [l.strip() for l in f]
            # Pair each of his messages with the message before it (what he answered).
            prev = ""
            for line in lines:
                m = EXPORT_LINE.match(line)
                if not m:
                    continue
                sender, text = m.group(1).strip(), m.group(2).strip()
                if "<Media omitted>" in text or "omitted>" in text or len(text) > 300:
                    prev = ""
                    continue
                if sender.lower() in MY_NAMES or sender.lower().startswith("alaa"):
                    if prev:
                        found.append({"them": prev[:200], "me": text})
                else:
                    prev = text
    examples = (found[-MAX_EXAMPLES:] + voice["examples"])[:MAX_EXAMPLES]
    voice["examples"] = examples
    if examples:
        sample = "\n".join(f"them: {e['them']}\nAlaa: {e['me']}" for e in examples[:40])
        try:
            voice["style"] = brain.ask_local(
                "Describe in 4 short bullet points how Alaa writes WhatsApp replies: length, "
                "language mix (Arabic/English, dialect), tone, typical words and emojis. "
                "Base it only on these examples:\n\n" + sample).strip()
        except requests.RequestException as e:
            log(f"style summary failed: {e}")
    save(VOICE_PATH, voice)
    return f"{len(found)} replies found in exports; {len(examples)} voice examples kept."


def classify(message):
    data = json.loads(brain.ask_local(
        "Label the WhatsApp message with one kind. Examples:\n" + CLASSIFY_EXAMPLES +
        f"\n\nMessage: {message}\nKind:", CLASSIFY_SCHEMA))
    return data.get("kind", "other")


def draft(sender, message):
    """Returns (routine, reason, reply). Only routine kinds get a reply."""
    kind = classify(message)
    if kind not in ROUTINE_KINDS:
        return False, kind.replace("_", " "), ""
    voice = load(VOICE_PATH, {"examples": [], "style": ""})
    arabic = bool(ARABIC.search(message))
    # Show examples in the message's language, so the model answers in that language.
    pool = [e for e in voice["examples"] + STARTER_EXAMPLES
            if bool(ARABIC.search(e["them"])) == arabic][:25] or STARTER_EXAMPLES
    examples = "\n".join(f"them: {e['them']}\nAlaa: {e['me']}" for e in pool)
    language = ("Jordanian Arabic (Arabic script only)" if arabic and not LATIN.search(message)
                else "English" if not arabic else "the same Arabic/English mix")
    prompt = (
        "Write Alaa's WhatsApp reply. Alaa is a doctor in Wadi Musa, Jordan, texting a friend or "
        f"acquaintance. Write in {language}. One short sentence, like the examples: casual, warm, "
        "no questions about how you can help, no hashtags, no signature. Don't promise anything "
        "specific you weren't told; for a time or plan, confirm it or say you'll confirm shortly.\n\n"
        f"How Alaa writes:\n{voice['style'] or '- short, warm, casual; Jordanian dialect in Arabic'}\n\n"
        f"Examples of his replies:\n{examples}\n\n"
        f"them: {message}\nAlaa:")
    for attempt in range(3):
        reply = " ".join((json.loads(brain.ask_local(prompt, REPLY_SCHEMA, temperature=0.4 + 0.25 * attempt))
                          .get("reply") or "").split())
        if acceptable(message, reply):
            return True, kind, reply
        log(f"rejected draft: {reply!r}")
    return False, f"{kind} (couldn't write a good draft)", ""


def keep_example(message, reply):
    voice = load(VOICE_PATH, {"examples": [], "style": ""})
    voice["examples"] = (voice["examples"] + [{"them": message[:200], "me": reply}])[-MAX_EXAMPLES:]
    save(VOICE_PATH, voice)


# ---------- WhatsApp ----------

def new_messages(state):
    try:
        out = subprocess.run(["termux-notification-list"], capture_output=True, text=True, timeout=30)
        items = json.loads(out.stdout or "[]")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []
    found = []
    for n in items:
        if n.get("packageName") not in WHATSAPP:
            continue
        title, text = (n.get("title") or "").strip(), (n.get("content") or "").strip()
        key = f"{title}|{text}|{n.get('when')}"
        if not title or not text or key in state["seen"]:
            continue
        state["seen"].append(key)
        # Skip group chats ("Group: Sender", "Sender @ Group") and summary lines.
        if ":" in title or " @ " in title or re.match(r"^\d+ (new )?messages", text) \
                or title.lower() == "whatsapp":
            continue
        found.append({"sender": title, "text": text})
    state["seen"] = state["seen"][-500:]
    return found


def screen_has(screen, name):
    return any(name.lower() in line.lower() for line in screen.splitlines()[:40])


def element_id(screen, pattern):
    for line in screen.splitlines():
        if re.search(pattern, line, re.I):
            m = re.match(r"\s*\[(\d+)\]", line)
            if m:
                return int(m.group(1))
    return None


def send_reply(phone, sender, reply):
    """Open the chat with `sender`, make sure it's the right one, type and send."""
    phone.call("open_app", app="WhatsApp")
    phone.call("wait", ms=1500)
    screen = phone.call("read_screen")
    chat = element_id(screen, rf'"{re.escape(sender)}"')
    if chat is None:
        search = element_id(screen, r'desc="Search|"Ask Meta AI or Search"')
        if search is None:
            return "couldn't find the chat or the search box"
        phone.call("tap", element=search)
        phone.call("type_text", text=sender)
        phone.call("wait", ms=1200)
        screen = phone.call("read_screen")
        chat = element_id(screen, rf'"{re.escape(sender)}"')
        if chat is None:
            return f"no chat named {sender}"
    phone.call("tap", element=chat)
    phone.call("wait", ms=1200)
    screen = phone.call("read_screen")
    header = "\n".join(screen.splitlines()[:30])
    if sender.lower() not in header.lower():
        return "the opened chat doesn't match the contact; not sending"
    box = element_id(screen, r'EditText')
    if box is None:
        return "message box not found"
    phone.call("type_text", element=box, text=reply)
    phone.call("wait", ms=500)
    screen = phone.call("read_screen")
    send = element_id(screen, r'desc="Send"')
    if send is None:
        return "send button not found (the text is typed; send it yourself)"
    phone.call("tap", element=send)
    phone.call("wait", ms=800)
    phone.call("press", button="home")
    return "sent"


def handle(phone, msg):
    sender, text = msg["sender"], msg["text"]
    try:
        routine, reason, reply = draft(sender, text)
    except (requests.RequestException, ValueError) as e:
        log(f"draft failed for {sender}: {e}")
        return
    if not routine:
        log(f"skipped {sender}: {reason}")
        phone.call("notify", text=f"Not routine, left for you: {sender} ({reason[:60]})")
        return
    for _ in range(3):
        answer = phone.call("confirm", summary=f"Reply to {sender} on WhatsApp.\n"
                                               f"They wrote: «{text[:200]}»\nSend: «{reply}»")
        if answer.startswith("APPROVED"):
            result = send_reply(phone, sender, reply)
            log(f"{sender}: {result}: {reply}")
            phone.call("notify", text=f"Reply to {sender}: {result}")
            if result == "sent":
                keep_example(text, reply)
            return
        if answer.startswith("CHANGES"):
            change = answer.split(":", 1)[-1].strip()
            try:
                reply = " ".join(brain.ask_local(
                    f"Rewrite this WhatsApp reply by Alaa as asked. Keep his voice, same language. "
                    f"Return only the new reply.\nThey wrote: {text}\nDraft: {reply}\n"
                    f"Change: {change}").split()).strip('"«»')
            except requests.RequestException as e:
                log(f"rewrite failed: {e}")
                return
            continue
        log(f"{sender}: denied")
        return
    log(f"{sender}: gave up after 3 changes")


def ensure_ollama():
    try:
        requests.get("http://127.0.0.1:11434/api/version", timeout=3)
    except requests.RequestException:
        subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        time.sleep(5)


def watch():
    phone = Phone()
    if not phone.available():
        sys.exit("Buddy isn't reachable. Open the Buddy app and try again.")
    ensure_ollama()
    open(FLAG, "w").close()
    state = load(STATE_PATH, {"seen": []})
    new_messages(state)  # mark what's already there as seen; only answer new messages
    save(STATE_PATH, state)
    log("stand-in ON: watching WhatsApp (python stand_in.py off to stop)")
    phone.call("notify", text="Stand-in is on: I'll draft replies to routine WhatsApp messages")
    try:
        while os.path.exists(FLAG):
            for msg in new_messages(state):
                save(STATE_PATH, state)
                handle(phone, msg)
            save(STATE_PATH, state)
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        pass
    if os.path.exists(FLAG):
        os.remove(FLAG)
    log("stand-in OFF")


def main(argv):
    cmd = argv[0] if argv else "status"
    if cmd == "on":
        watch()
    elif cmd == "off":
        if os.path.exists(FLAG):
            os.remove(FLAG)
        print("Stand-in will stop within a few seconds.")
    elif cmd == "voice":
        print(learn_voice())
    elif cmd == "test" and len(argv) > 1:
        routine, reason, reply = draft(argv[2] if len(argv) > 2 else "Ahmad", argv[1])
        print(f"routine: {routine}\nreason: {reason}\nreply: {reply}")
    else:
        voice = load(VOICE_PATH, {"examples": []})
        print(f"Stand-in: {'ON' if os.path.exists(FLAG) else 'off'}\n"
              f"Voice examples: {len(voice['examples'])}\n"
              f"Style: {voice.get('style') or 'not learned yet (python stand_in.py voice)'}")


if __name__ == "__main__":
    main(sys.argv[1:])
