# Agent Gateway (Ollama + API key + optional HTTPS + tool calling)

Client --(HTTP or HTTPS) + API key--> gateway (server.py) --localhost--> Ollama

## 1. Install the backend
- Linux: `curl -fsSL https://ollama.com/install.sh | sh`
- Windows: install from https://ollama.com/download
- Pull a tool-capable model:
  - CPU only / small machine: `ollama pull llama3.2:3b` (or `qwen2.5:3b`)
  - More RAM or a GPU: `ollama pull qwen2.5:7b`

## 2. Install the gateway
```
python -m venv .venv
# Linux:   source .venv/bin/activate
# Windows: .venv\Scripts\activate
pip install fastapi uvicorn httpx pyyaml
```

## 3. Configure (`config.yaml`)
Key settings:
- `server.host` / `server.port`: bind address. Default `127.0.0.1` (this machine only).
- `server.tls_enabled`: `false` for now; set `true` after step 7.
- `server.api_key_env`: the NAME of the environment variable that holds the key
  (keep it `AGENT_API_KEY`; never paste the key itself here).
- `backend.model`, `backend.timeout_s`: model name and request timeout (use 600 on CPU).
- `model.num_ctx`, `model.temperature`, `model.top_p`, `model.num_predict`.
- `agent.max_tool_steps`, `agent.enabled_tools`, `agent.system_prompt`.

Everything except `server.*` is re-read on every request (no restart).
`server.*` changes and edits to `server.py` need a restart.

## 4. Create an API key and run the server
Generate a key:
```
python -c "import secrets; print(secrets.token_hex(32))"
```
Window 1 (server). The key must be set BEFORE starting the server:
```
# Windows PowerShell
cd path\to\project
$env:AGENT_API_KEY = "your-key"
python server.py

# Linux
export AGENT_API_KEY="your-key"
python server.py
```
Wait for `Uvicorn running on http://127.0.0.1:8443` and leave the window open.

## 5. Test (second window)
Environment variables are per window, so set the same key here too.
Create `body.json` in the project folder:
```json
{"messages":[{"role":"user","content":"First use the get_time tool. Then use the calculator tool to multiply the current hour by 7. Tell me the result."}]}
```
Run:
```
# Windows PowerShell
$env:AGENT_API_KEY = "your-key"
curl.exe -H "Authorization: Bearer $env:AGENT_API_KEY" -H "Content-Type: application/json" --data "@body.json" http://127.0.0.1:8443/chat

# Linux
curl -H "Authorization: Bearer $AGENT_API_KEY" -H "Content-Type: application/json" --data @body.json http://127.0.0.1:8443/chat
```
`body.json` is just the request body; edit its `content` to ask something else.
The response has `reply` and `tool_trace` (each tool call in order, showing chaining).
The first request is slow while the model loads (on CPU, minutes is normal).

Endpoints:
- `GET /` welcome, `GET /health` liveness, `GET /docs` interactive docs
- `GET /config` current settings (key required)
- `POST /chat` talk to the agent (key required)

## 6. Troubleshooting
| Symptom | Cause / fix |
|---|---|
| `{"detail":"Not Found"}` | Wrong path. Use `/`, `/health`, `/config` or `/chat`. |
| `{"detail":"set env var NAME"}` | Server has no key. Set `AGENT_API_KEY` BEFORE `python server.py`; `api_key_env` in config must be `"AGENT_API_KEY"`. |
| `{"detail":"bad API key"}` | Key in the curl window differs from the server's. Compare `echo $env:AGENT_API_KEY` in both. |
| Port 8443 error (10048) | An old server is running. `netstat -ano \| findstr :8443`, then `taskkill /PID <pid> /F`. |
| `Could not connect to server` | Server not running. Start it again. |
| `502 backend error` | Ollama not running, wrong model name, or timeout. Check `ollama list`. |
| Tool calls wrong or skipped | Small model. Use explicit prompts, or try `qwen2.5:3b` / `qwen2.5:7b`. |

## 7. Enable encryption (TLS) later
Create a self-signed certificate (replace the IP with your server's):
```
mkdir certs
openssl req -x509 -newkey rsa:4096 -nodes -days 365 \
  -keyout certs/server.key -out certs/server.crt \
  -subj "/CN=agent" -addext "subjectAltName=IP:192.168.1.50,IP:127.0.0.1,DNS:localhost"
```
(Windows has no openssl by default: `winget install ShiningLight.OpenSSL.Light`.)
Then set `tls_enabled: true`, restart, and use `https://` with
`curl --cacert certs/server.crt ...`. For real use: Let's Encrypt or a private CA.
Only `server.crt` goes to clients; keep `server.key` private.

Before TLS, to reach it from another machine, keep host `127.0.0.1` and use an SSH
tunnel: `ssh -L 8443:127.0.0.1:8443 user@server`.

## Security notes
- Keep Ollama bound to 127.0.0.1 (its default); expose only the gateway.
- Do not bind to `0.0.0.0` with TLS off; the API key would travel in clear text.
- Never share or commit your API key. If it leaks, make a new one and restart.
- Open the firewall for the gateway port only.
- Each tool is something the model can do on your machine; add them carefully.

## Tools
Built in: `get_time`, `calculator` (a, b, operation), `read_note`, `write_note`
(files stay inside `notes_dir`). To add one, decorate a function with
`@tool(name, description, properties, required)` in server.py and add its name to
`agent.enabled_tools`. Prefer simple, typed parameters; small models handle them
better than free-form expressions.