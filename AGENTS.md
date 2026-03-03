# Repository Guidelines

## Project Structure & Module Organization
Core runtime code lives in `shuo/`. Keep orchestration in `conversation.py`, pure state transitions in `state.py`, immutable types/events/actions in `types.py`, and API endpoints in `server.py`. External integrations belong under `shuo/services/` (Twilio, STT, LLM, TTS, playback).

Entrypoint is `main.py` (`server-only` or outbound-call mode). Tests are in `tests/` (currently focused on the state machine). Utility and benchmarking scripts live in `scripts/`. Reference docs are in `docs/`; generated visuals (`*.png`) are repo artifacts, not source modules.

## Build, Test, and Development Commands
- `python -m venv .venv && source .venv/bin/activate`: create local virtualenv.
- `pip install -r requirements.txt`: install app and test dependencies.
- `cp .env.example .env`: create local config (fill real keys before running).
- `python main.py`: run server for inbound calls.
- `python main.py +15551234567`: run server and place an outbound test call.
- `python -m pytest tests/ -v`: run unit tests.
- `python scripts/service_map.py --save service_map.png`: regenerate service topology graphic.

## Coding Style & Naming Conventions
Use Python 3.9+ style with 4-space indentation and PEP 8 spacing. Follow existing naming patterns: `snake_case` for modules/functions/variables, `PascalCase` for classes and event/action types. Prefer explicit type hints on public functions and dataclass-based state objects.

Keep side effects out of `state.py`: state logic should stay pure and testable, while network/audio I/O stays in `services/` and `agent.py`. Group imports in stdlib/third-party/local order.

## Testing Guidelines
Use `pytest` (with `pytest-asyncio` available when needed). Name tests `test_<behavior>` and organize by feature with class grouping, as in `tests/test_update.py`. Add regression tests for every state-transition or interruption bug fix. Keep tests deterministic and free of real network/API calls.

## Commit & Pull Request Guidelines
Recent history favors short, imperative commit subjects (for example, `Add .env.example...`, `Refine README...`, `Fix reasoning`). Keep commits focused to one concern.

PRs should include:
- What changed and why.
- Any env/config changes (`.env.example`, required vars, ports, webhook URL assumptions).
- Test evidence (`python -m pytest tests/ -v`) and relevant runtime logs for call-flow changes.
