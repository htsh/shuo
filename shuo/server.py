"""
FastAPI server for shuo.

Endpoints:
- GET /health - Health check
- GET/POST /twiml - TwiML for Twilio media stream
- WebSocket /ws - Twilio media stream endpoint
- POST /v1/realtime/sessions - Mint ephemeral token for web/mobile realtime sessions
- WebSocket /ws/realtime - Realtime browser/mobile audio endpoint
- GET /web - Browser demo client
- GET /trace/latest - Returns the most recent call trace as JSON
- GET /bench/ttft - Benchmark TTFT across OpenAI models
"""

import json
import os
import time
import asyncio
import random
from collections import defaultdict, deque
from pathlib import Path
from typing import Deque, Dict, List, Optional

from fastapi import FastAPI, Query, Request, Response, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from openai import AsyncOpenAI

from .conversation import run_conversation_over_realtime, run_conversation_over_twilio
from .services.twilio_client import make_outbound_call
from .log import get_logger
from .realtime.auth import RealtimeAuthError, mint_session, verify_session_token
from .realtime.protocol import message_error

logger = get_logger("shuo.server")

app = FastAPI(title="shuo", docs_url=None, redoc_url=None)

STATIC_DIR = Path(__file__).resolve().parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# - Graceful shutdown / connection draining ------------------------------------
_draining = False          # Set True on SIGTERM - reject new calls/sessions
_active_calls = 0          # Count of live websocket conversations
_drain_event = asyncio.Event()  # Signalled when _active_calls hits 0

# - Realtime guardrails --------------------------------------------------------
_session_mints_by_ip: Dict[str, Deque[float]] = defaultdict(deque)
_realtime_active_by_ip: Dict[str, int] = defaultdict(int)


def _client_ip_from_request(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _client_ip_from_websocket(websocket: WebSocket) -> str:
    forwarded = websocket.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    if websocket.client and websocket.client.host:
        return websocket.client.host
    return "unknown"


def _allowed_origins() -> List[str]:
    raw = os.getenv(
        "REALTIME_ALLOWED_ORIGINS",
        "http://localhost:3040,http://127.0.0.1:3040",
    )
    return [x.strip() for x in raw.split(",") if x.strip()]


def _origin_allowed(origin: str) -> bool:
    # Non-browser clients usually omit Origin; allow them.
    if not origin:
        return True

    allowed = _allowed_origins()
    return "*" in allowed or origin in allowed


def _mint_limit_per_minute() -> int:
    raw = os.getenv("REALTIME_MAX_SESSION_MINTS_PER_MIN", "30")
    try:
        value = int(raw)
    except ValueError:
        value = 30
    return max(1, min(value, 1000))


def _max_sessions_per_ip() -> int:
    raw = os.getenv("REALTIME_MAX_SESSIONS_PER_IP", "3")
    try:
        value = int(raw)
    except ValueError:
        value = 3
    return max(1, min(value, 100))


def _allow_new_session_mint(ip: str) -> bool:
    now = time.monotonic()
    bucket = _session_mints_by_ip[ip]

    while bucket and (now - bucket[0]) > 60:
        bucket.popleft()

    if len(bucket) >= _mint_limit_per_minute():
        return False

    bucket.append(now)
    return True


def _realtime_ws_url(request: Request, token: str) -> str:
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)

    if host:
        ws_scheme = "wss" if scheme == "https" else "ws"
        return f"{ws_scheme}://{host}/ws/realtime?token={token}"

    return f"/ws/realtime?token={token}"


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok"}


@app.get("/web")
async def web_client():
    """Serve the browser mic demo."""
    index_path = STATIC_DIR / "web" / "index.html"
    if not index_path.exists():
        return JSONResponse({"error": "web client not found"}, status_code=404)
    return FileResponse(index_path)


@app.post("/v1/realtime/sessions")
async def create_realtime_session(request: Request):
    """Mint a short-lived one-time token for realtime websocket access."""
    origin = request.headers.get("origin", "").strip()
    ip = _client_ip_from_request(request)

    if not _origin_allowed(origin):
        return JSONResponse({"error": "origin not allowed"}, status_code=403)

    if not _allow_new_session_mint(ip):
        return JSONResponse({"error": "rate limit exceeded"}, status_code=429)

    try:
        token, claims = mint_session(origin=origin or None)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    return JSONResponse(
        {
            "token": token,
            "session_id": claims.session_id,
            "expires_at": claims.expires_at,
            "ws_url": _realtime_ws_url(request, token),
        },
        status_code=201,
    )


@app.api_route("/twiml", methods=["GET", "POST"])
async def twiml():
    """
    Return TwiML instructing Twilio to connect a websocket stream.

    Twilio calls this URL when the call is answered.
    During graceful shutdown, rejects new calls so they don't get cut off.
    """
    if _draining:
        logger.info("Draining - rejecting new inbound call")
        reject_twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Sorry, we are updating. Please call back in a moment.</Say>
    <Hangup/>
</Response>"""
        return Response(content=reject_twiml, media_type="application/xml")

    public_url = os.getenv("TWILIO_PUBLIC_URL", "")
    ws_url = public_url.replace("https://", "wss://").replace("http://", "ws://")
    ws_url = f"{ws_url}/ws"

    twiml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect record="record-from-answer-dual">
        <Stream url="{ws_url}" track="inbound_track" />
    </Connect>
</Response>"""

    return Response(content=twiml_response, media_type="application/xml")


@app.get("/trace/latest")
async def latest_trace():
    """Return the most recent call trace as JSON."""
    trace_dir = Path("/tmp/shuo")
    if not trace_dir.exists():
        return JSONResponse({"error": "No traces found"}, status_code=404)

    traces = sorted(trace_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not traces:
        return JSONResponse({"error": "No traces found"}, status_code=404)

    data = json.loads(traces[0].read_text())
    return JSONResponse(data)


@app.get("/call/{phone_number:path}")
async def trigger_call(phone_number: str):
    """
    Initiate an outbound call.

    Usage:
        curl https://your-server/call/+1234567890
    """
    if not phone_number.startswith("+"):
        phone_number = f"+{phone_number}"
    try:
        call_sid = make_outbound_call(phone_number)
        return {"status": "calling", "to": phone_number, "call_sid": call_sid}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# - TTFT Benchmark --------------------------------------------------------------

BENCH_PROMPT = "Explain how a combustion engine works."

# Each entry: (display_name, provider_key, model_id)
# provider_key is used to look up the right AsyncOpenAI client
DEFAULT_MODELS = [
    # OpenAI 4-series
    ("gpt-4o-mini",   "openai", "gpt-4o-mini"),
    ("gpt-4o",        "openai", "gpt-4o"),
    ("gpt-4.1-nano",  "openai", "gpt-4.1-nano"),
    ("gpt-4.1-mini",  "openai", "gpt-4.1-mini"),
    ("gpt-4.1",       "openai", "gpt-4.1"),
    # OpenAI 5-series
    ("gpt-5-nano",    "openai", "gpt-5-nano"),
    ("gpt-5-mini",    "openai", "gpt-5-mini"),
    ("gpt-5",         "openai", "gpt-5"),
    ("gpt-5.1",       "openai", "gpt-5.1"),
    ("gpt-5.2",       "openai", "gpt-5.2"),
    # Groq
    ("groq/llama-3.3-70b",  "groq", "llama-3.3-70b-versatile"),
    ("groq/llama-3.1-8b",   "groq", "llama-3.1-8b-instant"),
]

BENCH_MESSAGES = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": BENCH_PROMPT},
]


def _make_clients() -> dict:
    """Build provider -> AsyncOpenAI client map."""
    clients = {}
    oai_key = os.getenv("OPENAI_API_KEY", "")
    if oai_key:
        clients["openai"] = AsyncOpenAI(api_key=oai_key)
    groq_key = os.getenv("GROQ_API_KEY", "")
    if groq_key:
        clients["groq"] = AsyncOpenAI(
            api_key=groq_key,
            base_url="https://api.groq.com/openai/v1",
        )
    return clients


async def _measure_ttft(client: AsyncOpenAI, model: str) -> float:
    """
    Single TTFT measurement in milliseconds.

    Opens a streaming completion, records time-to-first-content-token,
    then closes the stream immediately.
    """
    is_new = model.startswith(("gpt-5", "o1", "o3", "o4"))
    token_param = "max_completion_tokens" if is_new else "max_tokens"

    params: dict = {
        "model": model,
        "messages": BENCH_MESSAGES,
        "stream": True,
        token_param: 20,
    }
    if is_new:
        params["extra_body"] = {"reasoning_effort": "none"}
    else:
        params["temperature"] = 0

    t0 = time.perf_counter()
    try:
        stream = await client.chat.completions.create(**params)
    except Exception as e:
        if is_new and "none" in str(e).lower():
            params["extra_body"] = {"reasoning_effort": "minimal"}
            t0 = time.perf_counter()
            stream = await client.chat.completions.create(**params)
        else:
            raise

    async for chunk in stream:
        delta = chunk.choices[0].delta if chunk.choices else None
        if delta and delta.content:
            ttft_ms = (time.perf_counter() - t0) * 1000
            await stream.close()
            return ttft_ms

    return (time.perf_counter() - t0) * 1000


@app.get("/bench/ttft")
async def bench_ttft(
    models: Optional[str] = Query(
        None,
        description="Comma-separated model names. Defaults to a built-in list.",
    ),
    runs: int = Query(30, ge=1, le=100, description="Runs per model"),
):
    """
    Benchmark TTFT across OpenAI-compatible models.

    Usage:
        curl https://your-server/bench/ttft
        curl https://your-server/bench/ttft?models=gpt-4o-mini,gpt-4o&runs=5
    """
    clients = _make_clients()

    if models:
        entries = []
        for m in models.split(","):
            m = m.strip()
            if not m:
                continue
            if m.startswith("groq/"):
                entries.append((m, "groq", m.removeprefix("groq/")))
            else:
                entries.append((m, "openai", m))
        model_entries = entries
    else:
        model_entries = DEFAULT_MODELS

    model_entries = [(name, prov, mid) for name, prov, mid in model_entries if prov in clients]

    schedule = [(name, prov, mid, i) for name, prov, mid in model_entries for i in range(runs)]
    random.shuffle(schedule)

    total = len(schedule)
    names = [name for name, _, _ in model_entries]
    logger.info(f"TTFT benchmark: {len(model_entries)} models x {runs} runs = {total} calls (randomised)")

    times_by_model: dict[str, list[float]] = defaultdict(list)
    errors_by_model: dict[str, list[str]] = defaultdict(list)

    for idx, (name, prov, mid, run_i) in enumerate(schedule, 1):
        try:
            ms = await _measure_ttft(clients[prov], mid)
            times_by_model[name].append(round(ms, 1))
            logger.info(f"  [{idx}/{total}] {name} #{run_i+1} -> {ms:.0f} ms")
        except Exception as e:
            errors_by_model[name].append(f"run {run_i+1}: {e}")
            logger.info(f"  [{idx}/{total}] {name} #{run_i+1} -> ERROR")

    results = []
    for name in names:
        t = times_by_model.get(name, [])
        errs = errors_by_model.get(name, [])
        if not t:
            results.append({"model": name, "error": errs[0] if errs else "no data"})
            logger.info(f"  {name} -> ERROR: {errs[0] if errs else 'no data'}")
            continue

        avg = round(sum(t) / len(t), 1)
        entry: dict = {
            "model": name,
            "runs": len(t),
            "avg_ms": avg,
            "min_ms": min(t),
            "max_ms": max(t),
            "all_ms": t,
        }
        if errs:
            entry["errors"] = errs
        results.append(entry)
        logger.info(f"  {name} -> avg {avg} ms  (min {min(t)}, max {max(t)})")

    return JSONResponse({
        "prompt": BENCH_PROMPT,
        "runs_per_model": runs,
        "results": results,
    })


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Twilio Media Streams websocket endpoint."""
    global _active_calls

    if _draining:
        await websocket.accept()
        await websocket.close(code=1013, reason="server draining")
        return

    await websocket.accept()
    _active_calls += 1
    logger.info(f"Call connected  (active: {_active_calls})")

    try:
        await run_conversation_over_twilio(websocket)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        _active_calls -= 1
        logger.info(f"Call ended  (active: {_active_calls})")
        if _draining and _active_calls <= 0:
            _drain_event.set()


@app.websocket("/ws/realtime")
async def realtime_websocket_endpoint(websocket: WebSocket):
    """Browser/mobile realtime websocket endpoint."""
    global _active_calls

    await websocket.accept()

    if _draining:
        await websocket.send_text(json.dumps(message_error("draining", "server is draining")))
        await websocket.close(code=1013)
        return

    origin = websocket.headers.get("origin", "").strip()
    if not _origin_allowed(origin):
        await websocket.send_text(json.dumps(message_error("forbidden", "origin not allowed")))
        await websocket.close(code=4403)
        return

    token = websocket.query_params.get("token", "")
    try:
        claims = verify_session_token(token, origin=origin or None, consume=True)
    except RuntimeError as e:
        await websocket.send_text(json.dumps(message_error("server_error", str(e))))
        await websocket.close(code=1011)
        return
    except RealtimeAuthError as e:
        await websocket.send_text(json.dumps(message_error("auth_failed", str(e))))
        await websocket.close(code=4401)
        return

    ip = _client_ip_from_websocket(websocket)
    if _realtime_active_by_ip[ip] >= _max_sessions_per_ip():
        await websocket.send_text(json.dumps(message_error("rate_limited", "too many active sessions")))
        await websocket.close(code=4429)
        return

    _realtime_active_by_ip[ip] += 1
    _active_calls += 1
    logger.info(f"Realtime session connected {claims.session_id}  (active: {_active_calls})")

    try:
        await run_conversation_over_realtime(websocket, session_id=claims.session_id)
    except Exception as e:
        logger.error(f"Realtime WebSocket error: {e}")
    finally:
        _active_calls -= 1
        _realtime_active_by_ip[ip] = max(0, _realtime_active_by_ip[ip] - 1)
        if _realtime_active_by_ip[ip] == 0:
            _realtime_active_by_ip.pop(ip, None)

        logger.info(f"Realtime session ended {claims.session_id}  (active: {_active_calls})")
        if _draining and _active_calls <= 0:
            _drain_event.set()
