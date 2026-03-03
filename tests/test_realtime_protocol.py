import json

import pytest

from shuo.realtime.protocol import (
    MAX_AUDIO_CHUNK_BYTES,
    ProtocolError,
    SessionStart,
    parse_control_message,
    validate_audio_chunk,
)


def test_parse_session_start_message():
    msg = parse_control_message(
        json.dumps({
            "type": "session.start",
            "codec": "pcm_s16le",
            "sample_rate_hz": 16000,
        })
    )

    assert isinstance(msg, SessionStart)
    assert msg.codec == "pcm_s16le"
    assert msg.sample_rate_hz == 16000


def test_parse_unknown_message_type_raises():
    with pytest.raises(ProtocolError):
        parse_control_message('{"type":"wat"}')


def test_parse_unsupported_codec_raises():
    with pytest.raises(ProtocolError):
        parse_control_message('{"type":"session.start","codec":"opus","sample_rate_hz":16000}')


def test_validate_audio_chunk_requires_int16_alignment():
    with pytest.raises(ProtocolError):
        validate_audio_chunk(b"\x00")


def test_validate_audio_chunk_rejects_large_packets():
    with pytest.raises(ProtocolError):
        validate_audio_chunk(b"\x00" * (MAX_AUDIO_CHUNK_BYTES + 2))
