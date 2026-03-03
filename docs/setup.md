# Setup Guide (Phone + Web + Mobile)

This project supports two runtime paths:
- Twilio phone calls (`/twiml` + `/ws`)
- Browser/mobile realtime audio (`/v1/realtime/sessions` + `/ws/realtime`)

## 1) Prerequisites

- Python 3.9+
- `pip`
- ngrok (for Twilio webhooks on local dev)
- API keys/accounts:
  - Deepgram (Flux STT)
  - ElevenLabs (TTS)
  - Groq (default live LLM inference)
  - Twilio (only for phone call mode)
  - OpenAI (optional, only for `/bench/ttft`)

## 2) Install and configure

```bash
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:

### Required for realtime web/mobile
- `DEEPGRAM_API_KEY`
- `GROQ_API_KEY`
- `ELEVENLABS_API_KEY`
- `REALTIME_SESSION_SECRET` (long random string)

### Required for Twilio phone mode
- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_PHONE_NUMBER`
- `TWILIO_PUBLIC_URL` (your public ngrok/host URL)

### Inference model settings
- `LLM_MODEL`: defaults to `llama-3.3-70b-versatile`
- Live call/web inference currently uses `GROQ_API_KEY` + `LLM_MODEL`
- `OPENAI_API_KEY` is optional and used by `GET /bench/ttft`

## 3) Run locally

```bash
python main.py
```

App listens on `http://localhost:3040` by default.

## 4) Browser microphone demo

1. Open `http://localhost:3040/web`
2. Click **Start Session**
3. Allow microphone permissions

The page mints an ephemeral token via `POST /v1/realtime/sessions`, then streams audio over `WS /ws/realtime`.

## 5) Twilio phone mode

In another terminal:

```bash
ngrok http 3040
```

Put ngrok HTTPS URL into `TWILIO_PUBLIC_URL`, then run:

```bash
python main.py +15551234567
```

## 6) Mobile app integration

Use the same realtime flow as web:
1. `POST /v1/realtime/sessions`
2. Connect `ws_url`
3. Send `session.start` + binary PCM16 16k chunks
4. Receive `audio.chunk` (`mulaw_8000`) and `turn.state`

Wire format details: `docs/realtime-protocol.md`.

## 7) Test and validation

```bash
python -m pytest tests/ -v
```

If tests fail because dependencies are missing, reinstall from `requirements.txt` in an environment with internet/package access.
