"""
Audio player for streaming audio to client transports.

Manages its own independent playback loop that drips audio
chunks at the correct rate, regardless of other activity.
"""

import json
import asyncio
from typing import List, Optional, Callable, Awaitable

from fastapi import WebSocket

from ..log import ServiceLogger
from ..realtime.protocol import message_audio_chunk, message_audio_clear

log = ServiceLogger("Player")


class AudioPlayer:
    """
    Streams audio to a transport at the correct rate.

    Features:
    - Independent playback loop (not affected by incoming messages)
    - Can be topped up with audio chunks dynamically (for streaming TTS)
    - Instant stop and clear on interrupt
    - Callback when playback completes
    """

    def __init__(
        self,
        send_audio: Callable[[str], Awaitable[None]],
        send_clear: Callable[[], Awaitable[None]],
        on_done: Optional[Callable[[], None]] = None,
    ):
        self._send_audio_fn = send_audio
        self._send_clear_fn = send_clear
        self._on_done = on_done

        self._chunks: List[str] = []
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._index = 0
        self._tts_done = False

    @classmethod
    def for_twilio(
        cls,
        websocket: WebSocket,
        stream_sid: str,
        on_done: Optional[Callable[[], None]] = None,
    ) -> "AudioPlayer":
        """Build an AudioPlayer that writes Twilio media stream messages."""

        async def _send_audio(payload: str) -> None:
            await websocket.send_text(json.dumps({
                "event": "media",
                "streamSid": stream_sid,
                "media": {"payload": payload},
            }))

        async def _send_clear() -> None:
            await websocket.send_text(json.dumps({
                "event": "clear",
                "streamSid": stream_sid,
            }))

        return cls(send_audio=_send_audio, send_clear=_send_clear, on_done=on_done)

    @classmethod
    def for_realtime(
        cls,
        websocket: WebSocket,
        on_done: Optional[Callable[[], None]] = None,
    ) -> "AudioPlayer":
        """Build an AudioPlayer that writes realtime protocol messages."""

        async def _send_audio(payload: str) -> None:
            await websocket.send_text(json.dumps(message_audio_chunk(payload)))

        async def _send_clear() -> None:
            await websocket.send_text(json.dumps(message_audio_clear()))

        return cls(send_audio=_send_audio, send_clear=_send_clear, on_done=on_done)

    @property
    def is_playing(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Start the playback loop."""
        if self.is_playing:
            await self.stop_and_clear()

        self._chunks = []
        self._index = 0
        self._running = True
        self._tts_done = False

        self._task = asyncio.create_task(self._playback_loop())

    async def send_chunk(self, chunk: str) -> None:
        """Add an audio chunk to the playback queue."""
        if not self._running:
            await self.start()

        self._chunks.append(chunk)

    def mark_tts_done(self) -> None:
        """Signal that TTS is complete - no more chunks coming."""
        self._tts_done = True

    async def play(self, chunks: List[str]) -> None:
        """Start playing a fixed list of audio chunks (legacy mode)."""
        if self.is_playing:
            await self.stop_and_clear()

        self._chunks = list(chunks)
        self._index = 0
        self._running = True
        self._tts_done = True

        self._task = asyncio.create_task(self._playback_loop())

    async def stop_and_clear(self) -> None:
        """Stop playback immediately and clear transport audio buffer."""
        self._running = False

        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        self._task = None
        self._chunks = []
        self._index = 0
        self._tts_done = False

        await self._send_clear()

    async def wait_until_done(self) -> None:
        """Wait for playback to complete (or be interrupted)."""
        if self._task:
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _playback_loop(self) -> None:
        """Independent loop that drips audio at ~20ms intervals."""
        try:
            while self._running:
                if self._index < len(self._chunks):
                    chunk = self._chunks[self._index]
                    await self._send_audio(chunk)
                    self._index += 1
                    await asyncio.sleep(0.020)

                elif self._tts_done:
                    break
                else:
                    await asyncio.sleep(0.010)

            if self._running:
                self._running = False
                if self._on_done:
                    self._on_done()

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("Playback failed", e)
            self._running = False

    async def _send_audio(self, payload: str) -> None:
        """Send a single audio chunk to the configured transport."""
        await self._send_audio_fn(payload)

    async def _send_clear(self) -> None:
        """Send clear message to flush downstream audio buffer."""
        await self._send_clear_fn()
