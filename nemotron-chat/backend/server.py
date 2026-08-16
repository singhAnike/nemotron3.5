"""
Nemotron Chat — backend server.

Holds the NVIDIA API key server-side (never sent to the browser), streams
chat completions to the frontend, and extracts text/images from uploaded
files so they can be added as context to the conversation.

Run:
    export NVIDIA_API_KEY="nvapi-..."
    pip install -r requirements.txt
    uvicorn server:app --host 0.0.0.0 --port 8000

Then open http://localhost:8000 in a browser.
"""
import base64
import io
import json
import os
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

API_KEY = os.environ.get("NVIDIA_API_KEY")
if not API_KEY:
    raise RuntimeError(
        "NVIDIA_API_KEY environment variable is not set. "
        "Run: export NVIDIA_API_KEY='nvapi-...' before starting the server."
    )

MODEL = os.environ.get("NEMOTRON_MODEL", "nvidia/nemotron-3.5-lightning-30b-a3b")

client = OpenAI(base_url="https://integrate.api.nvidia.com/v1", api_key=API_KEY)

app = FastAPI(title="Nemotron Chat")

# ---------------------------------------------------------------------------
# Chat endpoint (streaming)
# ---------------------------------------------------------------------------


class ChatMessage(BaseModel):
    role: str
    content: object  # str, or list of content parts (for text + image)


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    temperature: float = 0.7
    top_p: float = 0.95
    max_tokens: int = 4096


def sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


@app.post("/api/chat")
def chat(req: ChatRequest):
    messages = [{"role": m.role, "content": m.content} for m in req.messages]

    def event_stream():
        try:
            completion = client.chat.completions.create(
                model=MODEL,
                messages=messages,
                temperature=req.temperature,
                top_p=req.top_p,
                max_tokens=req.max_tokens,
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                stream=True,
            )
            for chunk in completion:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                content = getattr(delta, "content", None)
                if content:
                    yield sse({"type": "token", "content": content})
            yield sse({"type": "done"})
        except Exception as e:  # surfaces model/API errors to the UI instead of hanging
            yield sse({"type": "error", "message": str(e)})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Upload endpoint — extracts text from docs, base64-encodes images
# ---------------------------------------------------------------------------

TEXT_EXTS = {".txt", ".md", ".csv", ".json", ".py", ".js", ".html", ".css"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

MAX_EXTRACTED_CHARS = 20000  # guard against blowing the context window


def extract_pdf_text(raw: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(raw))
    parts = []
    for page in reader.pages:
        parts.append(page.extract_text() or "")
    return "\n".join(parts)


def extract_docx_text(raw: bytes) -> str:
    import docx

    doc = docx.Document(io.BytesIO(raw))
    return "\n".join(p.text for p in doc.paragraphs)


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    ext = Path(file.filename).suffix.lower()
    raw = await file.read()

    if ext in IMAGE_EXTS:
        b64 = base64.b64encode(raw).decode("utf-8")
        data_url = f"data:{IMAGE_MIME[ext]};base64,{b64}"
        return {"kind": "image", "filename": file.filename, "data_url": data_url}

    try:
        if ext == ".pdf":
            text = extract_pdf_text(raw)
        elif ext == ".docx":
            text = extract_docx_text(raw)
        elif ext in TEXT_EXTS:
            text = raw.decode("utf-8", errors="replace")
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file type '{ext}'. Supported: PDF, DOCX, TXT, MD, CSV, JSON, and common code/image files.",
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read file: {e}")

    truncated = False
    if len(text) > MAX_EXTRACTED_CHARS:
        text = text[:MAX_EXTRACTED_CHARS]
        truncated = True

    return {
        "kind": "text",
        "filename": file.filename,
        "text": text,
        "truncated": truncated,
    }


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
