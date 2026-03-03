# shuo 说

A voice agent framework in ~600 lines of Python.

```bash
python main.py +1234567890
```

```
🚀 Server starting on port 3040
✓  Ready https://mature-spaniel-physically.ngrok-free.app
📞 Calling +1234567890...
✓  Call initiated SID: CA094f2e...
🔌 WebSocket connected
▶  Stream started SID: MZ8a3b1f...
← Flux EndOfTurn "Hey, how's it going?"
◆ LISTENING → RESPONDING
→ Start Agent "Hey, how's it going?"
← Agent turn done
◆ RESPONDING → LISTENING
```

## How it works

Two abstractions, one pure function:

- **Deepgram Flux** — always-on STT + turn detection over a single WebSocket
- **Agent** — self-contained LLM → TTS → Player pipeline, owns conversation history
- **`process_event(state, event) → (state, actions)`** — the entire state machine in ~30 lines

Everything streams. LLM tokens feed TTS immediately, then audio streams to Twilio callers or realtime web/mobile websocket clients. If you interrupt (barge-in), the agent cancels everything and clears the downstream audio buffer instantly.

```
LISTENING ──EndOfTurn──→ RESPONDING ──Done──→ LISTENING
    ↑                        │
    └────StartOfTurn─────────┘  (barge-in)
```

## Project structure

```
shuo/
  types.py              # Immutable state, events, actions
  state.py              # Pure state machine (~30 lines)
  conversation.py       # Main event loop
  agent.py              # LLM → TTS → Player pipeline
  log.py                # Colored logging
  server.py             # FastAPI endpoints
  realtime/
    auth.py             # Ephemeral token mint/verify
    protocol.py         # Realtime websocket message schema
  static/web/           # Browser mic demo client
  services/
    flux.py             # Deepgram Flux (STT + turns)
    llm.py              # OpenAI GPT-4o-mini streaming
    tts.py              # ElevenLabs WebSocket streaming
    tts_pool.py         # TTS connection pool (warm spares)
    player.py           # Audio playback to Twilio
    twilio_client.py    # Outbound calls + message parsing
```

## Setup

Requires Python 3.9+, [ngrok](https://ngrok.com/), and API keys for Deepgram, Groq, and ElevenLabs (plus Twilio for phone mode). OpenAI is optional for benchmarking endpoints.

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in your keys
ngrok http 3040        # in another terminal
python main.py +1234567890
```

For full environment variable guidance (including inference keys and realtime auth), see `docs/setup.md`.

## Browser realtime demo

Start the server, open `http://localhost:3040/web`, and click **Start Session**.

```bash
python main.py
```

The demo flow:
1. `POST /v1/realtime/sessions` mints an ephemeral token.
2. Browser connects to `GET /ws/realtime?token=...`.
3. Browser sends PCM16 mic audio (16kHz), server returns streamed μ-law audio chunks.

Mobile apps can use the same `/v1/realtime/sessions` + `/ws/realtime` protocol. See `docs/realtime-protocol.md`.

## Tests

```bash
python -m pytest tests/ -v   # runs in ~0.03s
```

## License

MIT
