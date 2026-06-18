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
import base64, json, os, threading, time, urllib.request, urllib.error

# --- config (all via env; see README) -------------------------------------
SIGNAL_URL = os.environ.get("SIGNAL_URL", "http://127.0.0.1:8080")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
ACCOUNT    = os.environ.get("SIGNAL_ACCOUNT", "").strip()        # the linked number, e.g. +15551234567
OWNER      = os.environ.get("SIGNAL_OWNER", ACCOUNT).strip()      # allowlist admin (defaults to ACCOUNT)
STATE_FILE = os.path.expanduser(os.environ.get("SIGNAL_OLLAMA_STATE", "~/signal-ollama/state.json"))
DEFAULT_SYSTEM = os.environ.get("SIGNAL_OLLAMA_SYSTEM", "You are a helpful, concise assistant.")
# where signal-cli writes received attachments (host side of the daemon's config volume)
ATTACH_DIR = os.path.expanduser(os.environ.get("SIGNAL_ATTACHMENTS_DIR",
                                               "~/.local/share/signal-cli/attachments"))
# how long (s) the active model user keeps the single-GPU lock before it frees
ACTIVE_TTL = int(os.environ.get("SIGNAL_ACTIVE_TTL", "600"))
# auto-clear a session's model after this much idle time, unless /keep is set
MODEL_TTL = int(os.environ.get("SIGNAL_MODEL_TTL", "900"))   # 15 min

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

# Curated short aliases (case-insensitive). Any model WITHOUT one gets an alias
# auto-generated at runtime: first >=3 letters of its name + parameter count in
# billions (e.g. mistral-small:24b -> MIS24). Curated entries always win.
CURATED = {
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

def _norm(name):
    return name[:-7] if name.endswith(":latest") else name

def _auto_alias(name, pbil, taken):
    """First >=3 letters of the model name + parameter-count-in-billions,
    lengthened until unique (e.g. MIS24)."""
    base = "".join(c for c in _norm(name).split(":")[0] if c.isalpha()).upper() or "MODEL"
    for n in range(3, len(base) + 1):
        cand = base[:n] + pbil
        if cand not in taken:
            return cand
    cand, k = base + pbil, 2
    while cand in taken:
        cand, k = f"{base}{pbil}_{k}", k + 1
    return cand

def build_aliases():
    """Return (alias_upper -> full model, normalized_full -> alias). Curated
    aliases win; every other installed model gets an auto alias."""
    a2f, f2a, taken = {}, {}, set()
    for short, full in CURATED.items():
        a2f[short.upper()] = full
        f2a[_norm(full)] = short
        taken.add(short.upper())
    for m in sorted(_tags(), key=lambda x: x.get("name", "")):
        full = m.get("name", "")
        if not full or _norm(full) in f2a:
            continue
        cand = _auto_alias(full, _param_billions(m), taken)
        a2f[cand] = full
        f2a[_norm(full)] = cand
        taken.add(cand)
    return a2f, f2a

def resolve_model(name):
    """Map an alias / full name / base name / unique substring to an installed
    model. Works for any model in Ollama (curated or auto-aliased)."""
    if not name:
        return None
    q = name.strip()
    a2f, _ = build_aliases()
    if q.upper() in a2f:                           # alias (curated or auto)
        q = a2f[q.upper()]
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

def _prio(st, key):
    """Priority rank: owner is always highest; others default to 0."""
    if key == OWNER:
        return 10 ** 9
    return int(st.get("rank", {}).get(key, 0))

def _label(st, key):
    if key == OWNER:
        return "owner"
    return st.get("allow", {}).get(key) or (key[:8] + "…" if len(key) > 12 else key)


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

def send(recipient, text, attachments=None, group=None):
    params = {"account": ACCOUNT, "message": text}
    if group:
        params["groupId"] = group
    else:
        params["recipient"] = [recipient]
    if attachments:
        params["attachments"] = attachments
    rpc("send", params)

def react(recipient, target_author, target_ts, emoji, remove=False, group=None):
    params = {"account": ACCOUNT, "emoji": emoji, "targetAuthor": target_author,
              "targetTimestamp": target_ts, "remove": remove}
    if group:
        params["groupId"] = group
    else:
        params["recipient"] = [recipient]
    rpc("sendReaction", params)

def deliver(chat_key, msg, attachments=None):
    """Send to a chat by its session key: a group ('g:<id>'), the owner ('self'),
    or a 1:1 user id."""
    if isinstance(chat_key, str) and chat_key.startswith("g:"):
        send(None, msg, attachments=attachments, group=chat_key[2:])
    elif chat_key == OWNER:
        send(ACCOUNT, msg, attachments=attachments)
    else:
        send(chat_key, msg, attachments=attachments)


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
def _tags():
    try:
        with urllib.request.urlopen(OLLAMA_URL + "/api/tags", timeout=15) as r:
            return json.load(r).get("models", [])
    except Exception as e:
        print("ollama tags error", e, flush=True)
        return []

def list_models():
    return sorted(m["name"] for m in _tags())

def _param_billions(m):
    """Whole-billions parameter count from Ollama metadata, e.g. '70.6B' -> '70'."""
    ps = (m.get("details") or {}).get("parameter_size") or ""
    num = ""
    for ch in ps:
        if ch.isdigit() or ch == ".":
            num += ch
        elif num:
            break
    try:
        return str(int(float(num))) if num else ""   # truncate: 70.6B -> "70"
    except ValueError:
        return ""

def model_exists(name):
    return resolve_model(name) is not None

def model_caps(model):
    """Ollama capabilities list for a model, e.g. ['completion','vision']."""
    try:
        req = urllib.request.Request(OLLAMA_URL + "/api/show",
                                     data=json.dumps({"model": model}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r).get("capabilities") or []
    except Exception as e:
        print("ollama show error", e, flush=True)
        return []

def model_is_vision(model):
    """True if the model can read images (image input)."""
    return "vision" in model_caps(model)

def model_is_imagegen(model):
    """True if the model generates images (image output) and isn't a chat model."""
    caps = model_caps(model)
    return "image" in caps and "completion" not in caps

def generate_image(model, prompt, options):
    """Run a text->image model, return a list of base64 PNGs (raises on error)."""
    body = json.dumps({"model": model, "prompt": prompt, "stream": False,
                       "options": options}).encode()
    req = urllib.request.Request(OLLAMA_URL + "/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=1800) as r:
        d = json.load(r)
    if d.get("error"):
        raise RuntimeError(d["error"])
    return d.get("images") or []

def read_image_b64(attachment):
    """Read a received image attachment off disk and base64-encode it."""
    path = os.path.join(ATTACH_DIR, attachment.get("id", ""))
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()

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
    _, f2a = build_aliases()
    lines = []
    for m in list_models():
        d = MODEL_DESC.get(m) or MODEL_DESC.get(_norm(m))
        alias = f2a.get(_norm(m))
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
        "• /keep           pin the model (don't auto-close it after idle)",
        "• /reset          forget the conversation (keep model + system)",
        "• /info           show current model, system, params, mode",
        "• /help           this menu",
        "",
        "Prompting:",
        "• /ask <prompt>   prompt the model (needed in group chats; optional in DMs)",
        "• /sys <text>     set system prompt",
        "• /sys            show system prompt",
        "• /sys clear      revert to default system prompt",
        "• /raw            toggle raw mode (no history, no system prompt)",
        "",
        "In group chats: only commands and /ask reach the bot (no plain chatter).",
        "Images: send a photo to a vision model; image-gen models reply with a picture.",
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
              "• /revoke <id>          remove access",
              "• /rank <id> <n>        set priority (higher wins the model)",
              "• /who                  who holds the model + your DM target",
              "• /msg <id> <text>      message someone directly (no model)",
              "• /dm <id>              relay mode: your texts go to them; /close to exit"]
    t += ["", "Models:", models_block()]
    return "\n".join(t)

def info_text(s):
    opts = ", ".join(f"{k}={v}" for k, v in s["options"].items()) or "(defaults)"
    return ("ℹ️ session\n"
            f"model:  {s['model'] or '(none)'}\n"
            f"system: {s['system'] or DEFAULT_SYSTEM}\n"
            f"mode:   {'RAW (no history/system)' if s['raw'] else 'chat (multi-turn)'}\n"
            f"params: {opts}\n"
            f"history: {len(s['history'])} msgs\n"
            f"auto-close: {'pinned (/keep)' if s.get('keep') else str(MODEL_TTL // 60) + 'm idle'}")


# ----- message handling ----------------------------------------------------
def handle(sender, text, target_author, target_ts, attachments=None, group=None):
    key = canonical(sender)                       # phone number if known, else the id (uuid)
    is_owner = (key == OWNER or sender == OWNER)
    chat_key = ("g:" + group) if group else key   # session + lock scope (group or 1:1)
    with user_lock(chat_key):                     # per-chat lock (no global deadlock on a slow model)
        st = load_state()

        def reply(msg, attachments=None):         # reply to wherever the message came from
            send(None if group else (ACCOUNT if is_owner else sender), msg,
                 attachments=attachments, group=group)
        def ack(emoji):
            react(None if group else (ACCOUNT if is_owner else sender),
                  target_author, target_ts, emoji, group=group)
        notify = deliver                          # send to any chat by its session key

        # relay inbound (1:1 only): if this sender is the owner's DM target, forward to owner
        dm_target = st.get("sessions", {}).get(OWNER, {}).get("dm")
        if not group and not is_owner and dm_target and dm_target in (sender, key):
            send(ACCOUNT, f"💬 {_label(st, key)}: {text}")
            return

        # access control — owner + allowlisted; else ignored (pending tracked in 1:1 only)
        if not is_owner and sender not in st["allow"] and key not in st["allow"]:
            if not group:
                st.setdefault("pending", {})[sender] = text[:60]; save_state(st)
            return
        s = session(st, chat_key)
        body = text.strip()

        # /ask <prompt> prompts the model inside a group (also works in DMs)
        ask = body.startswith("/") and body.lower().split(" ", 1)[0] == "/ask"
        if ask:
            body = body[4:].strip()
            if not body:
                reply("Usage: /ask <prompt>"); return
        # in groups, ignore ordinary chatter — only commands and /ask reach the bot
        if group and not ask and not body.startswith("/"):
            return

        s["last"] = time.time()                   # real interaction → reset the idle timer
        save_state(st)

        # ----- commands -----
        if body.startswith("/") and not ask:
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
                if is_owner and s.get("dm"):            # exit DM relay first
                    tgt = s.pop("dm"); save_state(st); reply(f"💬 DM with {tgt} closed."); return
                s["model"] = None; s["history"] = []
                if (st.get("active") or {}).get("key") == chat_key:
                    st["active"] = {}                   # free the model lock
                save_state(st); reply("Session closed. Text a model name to start again."); return
            if cmd == "/reset":
                s["history"] = []; save_state(st); reply("✓ conversation cleared (model + system kept)."); return
            if cmd == "/keep":
                s["keep"] = not s.get("keep", False); save_state(st)
                reply("📌 model pinned — it won't auto-close."
                      if s["keep"] else f"⏳ model will auto-close after {MODEL_TTL // 60}m idle.")
                return
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
            # owner: messaging people directly (no model) + priority ranks
            if cmd in ("/msg", "/dm", "/rank", "/who"):
                if not is_owner:
                    reply("Owner only."); return
                if cmd == "/who":
                    act = st.get("active") or {}
                    holding = act.get("key") and (time.time() - act.get("ts", 0) < ACTIVE_TTL)
                    who = f"{act.get('label')} (model)" if holding else "nobody"
                    reply(f"active model user: {who}\nyour DM target: {s.get('dm') or '—'}"); return
                if cmd == "/msg":
                    sp = arg.split(maxsplit=1)
                    if len(sp) < 2:
                        reply("Usage: /msg <+number|uuid> <text>"); return
                    send(sp[0], sp[1]); reply(f"✓ sent to {sp[0]}."); return
                if cmd == "/dm":
                    who = arg.split()[0] if arg else ""
                    if not who:
                        if s.get("dm"):
                            t = s.pop("dm"); save_state(st); reply(f"💬 DM with {t} closed."); return
                        reply("Usage: /dm <+number|uuid>  — then your texts go to them (not the model); /close to exit."); return
                    s["dm"] = who; save_state(st)
                    reply(f"💬 DM mode on: your messages now go to {who} (not the model). Their replies come back here. /close to exit."); return
                if cmd == "/rank":
                    sp = arg.split()
                    if len(sp) != 2:
                        reply("Usage: /rank <+number|uuid> <n>   (higher n = higher priority)"); return
                    try:
                        st.setdefault("rank", {})[sp[0]] = int(sp[1])
                    except ValueError:
                        reply("rank must be a whole number."); return
                    save_state(st); reply(f"✓ {sp[0]} priority = {sp[1]}"); return
            reply(f"Unknown command {cmd}. /help for the menu."); return

        # owner DM relay (outbound, 1:1 only): plain text goes to the person, not the model
        if is_owner and not group and s.get("dm"):
            send(s["dm"], body)
            return

        # ----- not a command: it's a prompt (maybe with image attachments) -----
        images = [a for a in (attachments or [])
                  if str(a.get("contentType", "")).startswith("image/")]

        if not s["model"]:
            reply("⚠️ No model loaded. Pick one:\n" + models_block() +
                  ("\n→ /open <name>, then /ask <prompt>." if group
                   else "\n→ text a name or /open <name> to start."))
            full = resolve_model(body)            # a bare model name/alias opens it
            if full:
                s["model"] = full; s["history"] = []; save_state(st)
                reply(f"🤖 {full} — open. /help for commands.")
            return

        # single active model user — priority-ranked; previous holder notified on takeover
        now = time.time()
        my_prio = _prio(st, key)
        act = st.get("active") or {}
        held = act.get("key") and (now - act.get("ts", 0) < ACTIVE_TTL)
        if held and act["key"] != chat_key:
            if my_prio >= act.get("prio", 0):
                notify(act["key"], f"🔔 {_label(st, key)} took over the bot.")
            else:
                reply(f"⏳ {act.get('label')} is using the bot — you'll get it when they're free.")
                return
        st["active"] = {"key": chat_key, "label": ("a group" if group else _label(st, key)),
                        "prio": my_prio, "ts": now}
        save_state(st)

        # image-generation models: produce an image and send it back
        if model_is_imagegen(s["model"]):
            ack("👀")
            try:
                imgs = generate_image(s["model"], body, s["options"])
            except Exception as e:
                ack("❌"); reply(f"⚠️ image generation failed: {e}"); return
            if not imgs:
                ack("❌"); reply("⚠️ no image produced."); return
            reply("🖼️", attachments=[f"data:image/png;base64,{b}" for b in imgs])
            ack("✅"); return

        # images only go to vision-capable models
        image_b64 = []
        if images:
            if not model_is_vision(s["model"]):
                reply(f"⚠️ {s['model']} is not an image model — it can't read images. "
                      f"/open a vision model (a llava / *-vision model) first.")
                return
            try:
                image_b64 = [read_image_b64(a) for a in images]
            except Exception as e:
                reply(f"⚠️ couldn't read the image: {e}"); return

        # generate — stream and flush paragraph-by-paragraph (on blank lines)
        ack("👀")
        user_msg = {"role": "user", "content": body or "(image)"}
        if image_b64:
            user_msg["images"] = image_b64
        if s["raw"]:
            messages = [user_msg]
        else:
            messages = [{"role": "system", "content": s["system"] or DEFAULT_SYSTEM}]
            messages += s["history"]
            messages.append(user_msg)
        full, buf, sent_any = "", "", False
        try:
            for delta in chat_stream(s["model"], messages, s["options"]):
                full += delta
                buf += delta
                while "\n\n" in buf:                 # flush each complete paragraph
                    para, buf = buf.split("\n\n", 1)
                    para = para.strip()
                    if para:
                        reply(para); sent_any = True
        except Exception as e:
            ack("❌"); reply(f"⚠️ error: {e}"); return
        if buf.strip():                              # final partial paragraph
            reply(buf.strip()); sent_any = True
        if not sent_any:
            reply("(empty response)")
        if not s["raw"]:
            hist = (body + " [image]").strip() if (image_b64 and body) else ("[image]" if image_b64 else body)
            s["history"].append({"role": "user", "content": hist})
            s["history"].append({"role": "assistant", "content": full.strip()})
            save_state(st)
        ack("✅")


# ----- SSE listener --------------------------------------------------------
def extract(envelope):
    """Return (sender, text, target_author, target_ts, attachments, group) or None.
    group is a base64 groupId when the message came from a Signal group, else None."""
    env = envelope.get("envelope", envelope)
    ts = env.get("timestamp")
    # Owner's own messages (Note to Self, or messages the owner sent to a group)
    sync = env.get("syncMessage")
    if sync and isinstance(sync, dict):
        sent = sync.get("sentMessage")
        if sent and isinstance(sent, dict):
            dest = sent.get("destinationNumber") or sent.get("destination")
            atts = sent.get("attachments") or []
            group = (sent.get("groupInfo") or {}).get("groupId")
            if group and (sent.get("message") or atts):
                return OWNER, sent.get("message") or "", OWNER, sent.get("timestamp", ts), atts, group
            if dest == ACCOUNT and (sent.get("message") or atts):
                return OWNER, sent.get("message") or "", OWNER, sent.get("timestamp", ts), atts, None
        return None
    # incoming DM or group message
    data = env.get("dataMessage")
    if data and isinstance(data, dict):
        atts = data.get("attachments") or []
        group = (data.get("groupInfo") or {}).get("groupId")
        if data.get("message") or atts:
            sender = env.get("sourceNumber") or env.get("sourceUuid") or env.get("source")
            if sender:
                return sender, data.get("message") or "", sender, ts, atts, group
    return None

def reaper():
    """Auto-clear each session's model after MODEL_TTL idle, unless it's /keep-pinned."""
    while True:
        time.sleep(60)
        try:
            st = load_state()
            now = time.time()
            changed = False
            for ck, s in list(st.get("sessions", {}).items()):
                if s.get("model") and not s.get("keep") and now - s.get("last", 0) > MODEL_TTL:
                    s["model"] = None
                    s["history"] = []
                    if (st.get("active") or {}).get("key") == ck:
                        st["active"] = {}
                    changed = True
                    deliver(ck, f"💤 model auto-closed after {MODEL_TTL // 60}m idle. "
                                f"/open to start again (or /keep next time to pin it).")
            if changed:
                save_state(st)
        except Exception as e:
            print("reaper error", e, flush=True)

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
                    sender, text, tauthor, tts, atts, group = got
                    print(f"recv from {sender}{' [group]' if group else ''}: {text[:60]!r}" +
                          (f" (+{len(atts)} attachment)" if atts else ""), flush=True)
                    if tts in _seen:
                        continue
                    _seen.add(tts)
                    if len(_seen) > 2000:
                        _seen.clear()
                    threading.Thread(target=handle, args=(sender, text, tauthor, tts, atts, group),
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
    threading.Thread(target=reaper, daemon=True).start()
    listen()
