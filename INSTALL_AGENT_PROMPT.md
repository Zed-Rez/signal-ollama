# One-paste agent install prompt

Copy everything in the box below and paste it to a coding agent (Claude Code,
etc.) running on the machine where you want the bot. Fill in your number first.

---

```
Set up the "signal-ollama" bridge on this machine so I can chat with my local
Ollama models over Signal. Work autonomously; only stop to have me scan a QR code
or to ask for my phone number if I didn't give it.

MY LINKED SIGNAL NUMBER (E.164): <FILL IN, e.g. +15551234567>
(If I leave this blank, ask me for it before linking.)

Do the following, checking each step before moving on:

0. BASE TOOLS: Ensure git, curl, python3, and Docker are installed. Detect with
   `command -v <tool>`; install whatever is missing (Debian/Ubuntu:
   `sudo apt-get update && sudo apt-get install -y git curl python3 docker.io`,
   then `sudo systemctl enable --now docker` and `sudo usermod -aG docker $USER`;
   macOS: Docker Desktop + Xcode CLT). Verify each before continuing.

1. OLLAMA: Verify Ollama is reachable at http://127.0.0.1:11434 (curl
   /api/tags). If the `ollama` binary is missing, install it
   (Linux: `curl -fsSL https://ollama.com/install.sh | sh`; macOS: the app from
   ollama.com/download) and start it (`ollama serve` if no service). Ensure at
   least one model is pulled (`ollama list`); if none, pull `qwen2.5-coder:3b`
   as a default and tell me.

2. SIGNAL-CLI DAEMON: Ensure a signal-cli HTTP daemon, version >= 0.14.5, is
   running on http://127.0.0.1:8080 (GET /api/v1/check returns 200). The easy
   path is Docker image `bbernhard/signal-cli-rest-api:latest`:
     docker run -d --name signal-daemon --restart unless-stopped \
       -p 127.0.0.1:8080:8080 \
       -v "$HOME/signal-cli-config:/home/.local/share/signal-cli" \
       --entrypoint signal-cli bbernhard/signal-cli-rest-api:latest \
       --config /home/.local/share/signal-cli daemon --http 0.0.0.0:8080 --no-receive-stdout
   Confirm version with `docker exec signal-daemon signal-cli --version`.
   IMPORTANT: versions < 0.14.5 silently drop all inbound messages — upgrade if older.

3. LINK MY NUMBER (only if `listAccounts` shows no account): stop the daemon,
   run `signal-cli ... link -n signal-ollama` to get a sgnl:// URI, render it as
   a QR PNG, and SHOW IT TO ME to scan (Signal app -> Settings -> Linked Devices
   -> + ). The URI expires in ~1 min, so regenerate if I'm slow. Restart the
   daemon. Confirm via listAccounts that my number is registered.

4. INSTALL THE BRIDGE:
     git clone https://github.com/Zed-Rez/signal-ollama ~/signal-ollama
     cd ~/signal-ollama
     export SIGNAL_ACCOUNT="<my number>"
     export SIGNAL_OWNER="$SIGNAL_ACCOUNT"
     ./install.sh
   This installs a `systemd --user` service that auto-starts on boot.

5. VERIFY END-TO-END: confirm `systemctl --user is-active signal-ollama` is
   active and the log shows "SSE connected". Then tell me to text `/help` to my
   number's Note to Self, and watch the log for a `recv from ...` line to confirm
   the round-trip works.

Report what you did at each step. Do not hardcode my phone number into any file
that could be committed to git — it must only live in the systemd unit's
environment. Do not commit state.json.
```

---

That's the whole install. The agent handles Ollama, the signal-cli daemon,
linking, and the service; you just scan one QR and send `/help`.
