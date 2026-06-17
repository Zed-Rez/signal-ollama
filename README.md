# signal-ollama

Talk to your **local [Ollama](https://ollama.com) models over Signal**, from any
phone. A tiny (~250-line, stdlib-only) Python bridge listens to a
[signal-cli](https://github.com/AsamK/signal-cli) daemon and pipes your messages
straight to Ollama's `/api/chat`. No cloud, no agent framework — just you and a
raw model in a Signal chat.

```
You:  /open ds70r
Bot:  🤖 deepseek-r1:70b — open. /help for commands.
You:  explain why the sky is blue, briefly       [bot reacts 👀]
                                                  [bot reacts ✅ when done]
Bot:  Rayleigh scattering: air scatters short (blue) wavelengths …
```

## Features

- **Any Ollama model** — `/open <name>`, a short alias, a base name, or even a
  unique substring. Auto-discovers whatever you have installed.
- **Per-user sessions**, persisted to disk (model, system prompt, params, and
  full conversation history) until you `/reset`. The model is **not** held in
  VRAM between turns.
- **Every generation parameter** tunable via `/set` (temperature, top_p, top_k,
  num_ctx, num_predict, repeat_penalty, seed, mirostat, …).
- **System prompts** via `/sys`, a `/raw` mode (no history, no system prompt),
  and a configurable default system prompt.
- **👀 / ✅ reactions** so you know it received your message and when it's done
  (handy for slow large models).
- **Owner-managed allowlist** — only you can grant others access (`/allow`,
  `/revoke`, `/users`); strangers are ignored and you get a heads-up.
- Runs as a **systemd user service** — auto-restart, starts on boot.

## Requirements

- **Ollama** running locally with at least one model pulled.
- **signal-cli 0.14.5+** running as an HTTP daemon, with a Signal account linked
  (a second phone number is recommended so the bot has its own identity — see
  *Caveats*). The easiest daemon is the Docker image
  [`bbernhard/signal-cli-rest-api`](https://github.com/bbernhard/signal-cli-rest-api).
- **Python 3.8+** (standard library only — no pip installs).

> ⚠️ **signal-cli must be ≥ 0.14.5.** Earlier versions crash on every inbound
> message (`getServerGuid … must not be null`) after a mid-2025 Signal server
> change. If incoming messages are silently dropped, upgrade signal-cli.

## Install

See **[INSTALL.md](INSTALL.md)** for the full step-by-step (signal-cli daemon,
linking your number, Ollama checks, pulling models, running the bridge).

Quick version, once signal-cli + Ollama are up and a number is linked:

```bash
git clone <your-fork-url> signal-ollama && cd signal-ollama
export SIGNAL_ACCOUNT="+15551234567"     # your linked number (E.164)
export SIGNAL_OWNER="$SIGNAL_ACCOUNT"    # allowlist admin (defaults to ACCOUNT)
./install.sh                              # installs + starts the systemd service
```

Then text **`/help`** to your linked number's **Note to Self** (or, if you used a
dedicated bot number, text that number from your phone).

## Configuration (environment variables)

| Var | Default | Meaning |
|-----|---------|---------|
| `SIGNAL_ACCOUNT` | *(required)* | Your linked Signal number, E.164 (`+15551234567`) |
| `SIGNAL_OWNER` | = `SIGNAL_ACCOUNT` | Number allowed to manage the allowlist |
| `SIGNAL_URL` | `http://127.0.0.1:8080` | signal-cli daemon base URL |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama base URL |
| `SIGNAL_OLLAMA_STATE` | `~/signal-ollama/state.json` | Session/allowlist store |
| `SIGNAL_OLLAMA_SYSTEM` | `You are a helpful, concise assistant.` | Default system prompt |

## Commands

```
/help                 full menu (commands, settings, models)
/models               list installed models (with [ALIAS] tags)
/open <model>         load/switch model (alias, name, or unique substring)
/close                unload current model
/reset                forget the conversation (keep model + system)
/info                 show model, system prompt, params, mode, history size
/sys <text>           set system prompt   ·   /sys (show)   ·   /sys clear
/raw                  toggle raw mode (no history, no system prompt)
/set <param> <value>  set any Ollama option   ·   /unset <param>
/temp <v>             shortcut for temperature
/ctx <n>              shortcut for num_ctx

Owner only:
/users                list allowed users
/allow <+number> [label]   grant access
/revoke <+number>     remove access
```

Anything that isn't a command is sent to the model as a prompt.

## Model aliases

Curated short codes (initials + parameter count + role) are shown by `/models`
next to each model; you can always use the full model name too. Add your own in
the `ALIASES` dict in `bridge.py`. Examples shipped:

`L70A` `GO120` `DS70R` `Q30C` `Q3C` `DV24` `L70U` `L405` `DLP70`

## Caveats

- **Identity:** if you link the bot onto your *personal* number, replies come
  from *you*, and "others texting the bot" means texting your number. For a clean
  separate bot identity, **register a dedicated number** for signal-cli.
- **Cold start:** large models reload into VRAM on the first message after idle —
  the 👀 reaction confirms it's working during the wait.
- **Trust:** anyone you `/allow` can run prompts on your hardware. Only allow
  people you trust.

## License

MIT
