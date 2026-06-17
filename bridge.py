#!/usr/bin/env python3
"""
signal-ollama bridge — talk to local Ollama models over Signal.

Architecture: listens to the signal-cli daemon's SSE event stream, parses
incoming messages (Note-to-Self syncs + DMs from allowed users), runs them
against Ollama's /api/chat, and replies. Owner manages an allowlist via chat.

Stdlib only (urllib + json). Persists per-user sessions to state.json so the
selected model, system prompt, params and conversation history survive
restarts; the Ollama model itself is NOT pinned in VRAM between turns.
"""
import json, os, threading, time, urllib.request, urllib.error

# --- config (all via env; see README) -------------------------------------
SIGNAL_URL = os.environ.get("SIGNAL_URL", "http://127.0.0.1:8080")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
ACCOUNT    = os.environ.get("SIGNAL_ACCOUNT", "").strip()        # the linked number, e.g. +15551234567
OWNER      = os.environ.get("SIGNAL_OWNER", ACCOUNT).strip()      # allowlist admin (defaults to ACCOUNT)
STATE_FILE = os.path.expanduser(os.environ.get("SIGNAL_OLLAMA_STATE", "~/signal-ollama/state.json"))
DEFAULT_SYSTEM = os.environ.get("SIGNAL_OLLAMA_SYSTEM", "You are a helpful, concise assistant.")

# Known-model one-line descriptions (anything else shows with no blurb).
MODEL_DESC = {
    "llama3.3-abliterated-127k": "uncensored 70B, 127k ctx",
    "gpt-oss-64k":               "120B MoE, 64k ctx",
    "gpt-oss:120b":              "120B MoE",
    "mistral-large-32k":         "123B dense, 32k ctx (biggest that fits)",
    "deepseek-r1:70b":           "reasoning, 70B",
    "qwen3-coder:30b":           "coder, 30B",
    "devstral:24b":              "coder, 24B",
    "qwen2.5-coder:3b":          "coder, small/fast",
    "llama2-uncensored:70b":     "uncensored, older",
    "dolphin-llama3:70b":        "uncensored, 8k ctx",
}

# Short aliases (case-insensitive): initials + param count + role letter.
ALIASES = {
    "L70A":  "llama3.3-abliterated-127k",   # Llama 70B Abliterated (uncensored)
    "L70U":  "llama2-uncensored:70b",       # Llama 70B Uncensored
    "GO120": "gpt-oss-64k",                  # GPT-OSS 120B (64k ctx build)
    "GO120B":"gpt-oss:120b",                 # GPT-OSS 120B (raw)
    "ML123": "mistral-large-32k",            # Mistral-Large 123B (dense, 32k ctx)
    "DS70R": "deepseek-r1:70b",              # DeepSeek 70B Reasoning
    "Q30C":  "qwen3-coder:30b",              # Qwen 30B Coder
    "Q3C":   "qwen2.5-coder:3b",             # Qwen 3B Coder
    "DV24":  "devstral:24b",                 # Devstral 24B
    "DLP70": "dolphin-llama3:70b",           # Dolphin Llama 70B
}
_REV_ALIAS = {v: k for k, v in ALIASES.items()}

def resolve_model(name):
    """Map an alias / full name / base name / unique substring to an installed
    model. Fully general — works for any model in Ollama, not just aliased ones."""
    if not name:
        return None
    q = name.strip()
    if q.upper() in ALIASES:                       # curated short alias
        q = ALIASES[q.upper()]
    names = list_models()
    ql = q.lower()
    for n in names:                                # exact, :latest, or base-name match
        nl = n.lower()
        if nl == ql or nl == ql + ":latest" or nl.split(":")[0] == ql:
            return n
    matches = [n for n in names if ql in n.lower()]  # unique substring
    if len(matches) == 1:
        return matches[0]
    return None

# Ollama generation options the user may tune via /set.
SETTABLE = {
    "temperature": float, "top_p": float, "top_k": int, "min_p": float,
    "num_ctx": int, "num_predict": int, "repeat_penalty": float,
    "repeat_last_n": int, "seed": int, "presence_penalty": float,
    "frequency_penalty": float, "mirostat": int, "mirostat_tau": float,
    "mirostat_eta": float, "tfs_z": float,
}

_state_lock = threading.Lock()
_user_locks = {}
_seen = set()  # dedup of processed message timestamps


# ----- state ---------------------------------------------------------------
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"allow": {}, "sessions": {}}

def save_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=2)
    os.replace(tmp, STATE_FILE)

def session(st, user):
    s = st["sessions"].get(user)
    if not s:
        s = {"model": None, "system": None, "options": {}, "raw": False, "history": []}
        st["sessions"][user] = s
    return s

def user_lock(user):
    with _state_lock:
        if user not in _user_locks:
            _user_locks[user] = threading.Lock()
        return _user_locks[user]


# ----- signal RPC ----------------------------------------------------------
def rpc(method, params):
    body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode()
    req = urllib.request.Request(SIGNAL_URL + "/api/v1/rpc", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except Exception as e:
        print("rpc error", method, e, flush=True)
        return None

def send(recipient, text):
    rpc("send", {"account": ACCOUNT, "recipient": [recipient], "message": text})

def react(recipient, target_author, target_ts, emoji, remove=False):
    rpc("sendReaction", {"account": ACCOUNT, "recipient": [recipient],
                         "emoji": emoji, "targetAuthor": target_author,
                         "targetTimestamp": target_ts, "remove": remove})


# ----- identity resolution -------------------------------------------------
_uuid2num = {}

def _refresh_contacts():
    r = rpc("listContacts", {"account": ACCOUNT})
    res = (r or {}).get("result")
    if isinstance(res, list):
        _uuid2num.clear()
        for c in res:
            u, n = c.get("uuid"), c.get("number")
            if u and n:
                _uuid2num[u] = n

def canonical(idstr):
    """Map a sender id to a phone number when signal-cli knows it; otherwise
    return the id unchanged (UUIDs are stable identifiers in their own right)."""
    if not idstr or idstr.startswith("+"):
        return idstr
    if idstr not in _uuid2num:
        _refresh_contacts()
    return _uuid2num.get(idstr, idstr)


# ----- ollama --------------------------------------------------------------
def list_models():
    try:
        with urllib.request.urlopen(OLLAMA_URL + "/api/tags", timeout=15) as r:
            data = json.load(r)
        return sorted(m["name"] for m in data.get("models", []))
    except Exception as e:
        print("ollama tags error", e, flush=True)
        return []

def model_exists(name):
    return resolve_model(name) is not None

def chat_stream(model, messages, options):
    """Yield content deltas from Ollama's /api/chat as they stream in."""
    body = json.dumps({"model": model, "messages": messages,
                       "stream": True, "options": options}).encode()
    req = urllib.request.Request(OLLAMA_URL + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=1800) as r:
        for line in r:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            delta = obj.get("message", {}).get("content", "")
            if delta:
                yield delta
            if obj.get("done"):
                break


# ----- help text -----------------------------------------------------------
def models_block():
    lines = []
    for m in list_models():
        d = MODEL_DESC.get(m) or MODEL_DESC.get(m.replace(":latest", ""))
        alias = _REV_ALIAS.get(m) or _REV_ALIAS.get(m.replace(":latest", ""))
        tag = f"[{alias}] " if alias else ""
        lines.append(f"• {tag}{m}" + (f"   {d}" if d else ""))
    return "\n".join(lines) if lines else "(no models found)"

def help_text(is_owner):
    t = [
        "🤖 signal-ollama — commands",
        "",
        "Plain text = a prompt to the model.",
        "",
        "Session:",
        "• /open <model>   load/switch model (or just text a model name when idle)",
        "• /models         list models",
        "• /close          unload current model",
        "• /reset          forget the conversation (keep model + system)",
        "• /info           show current model, system, params, mode",
        "• /help           this menu",
        "",
        "Prompting:",
        "• /sys <text>     set system prompt",
        "• /sys            show system prompt",
        "• /sys clear      revert to default system prompt",
        "• /raw            toggle raw mode (no history, no system prompt)",
        "",
        "Tuning (any value optional, /unset <param> to clear):",
        "• /set <param> <value>   e.g. /set temperature 0.8",
        "• /temp <v>       shortcut for temperature",
        "• /ctx <n>        shortcut for num_ctx",
        "  params: " + ", ".join(SETTABLE.keys()),
        "",
        f"Default system prompt: \"{DEFAULT_SYSTEM}\"",
    ]
    if is_owner:
        t += ["", "Owner only:",
              "• /users                list allowed users",
              "• /pending              show unknown senders waiting (their id)",
              "• /allow <+number|uuid> [label]   grant access",
              "• /revoke <id>          remove access"]
    t += ["", "Models:", models_block()]
    return "\n".join(t)

def info_text(s):
    opts = ", ".join(f"{k}={v}" for k, v in s["options"].items()) or "(defaults)"
    return ("ℹ️ session\n"
            f"model:  {s['model'] or '(none)'}\n"
            f"system: {s['system'] or DEFAULT_SYSTEM}\n"
            f"mode:   {'RAW (no history/system)' if s['raw'] else 'chat (multi-turn)'}\n"
            f"params: {opts}\n"
            f"history: {len(s['history'])} msgs")


# ----- message handling ----------------------------------------------------
def handle(sender, text, target_author, target_ts):
    key = canonical(sender)                       # phone number if known, else the id (uuid)
    is_owner = (key == OWNER or sender == OWNER)
    with user_lock(key):
        st = load_state()
        # access control — owner + allowlisted (by number OR uuid); else ignored.
        # Unknown senders are recorded for /pending, silently (no notification).
        if not is_owner and sender not in st["allow"] and key not in st["allow"]:
            st.setdefault("pending", {})[sender] = text[:60]
            save_state(st)
            return
        s = session(st, key)
        body = text.strip()
        low = body.lower()

        def reply(msg):
            send(ACCOUNT if is_owner else sender, msg)

        # ----- commands -----
        if body.startswith("/"):
            parts = body.split(maxsplit=1)
            cmd = parts[0].lower()
            arg = parts[1].strip() if len(parts) > 1 else ""

            if cmd == "/help":
                reply(help_text(is_owner)); return
            if cmd == "/models":
                reply("Models:\n" + models_block() + "\n→ text a name or /open <name>."); save_state(st); return
            if cmd in ("/open",):
                target = arg.split()[0] if arg else ""
                if not target:
                    reply("Usage: /open <model>"); return
                full = resolve_model(target)
                if not full:
                    reply(f"Unknown model {target!r}.\n" + models_block()); return
                s["model"] = full; s["history"] = []
                save_state(st); reply(f"🤖 {full} — open. /help for commands."); return
            if cmd == "/close":
                s["model"] = None; s["history"] = []
                save_state(st); reply("Session closed. Text a model name to start again."); return
            if cmd == "/reset":
                s["history"] = []; save_state(st); reply("✓ conversation cleared (model + system kept)."); return
            if cmd == "/info":
                reply(info_text(s)); return
            if cmd == "/raw":
                s["raw"] = not s["raw"]; save_state(st)
                reply(f"✓ raw mode {'ON (no history, no system)' if s['raw'] else 'OFF (multi-turn chat)'}."); return
            if cmd == "/sys":
                if not arg:
                    reply(f"system prompt:\n{s['system'] or DEFAULT_SYSTEM}"); return
                if arg.lower() == "clear":
                    s["system"] = None; save_state(st); reply("✓ system prompt reverted to default."); return
                s["system"] = arg; save_state(st); reply("✓ system prompt set."); return
            if cmd in ("/temp", "/ctx", "/set"):
                if cmd == "/temp":
                    param, val = "temperature", arg
                elif cmd == "/ctx":
                    param, val = "num_ctx", arg
                else:
                    sp = arg.split(maxsplit=1)
                    if len(sp) != 2:
                        reply("Usage: /set <param> <value>\nparams: " + ", ".join(SETTABLE)); return
                    param, val = sp[0], sp[1]
                if param not in SETTABLE:
                    reply(f"Unknown param {param!r}.\nparams: " + ", ".join(SETTABLE)); return
                try:
                    s["options"][param] = SETTABLE[param](val)
                except ValueError:
                    reply(f"{param} expects {SETTABLE[param].__name__}."); return
                save_state(st); reply(f"✓ {param} = {s['options'][param]}"); return
            if cmd == "/unset":
                if arg in s["options"]:
                    del s["options"][arg]; save_state(st); reply(f"✓ {arg} cleared."); return
                reply(f"{arg} not set."); return
            # owner commands
            if cmd in ("/users", "/allow", "/revoke", "/pending"):
                if not is_owner:
                    reply("Owner only."); return
                if cmd == "/users":
                    lines = [f"• {OWNER}   you (owner)"] + [f"• {n}   {l}" for n, l in st["allow"].items()]
                    reply("Allowed:\n" + "\n".join(lines)); return
                if cmd == "/pending":
                    pend = st.get("pending", {})
                    if not pend:
                        reply("No pending senders."); return
                    lines = [f"• {sid}\n    said: {msg!r}" for sid, msg in pend.items()]
                    reply("Pending (use /allow <id> [label]):\n" + "\n".join(lines)); return
                if cmd == "/allow":
                    sp = arg.split(maxsplit=1)
                    who = sp[0] if sp else ""
                    if not who:
                        reply("Usage: /allow <+number|uuid> [label]"); return
                    st["allow"][who] = sp[1] if len(sp) > 1 else ""
                    st.get("pending", {}).pop(who, None)
                    save_state(st); reply(f"✓ {who} {st['allow'][who]} can now text the bot."); return
                if cmd == "/revoke":
                    if arg in st["allow"]:
                        lbl = st["allow"].pop(arg); st["sessions"].pop(arg, None)
                        save_state(st); reply(f"✓ {arg} {lbl} removed."); return
                    reply(f"{arg} not in allowlist."); return
            reply(f"Unknown command {cmd}. /help for the menu."); return

        # ----- not a command: it's a prompt -----
        if not s["model"]:
            reply("⚠️ No model loaded. Pick one:\n" + models_block() +
                  "\n→ text a name or /open <name> to start.")
            # treat a bare valid model name/alias as an open
            full = resolve_model(body)
            if full:
                s["model"] = full; s["history"] = []; save_state(st)
                send(ACCOUNT if is_owner else sender, f"🤖 {full} — open. /help for commands.")
            return

        # generate — stream and flush paragraph-by-paragraph (on blank lines)
        target = ACCOUNT if is_owner else sender
        react(target, target_author, target_ts, "👀")
        if s["raw"]:
            messages = [{"role": "user", "content": body}]
        else:
            messages = [{"role": "system", "content": s["system"] or DEFAULT_SYSTEM}]
            messages += s["history"]
            messages.append({"role": "user", "content": body})
        full, buf, sent_any = "", "", False
        try:
            for delta in chat_stream(s["model"], messages, s["options"]):
                full += delta
                buf += delta
                while "\n\n" in buf:                 # flush each complete paragraph
                    para, buf = buf.split("\n\n", 1)
                    para = para.strip()
                    if para:
                        send(target, para); sent_any = True
        except Exception as e:
            react(target, target_author, target_ts, "❌")
            send(target, f"⚠️ error: {e}")
            return
        if buf.strip():                              # final partial paragraph
            send(target, buf.strip()); sent_any = True
        if not sent_any:
            send(target, "(empty response)")
        if not s["raw"]:
            s["history"].append({"role": "user", "content": body})
            s["history"].append({"role": "assistant", "content": full.strip()})
            save_state(st)
        react(target, target_author, target_ts, "✅")


# ----- SSE listener --------------------------------------------------------
def extract(envelope):
    """Return (sender, text, target_author, target_ts) or None."""
    env = envelope.get("envelope", envelope)
    ts = env.get("timestamp")
    # Note to Self: sync sentMessage to own account
    sync = env.get("syncMessage")
    if sync and isinstance(sync, dict):
        sent = sync.get("sentMessage")
        if sent and isinstance(sent, dict):
            dest = sent.get("destinationNumber") or sent.get("destination")
            if dest == ACCOUNT and sent.get("message"):
                return OWNER, sent["message"], OWNER, sent.get("timestamp", ts)
        return None
    # normal DM
    data = env.get("dataMessage")
    if data and isinstance(data, dict) and data.get("message"):
        sender = env.get("sourceNumber") or env.get("source")
        if sender:
            return sender, data["message"], sender, ts
    return None

def listen():
    url = SIGNAL_URL + f"/api/v1/events?account={ACCOUNT.replace('+', '%2B')}"
    while True:
        try:
            with urllib.request.urlopen(url, timeout=300) as r:
                print("SSE connected", flush=True)
                for raw in r:
                    line = raw.decode(errors="ignore").strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        payload = json.loads(line[5:].strip())
                    except Exception:
                        continue
                    got = extract(payload)
                    if not got:
                        continue
                    sender, text, tauthor, tts = got
                    print(f"recv from {sender}: {text[:60]!r}", flush=True)
                    if tts in _seen:
                        continue
                    _seen.add(tts)
                    if len(_seen) > 2000:
                        _seen.clear()
                    threading.Thread(target=handle, args=(sender, text, tauthor, tts),
                                     daemon=True).start()
        except Exception as e:
            print("SSE error, reconnecting:", e, flush=True)
            time.sleep(3)


if __name__ == "__main__":
    if not ACCOUNT:
        raise SystemExit("SIGNAL_ACCOUNT is not set. Export your linked Signal "
                         "number, e.g. SIGNAL_ACCOUNT=+15551234567 (see README).")
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    if not os.path.exists(STATE_FILE):
        save_state({"allow": {}, "sessions": {}})
    print(f"signal-ollama bridge starting (account {ACCOUNT[:5]}…)", flush=True)
    listen()
