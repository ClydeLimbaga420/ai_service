"""Agent gateway: HTTPS + API key -> Ollama, with config hot-reload and a tool-calling loop."""
import ast, datetime, hmac, operator, os, pathlib
import httpx, uvicorn, yaml
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

CONFIG_PATH = os.environ.get("AGENT_CONFIG", "config.yaml")
_cache = {"mtime": 0.0, "data": {}}


def cfg() -> dict:
    """Re-read config.yaml whenever it changes on disk."""
    m = os.path.getmtime(CONFIG_PATH)
    if m != _cache["mtime"]:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            _cache["data"] = yaml.safe_load(f)
        _cache["mtime"] = m
    return _cache["data"]


# ---------------- tools ----------------
TOOLS = {}  # name -> (schema, function)


def tool(name, description, properties, required):
    def deco(fn):
        TOOLS[name] = (
            {"type": "function", "function": {
                "name": name, "description": description,
                "parameters": {"type": "object", "properties": properties, "required": required}}},
            fn)
        return fn
    return deco


@tool("get_time", "Get the current local date and time. Returns the hour (0-23) and minute as plain numbers.", {}, [])
def get_time():
    n = datetime.datetime.now()
    return f"{n.isoformat(timespec='seconds')} (hour={n.hour}, minute={n.minute})"


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.Pow: operator.pow, ast.USub: operator.neg,
        ast.Mod: operator.mod}


def _eval(n):
    if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
        return n.value
    if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
        return _OPS[type(n.op)](_eval(n.left), _eval(n.right))
    if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
        return _OPS[type(n.op)](_eval(n.operand))
    raise ValueError("unsupported expression")


@tool("calculator", "Do arithmetic on two numbers. Example: a=14, b=7, operation='multiply' gives 98.",
      {"a": {"type": "number", "description": "first number"},
       "b": {"type": "number", "description": "second number"},
       "operation": {"type": "string", "enum": ["add", "subtract", "multiply", "divide"]}},
      ["a", "b", "operation"])
def calculator(a, b, operation: str):
    a, b = float(a), float(b)
    ops = {"add": a + b, "subtract": a - b, "multiply": a * b}
    if operation == "divide":
        if b == 0:
            raise ValueError("division by zero")
        result = a / b
    elif operation in ops:
        result = ops[operation]
    else:
        raise ValueError("operation must be add, subtract, multiply or divide")
    return str(int(result)) if result == int(result) else str(result)


def _note_path(name: str) -> pathlib.Path:
    base = pathlib.Path(cfg()["agent"]["notes_dir"]).resolve()
    base.mkdir(exist_ok=True)
    p = (base / name).resolve()
    if base not in p.parents:  # block path traversal
        raise ValueError("path outside notes dir")
    return p


@tool("read_note", "Read a text note by file name.",
      {"name": {"type": "string"}}, ["name"])
def read_note(name: str):
    return _note_path(name).read_text(encoding="utf-8")


@tool("write_note", "Write text to a note file.",
      {"name": {"type": "string"}, "content": {"type": "string"}}, ["name", "content"])
def write_note(name: str, content: str):
    _note_path(name).write_text(content, encoding="utf-8")
    return "ok"


# ---------------- agent loop ----------------
def run_agent(messages: list) -> dict:
    c = cfg()
    enabled = [n for n in c["agent"]["enabled_tools"] if n in TOOLS]
    schemas = [TOOLS[n][0] for n in enabled]
    convo = [{"role": "system", "content": c["agent"]["system_prompt"]}] + messages
    trace = []
    for _ in range(c["agent"]["max_tool_steps"] + 1):
        r = httpx.post(
            f'{c["backend"]["url"]}/api/chat',
            json={"model": c["backend"]["model"], "messages": convo, "tools": schemas,
                  "stream": False, "options": c["model"]},
            timeout=c["backend"]["timeout_s"])
        r.raise_for_status()
        msg = r.json()["message"]
        convo.append(msg)
        calls = msg.get("tool_calls") or []
        if not calls:
            return {"reply": msg.get("content", ""), "tool_trace": trace}
        for call in calls:
            name, args = call["function"]["name"], call["function"].get("arguments", {})
            try:
                if name not in enabled:
                    raise ValueError(f"tool '{name}' not enabled")
                result = str(TOOLS[name][1](**args))
            except Exception as e:  # feed errors back so the model can recover
                result = f"error: {e}"
            trace.append({"tool": name, "args": args, "result": result})
            convo.append({"role": "tool", "tool_name": name, "content": result})
    return {"reply": "Stopped: max_tool_steps reached.", "tool_trace": trace}


# ---------------- API ----------------
app = FastAPI(title="Agent Gateway")


class ChatIn(BaseModel):
    messages: list[dict]


def check_key(auth: str | None):
    env = cfg()["server"]["api_key_env"]
    key = os.environ.get(env, "")
    if not key:
        raise HTTPException(500, f"set env var {env}")
    if not auth or not hmac.compare_digest(auth, f"Bearer {key}"):
        raise HTTPException(401, "bad API key")


@app.post("/chat")
def chat(body: ChatIn, authorization: str | None = Header(None)):
    check_key(authorization)
    try:
        return run_agent(body.messages)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"backend error: {e}")


@app.get("/config")
def get_config(authorization: str | None = Header(None)):
    check_key(authorization)
    c = cfg()
    return {"backend": c["backend"], "model": c["model"], "agent": c["agent"],
            "tools_available": list(TOOLS)}


@app.get("/")
def root():
    return {"service": "Agent Gateway", "status": "running",
            "endpoints": {"GET /health": "liveness check",
                          "GET /config": "current settings (API key required)",
                          "POST /chat": "talk to the agent (API key required)",
                          "GET /docs": "interactive API docs"}}


@app.get("/health")
def health():
    return {"ok": True}


if __name__ == "__main__":
    s = cfg()["server"]
    tls = {}
    if s.get("tls_enabled"):
        tls = {"ssl_certfile": s["tls_cert"], "ssl_keyfile": s["tls_key"]}
    elif s["host"] not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: TLS is off and the server is reachable from the network. "
              "Traffic and the API key are sent in clear text.")
    uvicorn.run(app, host=s["host"], port=s["port"], **tls)