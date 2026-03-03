# Realtime Protocol (Web/Mobile)

This document describes the browser/mobile transport added on top of shuo's existing Twilio flow.

## Session bootstrap

1. Client calls `POST /v1/realtime/sessions`.
2. Server returns:
   - `token`
   - `session_id`
   - `expires_at` (unix epoch seconds)
   - `ws_url` (contains token query param)
3. Client opens websocket to `ws_url`.
4. First control message must be:

```json
{"type":"session.start","codec":"pcm_s16le","sample_rate_hz":16000}
```

## Client -> server messages

### Control (text JSON)
- `session.start`
- `session.stop`
- `ping`

### Audio (binary)
- Raw PCM16 little-endian frames.
- Mono, 16kHz.
- Recommended chunk size: `320` samples (`640` bytes, 20ms).

## Server -> client messages (text JSON)

- `session.ready`

```json
{"type":"session.ready","session_id":"...","codec":"pcm_s16le","sample_rate_hz":16000}
```

- `turn.state`

```json
{"type":"turn.state","state":"listening"}
```

- `transcript.final`

```json
{"type":"transcript.final","text":"hello there"}
```

- `audio.chunk`

```json
{"type":"audio.chunk","codec":"mulaw_8000","audio_b64":"..."}
```

- `audio.clear`

```json
{"type":"audio.clear"}
```

- `error`

```json
{"type":"error","code":"bad_message","message":"..."}
```

## Security and guardrails

- Tokens are signed with `REALTIME_SESSION_SECRET` and short-lived (`REALTIME_TOKEN_TTL_SECONDS`).
- Tokens are one-time use (replay-protected).
- Origin checks use `REALTIME_ALLOWED_ORIGINS`.
- Rate controls:
  - `REALTIME_MAX_SESSION_MINTS_PER_MIN`
  - `REALTIME_MAX_SESSIONS_PER_IP`
