"""Realtime websocket message protocol helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Union

UPLINK_CODEC = "pcm_s16le"
UPLINK_SAMPLE_RATE_HZ = 16000
DOWNLINK_CODEC = "mulaw_8000"
PCM_FRAME_SAMPLES = 320  # 20ms @ 16k
PCM_FRAME_BYTES = PCM_FRAME_SAMPLES * 2
MAX_AUDIO_CHUNK_BYTES = PCM_FRAME_BYTES * 10


class ProtocolError(ValueError):
    """Raised when a realtime websocket message is invalid."""


@dataclass(frozen=True)
class SessionStart:
    codec: str
    sample_rate_hz: int


@dataclass(frozen=True)
class SessionStop:
    pass


@dataclass(frozen=True)
class Ping:
    pass


ControlMessage = Union[SessionStart, SessionStop, Ping]


def parse_control_message(raw: str) -> ControlMessage:
    """Parse a JSON control message from the realtime client."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProtocolError("control message must be valid JSON") from exc

    if not isinstance(data, dict):
        raise ProtocolError("control message must be a JSON object")

    msg_type = data.get("type")

    if msg_type == "session.start":
        codec = str(data.get("codec", ""))
        sample_rate_hz = int(data.get("sample_rate_hz", 0))

        if codec != UPLINK_CODEC:
            raise ProtocolError(f"unsupported codec: {codec}")
        if sample_rate_hz != UPLINK_SAMPLE_RATE_HZ:
            raise ProtocolError(f"unsupported sample_rate_hz: {sample_rate_hz}")

        return SessionStart(codec=codec, sample_rate_hz=sample_rate_hz)

    if msg_type == "session.stop":
        return SessionStop()

    if msg_type == "ping":
        return Ping()

    raise ProtocolError(f"unknown control message type: {msg_type}")


def validate_audio_chunk(chunk: bytes) -> None:
    """Validate binary PCM16LE audio chunk constraints."""
    if not chunk:
        raise ProtocolError("audio chunk is empty")
    if len(chunk) % 2 != 0:
        raise ProtocolError("audio chunk must be int16-aligned")
    if len(chunk) > MAX_AUDIO_CHUNK_BYTES:
        raise ProtocolError("audio chunk too large")


def message_session_ready(session_id: str) -> dict:
    return {
        "type": "session.ready",
        "session_id": session_id,
        "codec": UPLINK_CODEC,
        "sample_rate_hz": UPLINK_SAMPLE_RATE_HZ,
    }


def message_turn_state(state: str) -> dict:
    return {
        "type": "turn.state",
        "state": state,
    }


def message_transcript_final(text: str) -> dict:
    return {
        "type": "transcript.final",
        "text": text,
    }


def message_audio_chunk(audio_b64: str) -> dict:
    return {
        "type": "audio.chunk",
        "codec": DOWNLINK_CODEC,
        "audio_b64": audio_b64,
    }


def message_audio_clear() -> dict:
    return {
        "type": "audio.clear",
    }


def message_error(code: str, message: str) -> dict:
    return {
        "type": "error",
        "code": code,
        "message": message,
    }


def message_pong() -> dict:
    return {
        "type": "pong",
    }
