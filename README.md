# A.U.R.A

A.U.R.A, short for Adaptive Universal Responsive Agent, is a local AI assistant with a FastAPI backend, a React/Vite HUD, voice input and output, deterministic tool routing, browser controls, weather/news utilities, persistent memory hooks, and vision-aware UI modes.

The project keeps the Python service and web HUD in one compact repository. Runtime secrets, diagnostics, generated reports, model caches, build caches, and local memory files are intentionally excluded from Git.

## What It Includes

- `jarvis/` - Python backend, voice pipeline, tool routing, diagnostics, memory, browser helpers, and static app serving.
- `jarvis/web/` - React + Vite frontend for the A.U.R.A orb console, diagnostics, mobile remote control, voice runtime, and vision components.
- `scripts/` - setup and local validation helpers.
- `tests/` - pytest coverage for routing, memory, weather, voice, parser, and task behavior.
- `whatsapp-mcp/` - optional WhatsApp MCP bridge source.
- `docs/img/` - lightweight visual assets used by the project.

## Quick Start

```powershell
git clone https://github.com/eswarb-dev/AURA.git
cd AURA
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
cd jarvis\web
npm install
npm run build
cd ..\..
.\start.ps1
```

Open the HUD at `http://127.0.0.1:8765/`.

## Development

Backend checks:

```powershell
python -m pytest
```

Frontend checks:

```powershell
cd jarvis\web
npm run build
```

## Configuration

Copy `.env.example` for local settings and keep real secrets out of the repository. The runtime still uses compatibility names such as `JARVIS_*` in several places, even when the visible product name is A.U.R.A.

## Safety

A.U.R.A can control local tools when enabled. Keep it bound to localhost, review any destructive command paths before use, and do not commit private profiles, diagnostics, provider keys, or local memory data.
