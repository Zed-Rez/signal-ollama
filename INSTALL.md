# Install guide — signal-ollama

End-to-end setup on a fresh machine: base tools → Ollama → signal-cli daemon →
link a number → run the bridge. Linux/macOS. The only steps needing root are
installing the base packages (git/curl/python3/Docker) in step 0.

---

## 0. Prerequisites (fresh machine)

The bridge is pure Python stdlib, but the surrounding pieces need a few base
tools: **git, curl, python3, and Docker**. Check what's there and install the rest.

**Debian/Ubuntu:**
```bash
# detect what's missing
for c in git curl python3 docker; do
  command -v "$c" >/dev/null && echo "$c: ok" || echo "$c: MISSING"
done

# install whatever was missing
sudo apt-get update
sudo apt-get install -y git curl python3 docker.io

# start Docker and allow running it without sudo
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"     # then log out/in, or run: newgrp docker
```

**macOS:** install [Docker Desktop](https://www.docker.com/products/docker-desktop/)
and start it; `git`, `curl`, `python3` come with the Xcode CLT
(`xcode-select --install`).

Verify all four:
```bash
git --version && curl --version | head -1 && python3 --version && docker --version
```

## 1. Ollama + a model

Install Ollama if it's not already present, make sure its API is up, then pull a
model:

```bash
# install if missing
#   Linux:
command -v ollama >/dev/null || curl -fsSL https://ollama.com/install.sh | sh
#   macOS: download the app from https://ollama.com/download (or: brew install ollama)

# is the API up? (the Linux installer starts a service; otherwise run `ollama serve &`)
curl -s http://127.0.0.1:11434/api/tags >/dev/null \
  && echo "Ollama up" || echo "Ollama not running — start it: 'ollama serve'"

# pull a model (pick any; small one shown for a quick test)
ollama pull qwen2.5-coder:3b
# ...or something beefier if your hardware allows:
# ollama pull deepseek-r1:70b

# confirm it's listed
ollama list
```

> Tip: to bake a larger context window into a model, create a derivative:
> ```bash
> printf 'FROM <base-model>\nPARAMETER num_ctx 65536\n' > Modelfile
> ollama create <name>-64k -f Modelfile
> ```

## 2. signal-cli daemon (Docker)

Run the daemon (signal-cli **≥ 0.14.5** — older versions drop all inbound
messages). Data persists in `./signal-cli-config`:

```bash
mkdir -p "$HOME/signal-cli-config"
docker run -d --name signal-daemon --restart unless-stopped \
  -p 127.0.0.1:8080:8080 \
  -v "$HOME/signal-cli-config:/home/.local/share/signal-cli" \
  --entrypoint signal-cli bbernhard/signal-cli-rest-api:latest \
  --config /home/.local/share/signal-cli daemon --http 0.0.0.0:8080 --no-receive-stdout

# health + version (want 0.14.5+)
curl -s -o /dev/null -w 'daemon: %{http_code}\n' http://127.0.0.1:8080/api/v1/check
docker exec signal-daemon signal-cli --version
```

### signal-cli help / useful calls

```bash
# list linked accounts
curl -s -X POST http://127.0.0.1:8080/api/v1/rpc -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"listAccounts","id":1}'

# send a test message (after linking)
curl -s -X POST http://127.0.0.1:8080/api/v1/rpc -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"send","params":{"account":"+15551234567","recipient":["+15551234567"],"message":"hi"},"id":1}'
```

## 3. Link a Signal number

You're adding signal-cli as a **linked device** (your phone stays primary; no SMS
verification needed). **Recommended:** use a *dedicated* second number so the bot
has its own identity rather than your personal one.

The robust way to link (daemon stopped so it doesn't hold the config lock):

```bash
docker stop signal-daemon
docker run --rm -v "$HOME/signal-cli-config:/home/.local/share/signal-cli" \
  --entrypoint signal-cli bbernhard/signal-cli-rest-api:latest \
  --config /home/.local/share/signal-cli link -n "signal-ollama"
# It prints a sgnl://... URI. Turn it into a QR and scan it:
#   Signal app → Settings → Linked Devices → + → scan
# (the URI expires in ~1 min; rerun if it lapses)
docker start signal-daemon
```

Render the printed URI as a QR however you like, e.g.:

```bash
pip install --user qrcode      # or: pipx run qrcode "<uri>"
python3 -c "import qrcode,sys; qrcode.make(sys.argv[1]).save('link.png')" "<paste-uri>"
```

Confirm it linked:

```bash
curl -s -X POST http://127.0.0.1:8080/api/v1/rpc -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"listAccounts","id":1}'
# -> {"result":[{"number":"+15551234567"}], ...}
```

## 4. The bridge

```bash
git clone <your-fork-url> signal-ollama && cd signal-ollama
export SIGNAL_ACCOUNT="+15551234567"     # the number you just linked
export SIGNAL_OWNER="$SIGNAL_ACCOUNT"    # who can manage the allowlist
./install.sh                              # installs + enables the systemd --user service
```

`install.sh` writes a `systemd --user` unit, enables it (so it starts on boot —
it also runs `loginctl enable-linger` so it survives logout), and starts it.

Verify and tail logs:

```bash
systemctl --user status signal-ollama.service
journalctl --user -u signal-ollama.service -f
```

## 5. Use it

Text **`/help`** to the linked number's **Note to Self** (or text a dedicated bot
number from your phone). Then:

```
/models                 # see what's installed
/open qwen2.5-coder:3b  # or an alias / unique substring
hello                   # chat
/sys You are terse.     # set a system prompt
/temp 0.8               # tune any parameter
/reset                  # clear history
```

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Inbound messages silently dropped | signal-cli too old — upgrade to ≥ 0.14.5 |
| `daemon: 000` from the health check | daemon not up / wrong port |
| `docker: permission denied` | run `newgrp docker` (or log out/in) after `usermod -aG docker` |
| `ollama: command not found` | not installed — see step 1 |
| `SIGNAL_ACCOUNT is not set` on start | export it (and re-`install.sh`) |
| No reply, no 👀 | check `journalctl --user -u signal-ollama -f` for `recv from …` |
| Replies appear to come "from you" | expected on a personal number — use a dedicated number |
