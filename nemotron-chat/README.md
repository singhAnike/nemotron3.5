# Nemotron Chat

A self-hosted chat UI for NVIDIA's Nemotron models — streaming responses,
markdown/code rendering, and file/image upload for context, all in a
single small FastAPI + vanilla JS app (no build step).

## 1. Rotate your API key first

The key in your original script was pasted in plain text, which means it's
been exposed. Go to https://build.nvidia.com, revoke that key, and generate
a new one before using it here.

## 2. Set up the backend

```bash
cd backend
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

export NVIDIA_API_KEY="nvapi-your-new-key-here"   # Windows (PowerShell): $env:NVIDIA_API_KEY="..."

uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```

## 3. Open the app

Go to **http://localhost:8000** — the FastAPI server serves the frontend
directly, so there's nothing else to run.

## How it works

- `backend/server.py` holds your API key server-side and never sends it to
  the browser. It exposes two endpoints:
  - `POST /api/chat` — streams the model's reply token-by-token using
    server-sent events.
  - `POST /api/upload` — accepts a file. Images are base64-encoded and
    passed to the model as multimodal content; PDFs/DOCX/text files have
    their text extracted and added as context to your next message.
- `frontend/` is plain HTML/CSS/JS — no build tools. It renders markdown
  and syntax-highlighted code via `marked.js` and `highlight.js` (loaded
  from a CDN).

## Notes

- **Image support depends on the model.** Nemotron-3.5-Lightning may or
  may not accept image inputs — if it rejects them, you'll see the error
  surfaced in the chat rather than a silent failure. Text/PDF/DOCX context
  always works, since that's just prepended as text.
- Conversation history lives in the browser tab's memory only — refreshing
  the page clears it. If you want persistent chat history across sessions,
  that's a reasonable next step (e.g. writing history to a local SQLite
  file per session) — say the word if you'd like that added.
- Change the model by setting `NEMOTRON_MODEL` before starting the server,
  or editing the default in `server.py`.
