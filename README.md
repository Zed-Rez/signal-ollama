# signal-ollama

Chat with your **local [Ollama](https://ollama.com) models over Signal**, from
any phone. A single-file, stdlib-only Python bridge listens to a
[signal-cli](https://github.com/AsamK/signal-cli) daemon and pipes your messages
straight to Ollama's `/api/chat`. No cloud, no agent framework — just you and a
raw model in a Signal chat.

```
You:  /open ds70r
Bot:  🤖 deepseek-r1:70b — open. /help for commands.
You:  why is the sky blue, briefly        [bot reacts 👀, then ✅ when done]
Bot:  Rayleigh scattering — air scatters short (blue) wavelengths most.
```

## Install (easiest: one paste + one QR scan)

Paste the prompt in **[INSTALL_AGENT_PROMPT.md](INSTALL_AGENT_PROMPT.md)** to a
coding agent (Claude Code, etc.) on the target machine. It installs the base
tools, Ollama, the signal-cli daemon, and the service for you — **the only thing
you do by hand is scan one QR code in the Signal app.**

Prefer to do it yourself? See **[INSTALL.md](INSTALL.md)**. Short version once
Ollama + a signal-cli daemon are running and a number is linked:

```bash
git clone https://github.com/Zed-Rez/signal-ollama && cd signal-ollama
export SIGNAL_ACCOUNT="+15551234567"   # your linked number (E.164)
./install.sh                            # installs + starts the systemd service
```

Then text **`/help`** to that number's **Note to Self**.

## Features

- **Any Ollama model** — by alias, full name, or unique substring; auto-discovers
  what you have installed.
- **Persisted per-user sessions** — model, system prompt, params, and history
  saved to disk until `/reset`; the model isn't pinned in VRAM between turns.
- **Every generation parameter** tunable via `/set` (temperature, top_p, num_ctx,
  seed, mirostat, …), plus a `/raw` mode and a configurable default system prompt.
- **Streams paragraph-by-paragraph** — replies arrive as each paragraph finishes,
  not as one wall of text at the end; 👀 / ✅ reactions mark received vs. done.
- **Owner-managed allowlist** — only you can `/allow` others; strangers are
  ignored and you get a heads-up.
- Runs as a **systemd user service** (auto-restart, starts on boot).

## Commands

```
/help                 full menu (commands, settings, models)
/models               list installed models (with [ALIAS] tags)
/open <model>         load/switch model (alias, name, or unique substring)
/close                unload current model      /reset   forget the conversation
/info                 show model, system, params, mode, history size
/sys <text>           set system prompt   ·   /sys (show)   ·   /sys clear
/raw                  toggle raw mode (no history, no system prompt)
/set <param> <value>  set any Ollama option  ·  /unset  ·  /temp <v>  ·  /ctx <n>

Owner only:  /users   ·   /allow <+number> [label]   ·   /revoke <+number>
```

Anything that isn't a command is sent to the model as a prompt. Short model
aliases (e.g. `L70A`, `GO120`, `DS70R`, `Q30C`) are shown by `/models`; edit the
`ALIASES` dict in `bridge.py` to add your own.

## Configuration (environment)

| Var | Default | Meaning |
|-----|---------|---------|
| `SIGNAL_ACCOUNT` | *(required)* | Your linked Signal number, E.164 |
| `SIGNAL_OWNER` | = `SIGNAL_ACCOUNT` | Number allowed to manage the allowlist |
| `SIGNAL_URL` | `http://127.0.0.1:8080` | signal-cli daemon base URL |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama base URL |
| `SIGNAL_OLLAMA_STATE` | `~/signal-ollama/state.json` | Session/allowlist store |
| `SIGNAL_OLLAMA_SYSTEM` | `You are a helpful, concise assistant.` | Default system prompt |

> ⚠️ **signal-cli must be ≥ 0.14.5.** Earlier versions crash on every inbound
> message (`getServerGuid … must not be null`) after a 2025 Signal server change.

## Caveats

- **Identity:** on your *personal* number, replies come from *you* and others
  "texting the bot" means texting your number. For a separate identity, register a
  dedicated number for signal-cli.
- **Trust:** anyone you `/allow` can run prompts on your hardware.

## License

MIT — see [LICENSE](LICENSE).
