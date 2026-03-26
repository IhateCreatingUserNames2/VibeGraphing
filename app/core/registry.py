"""
Registry persistente em memória para:
- Custom Tools (criadas pelo usuário)
- Arquivos RAG (upload + seleção por agente)
- Gallery de projetos públicos
"""

import uuid
import base64
import hashlib
from datetime import datetime
from typing import Optional, Any


# ══════════════════════════════════════════════════════════════════════════════
# CUSTOM TOOLS REGISTRY
# ══════════════════════════════════════════════════════════════════════════════
class CustomToolStore:
    def __init__(self):
        self._tools: dict[str, dict] = {}

    def create(self, name: str, description: str, code: str) -> dict:
        tid = str(uuid.uuid4())[:8]
        tool = {
            "id": tid,
            "name": name,
            "description": description,
            "code": code,
            "created_at": datetime.utcnow().isoformat(),
        }
        self._tools[tid] = tool
        return tool

    def get(self, tid: str) -> Optional[dict]:
        return self._tools.get(tid)

    def update(self, tid: str, **kwargs) -> Optional[dict]:
        if tid not in self._tools:
            return None
        self._tools[tid].update(kwargs)
        self._tools[tid]["updated_at"] = datetime.utcnow().isoformat()
        return self._tools[tid]

    def delete(self, tid: str) -> bool:
        return bool(self._tools.pop(tid, None))

    def list_all(self) -> list[dict]:
        return sorted(self._tools.values(), key=lambda t: t["created_at"], reverse=True)


# ══════════════════════════════════════════════════════════════════════════════
# RAG FILE STORE
# ══════════════════════════════════════════════════════════════════════════════
class RAGFileStore:
    def __init__(self):
        self._files: dict[str, dict] = {}

    def upload(self, filename: str, content: bytes, mime_type: str) -> dict:
        fid = str(uuid.uuid4())[:8]
        # Extrai texto se for txt/md/html/py; caso contrário base64
        text_content = ""
        if mime_type in ("text/plain", "text/markdown", "text/html",
                         "text/x-python", "application/json", "text/csv"):
            try:
                text_content = content.decode("utf-8", errors="replace")
            except Exception:
                text_content = ""
        file_obj = {
            "id":           fid,
            "filename":     filename,
            "mime_type":    mime_type,
            "size":         len(content),
            "text_content": text_content,
            "b64_content":  base64.b64encode(content).decode() if not text_content else "",
            "sha256":       hashlib.sha256(content).hexdigest()[:12],
            "uploaded_at":  datetime.utcnow().isoformat(),
            "selected":     False,  # se está no contexto global
        }
        self._files[fid] = file_obj
        return {k: v for k, v in file_obj.items() if k != "b64_content"}  # não retorna binário

    def get(self, fid: str) -> Optional[dict]:
        return self._files.get(fid)

    def get_text(self, fid: str) -> str:
        f = self._files.get(fid)
        if not f:
            return ""
        return f.get("text_content", "")

    def set_selected(self, fid: str, selected: bool) -> None:
        if fid in self._files:
            self._files[fid]["selected"] = selected

    def get_selected_context(self, max_chars: int = 40000) -> str:
        """Retorna conteúdo dos arquivos selecionados concatenado."""
        selected = [f for f in self._files.values() if f.get("selected") and f.get("text_content")]
        if not selected:
            return ""
        parts = []
        used = 0
        for f in selected:
            content = f["text_content"]
            header  = f"=== ARQUIVO: {f['filename']} ===\n"
            chunk   = content if (used + len(content)) <= max_chars else content[:max_chars - used]
            parts.append(header + chunk)
            used += len(chunk)
            if used >= max_chars:
                parts.append("[... outros arquivos truncados por limite de contexto]")
                break
        return "\n\n".join(parts)

    def delete(self, fid: str) -> bool:
        return bool(self._files.pop(fid, None))

    def list_all(self) -> list[dict]:
        return [
            {k: v for k, v in f.items() if k not in ("b64_content", "text_content")}
            for f in sorted(self._files.values(), key=lambda x: x["uploaded_at"], reverse=True)
        ]


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC GALLERY
# ══════════════════════════════════════════════════════════════════════════════
class GalleryStore:
    def __init__(self):
        self._entries: dict[str, dict] = {}

    def publish(self, job_id: str, job_result: dict, user_request: str,
                title: str = "", tags: list[str] = [],
                allow_edit: bool = False) -> dict:
        gid = str(uuid.uuid4())[:8]
        entry = {
            "id":            gid,
            "job_id":        job_id,
            "title":         title or user_request[:60],
            "description":   user_request,
            "creation_type": job_result.get("creation_type", "other"),
            "detected_style":job_result.get("detected_style", ""),
            "color_palette": job_result.get("color_palette", []),
            "tags":          tags,
            "allow_edit":    allow_edit,
            "content":       job_result.get("content", ""),
            "views":         0,
            "downloads":     0,
            "published_at":  datetime.utcnow().isoformat(),
        }
        self._entries[gid] = entry
        return {k: v for k, v in entry.items() if k != "content"}

    def get(self, gid: str) -> Optional[dict]:
        e = self._entries.get(gid)
        if e:
            e["views"] += 1
        return e

    def list_all(self, ctype: str = "", search: str = "") -> list[dict]:
        entries = list(self._entries.values())
        if ctype:
            entries = [e for e in entries if e.get("creation_type") == ctype]
        if search:
            s = search.lower()
            entries = [e for e in entries if s in e["title"].lower() or s in e["description"].lower()]
        return [
            {k: v for k, v in e.items() if k != "content"}
            for e in sorted(entries, key=lambda x: x["published_at"], reverse=True)
        ]

    def inc_downloads(self, gid: str):
        if gid in self._entries:
            self._entries[gid]["downloads"] += 1

    def delete(self, gid: str) -> bool:
        return bool(self._entries.pop(gid, None))


# ── Singletons ────────────────────────────────────────────────────────────────
custom_tool_store = CustomToolStore()
rag_file_store    = RAGFileStore()
gallery_store     = GalleryStore()

# ══════════════════════════════════════════════════════════════════════════════
# MEDIA ASSET STORE — generated images, videos, audio files
# Persists generated media in memory and serves them via /api/assets/{id}
# Enables preview iframes to load generated assets by URL
# ══════════════════════════════════════════════════════════════════════════════
class MediaAssetStore:
    def __init__(self):
        self._assets: dict[str, dict] = {}

    def register(self, asset_id: str, asset_type: str, url: Optional[str],
                 prompt: str, model: str, meta: dict = {}) -> dict:
        """Register a newly generated asset."""
        asset = {
            "id":         asset_id,
            "type":       asset_type,   # image | video | audio | file
            "url":        url,          # external URL (Pollinations, etc.) or None if pending
            "prompt":     prompt,
            "model":      model,
            "meta":       meta,
            "data":       None,         # raw bytes if uploaded/fetched
            "mime_type":  None,
            "created_at": datetime.utcnow().isoformat(),
        }
        self._assets[asset_id] = asset
        return {k: v for k, v in asset.items() if k != "data"}

    def update_url(self, asset_id: str, url: str) -> None:
        """Update URL after async generation completes."""
        if asset_id in self._assets:
            self._assets[asset_id]["url"] = url
            if self._assets[asset_id].get("meta"):
                self._assets[asset_id]["meta"]["status"] = "ready"

    def store_bytes(self, asset_id: str, data: bytes, mime_type: str, url_path: str) -> None:
        """Store raw bytes for a file uploaded during the session (images, files, etc.)."""
        if asset_id in self._assets:
            self._assets[asset_id]["data"]      = data
            self._assets[asset_id]["mime_type"] = mime_type
            self._assets[asset_id]["url"]       = url_path
        else:
            self._assets[asset_id] = {
                "id": asset_id, "type": "file",
                "url": url_path, "prompt": "", "model": "upload",
                "meta": {}, "data": data, "mime_type": mime_type,
                "created_at": datetime.utcnow().isoformat(),
            }

    def upload_file(self, filename: str, data: bytes, mime_type: str) -> dict:
        """Upload a file (from annotation window or tool result) and return asset."""
        import uuid as _uuid
        asset_id = str(_uuid.uuid4())[:12]
        url_path = f"/api/assets/{asset_id}"
        self.store_bytes(asset_id, data, mime_type, url_path)
        self._assets[asset_id]["filename"] = filename
        self._assets[asset_id]["size"] = len(data)
        return {"id": asset_id, "url": url_path, "filename": filename,
                "mime_type": mime_type, "size": len(data)}

    def get(self, asset_id: str) -> Optional[dict]:
        return self._assets.get(asset_id)

    def get_bytes(self, asset_id: str) -> Optional[tuple[bytes, str]]:
        """Returns (data, mime_type) if stored locally, None otherwise."""
        asset = self._assets.get(asset_id)
        if asset and asset.get("data"):
            return asset["data"], asset.get("mime_type", "application/octet-stream")
        return None

    def list_by_job(self, job_id: str) -> list[dict]:
        return [
            {k: v for k, v in a.items() if k != "data"}
            for a in self._assets.values()
            if a.get("meta", {}).get("job_id") == job_id
        ]

    def list_all(self) -> list[dict]:
        return [
            {k: v for k, v in a.items() if k != "data"}
            for a in sorted(self._assets.values(), key=lambda x: x["created_at"], reverse=True)
        ]

    def delete(self, asset_id: str) -> bool:
        return bool(self._assets.pop(asset_id, None))


# ── Updated singletons ─────────────────────────────────────────────────────────
media_asset_store = MediaAssetStore()