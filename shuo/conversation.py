"""
The main event loops for shuo.

Both transports run the same pure state machine:
    event -> process_event -> actions

Current transport variants:
- Twilio Media Streams (`/ws`)
- Realtime browser/mobile WebSocket (`/ws/realtime`)
"""

import json
import asyncio
from typing import Optional

from fastapi import WebSocket

from .types import (
    AppState,
    Event, StreamStartEvent, StreamStopEvent, MediaEvent,
    FluxStartOfTurnEvent, FluxEndOfTurnEvent, AgentTurnDoneEvent,
    FeedFluxAction, StartAgentTurnAction, ResetAgentTurnAction,
    Phase,
)
from .state import process_event
from .services.flux import FluxService
from .services.tts_pool import TTSPool
from .services.twilio_client import parse_twilio_message
from .agent import Agent
from .tracer import Tracer
from .log import Logger, get_logger
from .realtime.protocol import (
    ProtocolError,
    SessionStart,
    SessionStop,
    Ping,
    parse_control_message,
    validate_audio_chunk,
    message_session_ready,
    message_turn_state,
    message_transcript_final,
    message_pong,
    message_error,
)

logger = get_logger("shuo.conversation")


async def _cancel_reader(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def run_conversation_over_twilio(websocket: WebSocket) -> None:
    """Main event loop for a Twilio call."""
    event_log = Logger(verbose=False)
    event_queue: asyncio.Queue[Event] = asyncio.Queue()
    tracer = Tracer()

    agent: Optional[Agent] = None
    tts_pool = TTSPool(pool_size=1, ttl=8.0)
    stream_sid: Optional[str] = None

    async def on_flux_end_of_turn(transcript: str) -> None:
        await event_queue.put(FluxEndOfTurnEvent(transcript=transcript))

    async def on_flux_start_of_turn() -> None:
        await event_queue.put(FluxStartOfTurnEvent())

    flux = FluxService(
        on_end_of_turn=on_flux_end_of_turn,
        on_start_of_turn=on_flux_start_of_turn,
    )

    async def read_twilio() -> None:
        """Background task to read Twilio websocket messages."""
        try:
            while True:
                raw = await websocket.receive_text()
                data = json.loads(raw)
                event = parse_twilio_message(data)
                if event:
                    await event_queue.put(event)
                    if isinstance(event, StreamStopEvent):
                        break
        except Exception as e:
            event_log.error("Twilio reader", e)
            await event_queue.put(StreamStopEvent())

    state = AppState()
    reader_task = asyncio.create_task(read_twilio())

    try:
        while True:
            event = await event_queue.get()
            event_log.event(event)

            if isinstance(event, StreamStartEvent):
                stream_sid = event.stream_sid
                await flux.start()
                await tts_pool.start()
                agent = Agent(
                    websocket=websocket,
                    stream_sid=event.stream_sid,
                    on_done=lambda: event_queue.put_nowait(AgentTurnDoneEvent()),
                    tts_pool=tts_pool,
                    tracer=tracer,
                    transport="twilio",
                )

            old_phase = state.phase
            state, actions = process_event(state, event)
            event_log.transition(old_phase, state.phase)

            for action in actions:
                event_log.action(action)
                if isinstance(action, FeedFluxAction):
                    await flux.send(action.audio_bytes)

                elif isinstance(action, StartAgentTurnAction):
                    if agent:
                        await agent.start_turn(action.transcript)

                elif isinstance(action, ResetAgentTurnAction):
                    if agent:
                        await agent.cancel_turn()

            if isinstance(event, StreamStopEvent):
                break

    except Exception as e:
        event_log.error("Call loop", e)
        raise

    finally:
        await _cancel_reader(reader_task)

        if agent:
            await agent.cleanup()

        await tts_pool.stop()
        await flux.stop()

        call_id = stream_sid or "unknown"
        tracer.save(call_id)

        Logger.websocket_disconnected()


async def run_conversation_over_realtime(websocket: WebSocket, session_id: str) -> None:
    """Main event loop for browser/mobile realtime sessions."""
    event_log = Logger(verbose=False)
    event_queue: asyncio.Queue[Event] = asyncio.Queue()
    tracer = Tracer()

    agent: Optional[Agent] = None
    tts_pool = TTSPool(pool_size=1, ttl=8.0)
    stream_sid: Optional[str] = None

    async def on_flux_end_of_turn(transcript: str) -> None:
        await event_queue.put(FluxEndOfTurnEvent(transcript=transcript))

    async def on_flux_start_of_turn() -> None:
        await event_queue.put(FluxStartOfTurnEvent())

    flux = FluxService(
        on_end_of_turn=on_flux_end_of_turn,
        on_start_of_turn=on_flux_start_of_turn,
        encoding="linear16",
        sample_rate=16000,
    )

    async def _send(payload: dict) -> None:
        await websocket.send_text(json.dumps(payload))

    async def read_realtime() -> None:
        """Background task to read realtime websocket frames."""
        started = False

        try:
            while True:
                packet = await websocket.receive()
                packet_type = packet.get("type")

                if packet_type == "websocket.disconnect":
                    await event_queue.put(StreamStopEvent())
                    break

                text = packet.get("text")
                if text is not None:
                    try:
                        control = parse_control_message(text)
                    except ProtocolError as e:
                        await _send(message_error("bad_message", str(e)))
                        continue

                    if isinstance(control, Ping):
                        await _send(message_pong())
                        continue

                    if isinstance(control, SessionStart):
                        if started:
                            await _send(message_error("bad_state", "session already started"))
                            continue

                        started = True
                        await _send(message_session_ready(session_id))
                        await event_queue.put(StreamStartEvent(stream_sid=session_id))
                        continue

                    if isinstance(control, SessionStop):
                        await event_queue.put(StreamStopEvent())
                        break

                audio = packet.get("bytes")
                if audio is not None:
                    if not started:
                        await _send(message_error("bad_state", "send session.start before audio"))
                        continue

                    try:
                        validate_audio_chunk(audio)
                    except ProtocolError as e:
                        await _send(message_error("bad_audio", str(e)))
                        continue

                    await event_queue.put(MediaEvent(audio_bytes=audio))

        except Exception as e:
            event_log.error("Realtime reader", e)
            await event_queue.put(StreamStopEvent())

    state = AppState()
    reader_task = asyncio.create_task(read_realtime())

    try:
        while True:
            event = await event_queue.get()
            event_log.event(event)

            if isinstance(event, StreamStartEvent):
                stream_sid = event.stream_sid
                await flux.start()
                await tts_pool.start()
                agent = Agent(
                    websocket=websocket,
                    stream_sid=event.stream_sid,
                    on_done=lambda: event_queue.put_nowait(AgentTurnDoneEvent()),
                    tts_pool=tts_pool,
                    tracer=tracer,
                    transport="realtime",
                )
                await _send(message_turn_state("listening"))

            if isinstance(event, FluxEndOfTurnEvent) and event.transcript:
                await _send(message_transcript_final(event.transcript))

            old_phase = state.phase
            state, actions = process_event(state, event)
            event_log.transition(old_phase, state.phase)

            if old_phase != state.phase:
                phase_name = "responding" if state.phase == Phase.RESPONDING else "listening"
                await _send(message_turn_state(phase_name))

            for action in actions:
                event_log.action(action)
                if isinstance(action, FeedFluxAction):
                    await flux.send(action.audio_bytes)

                elif isinstance(action, StartAgentTurnAction):
                    if agent:
                        await agent.start_turn(action.transcript)

                elif isinstance(action, ResetAgentTurnAction):
                    if agent:
                        await agent.cancel_turn()

            if isinstance(event, StreamStopEvent):
                break

    except Exception as e:
        event_log.error("Realtime loop", e)
        raise

    finally:
        await _cancel_reader(reader_task)

        if agent:
            await agent.cleanup()

        await tts_pool.stop()
        await flux.stop()

        call_id = stream_sid or session_id
        tracer.save(call_id)

        Logger.websocket_disconnected()
