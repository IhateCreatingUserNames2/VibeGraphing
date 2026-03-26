"""
╔══════════════════════════════════════════════════════════════╗
║         MAS CREATOR — Vibe Graphing Universal Agent          ║
║  Cria qualquer coisa via pipeline Multi-Agent System         ║
║  Backend: FastAPI + OpenRouter                               ║
╚══════════════════════════════════════════════════════════════╝
"""

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
import os

from app.api.routes import router

app = FastAPI(
    title="MAS Creator — Vibe Graphing",
    description="Universal Multi-Agent System que cria qualquer coisa a partir de linguagem natural",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# API routes
app.include_router(router, prefix="/api")

# Serve frontend (arquivo único, sem build)
FRONTEND_PATH = os.path.join(os.path.dirname(__file__), "frontend", "index.html")

@app.get("/", include_in_schema=False)
async def serve_frontend():
    if os.path.exists(FRONTEND_PATH):
        return FileResponse(FRONTEND_PATH, media_type="text/html")
    return HTMLResponse("<h1>Frontend não encontrado</h1>", 404)


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8009, reload=True)
