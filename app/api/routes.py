"""API Routes — FastAPI"""

import base64
from typing import Optional
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, Response

from app.core.models   import job_store
from app.core.llm      import MODELS, call_llm
from app.core.registry import custom_tool_store, rag_file_store, gallery_store
from app.agents.pipeline import run_vibe_graphing_pipeline
from app.agents.editor   import run_edit_pipeline
from app.agents.tools    import list_tools

router = APIRouter()

ALLOWED_IMAGE_TYPES = {"image/jpeg","image/png","image/webp","image/gif"}
MAX_IMAGES = 5

async def _read_images(files: list[UploadFile]) -> list[dict]:
    result = []
    for f in files:
        if f.content_type not in ALLOWED_IMAGE_TYPES:
            raise HTTPException(400, f"Tipo não suportado: {f.content_type}")
        data = await f.read()
        if len(data) > 10 * 1024 * 1024:
            raise HTTPException(400, "Imagem muito grande (máx 10MB).")
        result.append({"data": base64.b64encode(data).decode(),
                        "media_type": f.content_type, "filename": f.filename})
    return result


# ══════════════════════════════════════════════════════════════════════════════
# CRIAÇÃO / EDIÇÃO / UNDO / VERSÕES
# ══════════════════════════════════════════════════════════════════════════════
@router.post("/create")
async def create(
    background_tasks: BackgroundTasks,
    request:       str = Form(...),
    creation_type: Optional[str] = Form(None),
    style:         Optional[str] = Form(None),
    hil_enabled:   Optional[str] = Form("false"),
    hil_stages:    Optional[str] = Form("all"),
    images: list[UploadFile] = File(default=[]),
):
    if not request or len(request.strip()) < 5:
        raise HTTPException(400, "Descrição muito curta.")
    if len(images) > MAX_IMAGES:
        raise HTTPException(400, f"Máximo {MAX_IMAGES} imagens.")
    processed = await _read_images(images)

    # Injeta contexto dos arquivos RAG selecionados no request
    rag_ctx = rag_file_store.get_selected_context()
    full_request = request.strip()
    if rag_ctx:
        full_request += f"\n\n[CONTEXTO DE ARQUIVOS RAG]\n{rag_ctx}"

    hil_on = str(hil_enabled).lower() in ("true","1","yes")
    hil_stage_list = [s.strip() for s in (hil_stages or "all").split(",") if s.strip()]

    job = job_store.create(
        user_request=full_request, creation_type_hint=creation_type or "",
        style_hint=style or "", has_images=bool(processed), image_count=len(processed),
        hil_enabled=hil_on, hil_stages=hil_stage_list,
    )
    background_tasks.add_task(run_vibe_graphing_pipeline, job.id, processed)
    return {"job_id": job.id, "status_url": f"/api/status/{job.id}",
            "result_url": f"/api/result/{job.id}"}


@router.post("/edit/{job_id}")
async def edit(
    job_id: str, background_tasks: BackgroundTasks,
    instruction: str = Form(...),
    images: list[UploadFile] = File(default=[]),
):
    job = job_store.get(job_id)
    if not job:       raise HTTPException(404, "Job não encontrado.")
    if job.status == "running": raise HTTPException(409, "Job em execução.")
    if not job.result: raise HTTPException(400, "Sem resultado para editar.")
    processed = await _read_images(images)
    background_tasks.add_task(run_edit_pipeline, job_id, instruction.strip(), processed)
    return {"job_id": job_id, "message": f"Editando: '{instruction[:80]}'..."}


@router.post("/undo/{job_id}")
async def undo(job_id: str, version: Optional[int] = None):
    job = job_store.get(job_id)
    if not job: raise HTTPException(404)
    if not job.versions: raise HTTPException(400, "Sem versões anteriores.")
    if job.status == "running": raise HTTPException(409)
    target = next((v for v in job.versions if v["version"] == version), None) \
             if version is not None else job.versions[-1]
    if not target: raise HTTPException(404, f"Versão {version} não encontrada.")
    job.save_version(f"[undo para v{target['version']}]")
    if job.result:
        job.result["content"] = target["content"]
        job.result["version"] = target["version"]
    job_store.update(job)
    return {"restored_version": target["version"]}


@router.get("/versions/{job_id}")
async def get_versions(job_id: str):
    job = job_store.get(job_id)
    if not job: raise HTTPException(404)
    return {"versions": [{"version": v["version"], "timestamp": v["timestamp"],
                           "instruction": v.get("instruction",""), "size": len(v.get("content",""))}
                          for v in job.versions]}


@router.get("/version/{job_id}/{version}", response_class=HTMLResponse)
async def preview_version(job_id: str, version: int):
    job = job_store.get(job_id)
    if not job: raise HTTPException(404)
    target = next((v for v in job.versions if v["version"] == version), None)
    if not target: raise HTTPException(404)
    content = target.get("content","")
    if content.strip().lower().startswith("<!doctype"):
        return HTMLResponse(content)
    escaped = content.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    return HTMLResponse(f"<pre style='font-family:monospace;padding:2rem'>{escaped}</pre>")


# ══════════════════════════════════════════════════════════════════════════════
# STATUS / RESULT / PREVIEW / DOWNLOAD
# ══════════════════════════════════════════════════════════════════════════════
@router.get("/status/{job_id}")
async def get_status(job_id: str):
    job = job_store.get(job_id)
    if not job: raise HTTPException(404)
    return {
        "job_id": job.id, "status": job.status, "stage": job.stage,
        "stage_progress": job.stage_progress.model_dump(),
        "has_result": job.result is not None,
        "version": len(job.versions), "error": job.error,
        "created_at": job.created_at, "updated_at": job.updated_at,
        "pipeline": {
            "role_assignment": job.role_assignment,
            "topology": job.topology,
            "agents_executed": list(job.agent_outputs.keys()) if job.agent_outputs else [],
        },
    }


@router.get("/result/{job_id}")
async def get_result(job_id: str):
    job = job_store.get(job_id)
    if not job: raise HTTPException(404)
    if job.status == "error": raise HTTPException(500, f"Erro: {job.error}")
    if job.status != "done": return {"status": job.status, "message": "Processando..."}
    return {"job_id": job.id, "status": "done", "result": job.result}


@router.get("/preview/{job_id}", response_class=HTMLResponse)
async def preview(job_id: str):
    job = job_store.get(job_id)
    if not job or job.status != "done":
        return HTMLResponse("<h1 style='font-family:sans-serif;padding:2rem'>⏳ Processando...</h1>", 202)
    content = job.result.get("content","")
    if content.strip().lower().startswith("<!doctype") or "<html" in content.lower()[:200]:
        return HTMLResponse(content)
    escaped = content.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    return HTMLResponse(f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>body{{font-family:'Fira Code',monospace;background:#0d1117;color:#e6edf3;padding:2rem}}
pre{{white-space:pre-wrap;word-break:break-word}}</style></head>
<body><pre>{escaped}</pre></body></html>""")


@router.get("/download/{job_id}")
async def download(job_id: str):
    job = job_store.get(job_id)
    if not job or job.status != "done": raise HTTPException(404)
    content = job.result.get("content","")
    ct = job.result.get("creation_type","other")
    ext_map = {"website":("index.html","text/html"),"landing_page":("index.html","text/html"),
               "game":("game.html","text/html"),"dashboard":("dashboard.html","text/html"),
               "code":("output.py","text/plain"),"api":("api.py","text/plain"),
               "document":("document.md","text/markdown")}
    filename, media_type = ext_map.get(ct, ("output.txt","text/plain"))
    return Response(content=content.encode("utf-8"), media_type=media_type,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/jobs")
async def list_jobs():
    jobs = job_store.list_recent(30)
    return [{"job_id":j.id,"status":j.status,"stage":j.stage,
             "user_request":j.user_request[:80]+("..."if len(j.user_request)>80 else ""),
             "creation_type":j.role_assignment.get("creation_type") if j.role_assignment else None,
             "version":len(j.versions),"created_at":j.created_at} for j in jobs]


# ══════════════════════════════════════════════════════════════════════════════
# CUSTOM TOOLS
# ══════════════════════════════════════════════════════════════════════════════
@router.get("/custom-tools")
async def list_custom_tools():
    return {"tools": custom_tool_store.list_all()}


@router.post("/custom-tools")
async def create_custom_tool(
    name: str = Form(...),
    description: str = Form(...),
    code: str = Form(...),
):
    if not name.strip(): raise HTTPException(400, "Nome obrigatório.")
    tool = custom_tool_store.create(name.strip(), description.strip(), code.strip())
    return tool


@router.put("/custom-tools/{tid}")
async def update_custom_tool(
    tid: str,
    name: str = Form(...),
    description: str = Form(...),
    code: str = Form(...),
):
    tool = custom_tool_store.update(tid, name=name, description=description, code=code)
    if not tool: raise HTTPException(404)
    return tool


@router.delete("/custom-tools/{tid}")
async def delete_custom_tool(tid: str):
    if not custom_tool_store.delete(tid): raise HTTPException(404)
    return {"deleted": tid}


@router.post("/custom-tools/generate")
async def generate_tool_code(description: str = Form(...)):
    """Usa LLM para gerar código de uma tool a partir da descrição."""
    TOOL_TEMPLATE = '''
class MyTool(BaseTool):
    name = "tool_name"           # snake_case, sem espaços
    description = "O que faz"
    parameters_schema = {
        "type": "object",
        "properties": {
            "param1": {"type": "string", "description": "Descrição do param"},
        },
        "required": ["param1"],
    }

    async def execute(self, params: dict) -> str:
        import json
        param1 = params.get("param1", "")
        # Sua lógica aqui — pode usar httpx para requisições:
        # async with httpx.AsyncClient(timeout=15.0) as client:
        #     r = await client.get("https://api.exemplo.com", params={"q": param1})
        #     data = r.json()
        result = {"output": f"Processado: {param1}"}
        return json.dumps(result, ensure_ascii=False)
'''
    prompt = f"""Crie uma Tool Python para um sistema Multi-Agent seguindo EXATAMENTE este template:

{TOOL_TEMPLATE}

DESCRIÇÃO DO QUE A TOOL DEVE FAZER:
{description}

REGRAS:
1. Herde de BaseTool (não precisa importar, já está disponível)
2. Use httpx para requisições HTTP (já importado)
3. Retorne SEMPRE json.dumps(dict) como string
4. name deve ser snake_case descritivo
5. parameters_schema deve ter todos os parâmetros necessários
6. Código funcional, sem imports desnecessários
7. Retorne APENAS o código Python da classe, sem explicações"""

    code = await call_llm(
        messages=[{"role":"user","content":prompt}],
        system="Você é um expert em Python. Gere apenas código limpo e funcional.",
        model_type="build", max_tokens=2000,
    )
    # Limpa fences
    code = code.strip()
    for fence in ("```python","```"):
        if code.startswith(fence): code = code[len(fence):]
        if code.endswith("```"): code = code[:-3]
    return {"code": code.strip()}


# ══════════════════════════════════════════════════════════════════════════════
# RAG FILES
# ══════════════════════════════════════════════════════════════════════════════
ALLOWED_FILE_TYPES = {
    "text/plain","text/markdown","text/html","text/csv","text/x-python",
    "application/json","application/pdf",
    "image/jpeg","image/png","image/webp",
}

@router.get("/rag-files")
async def list_rag_files():
    return {"files": rag_file_store.list_all()}


@router.post("/rag-files")
async def upload_rag_file(file: UploadFile = File(...)):
    if file.content_type not in ALLOWED_FILE_TYPES:
        raise HTTPException(400, f"Tipo não suportado: {file.content_type}")
    data = await file.read()
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(400, "Arquivo muito grande (máx 20MB).")
    result = rag_file_store.upload(file.filename, data, file.content_type)
    return result


@router.patch("/rag-files/{fid}/select")
async def toggle_rag_file(fid: str, selected: bool = Form(...)):
    f = rag_file_store.get(fid)
    if not f: raise HTTPException(404)
    rag_file_store.set_selected(fid, selected)
    return {"id": fid, "selected": selected}


@router.get("/rag-files/{fid}/content")
async def get_rag_file_content(fid: str):
    f = rag_file_store.get(fid)
    if not f: raise HTTPException(404)
    text = rag_file_store.get_text(fid)
    return {"id": fid, "filename": f["filename"], "content": text[:5000],
            "total_chars": len(text)}


@router.delete("/rag-files/{fid}")
async def delete_rag_file(fid: str):
    if not rag_file_store.delete(fid): raise HTTPException(404)
    return {"deleted": fid}


# ══════════════════════════════════════════════════════════════════════════════
# GALLERY
# ══════════════════════════════════════════════════════════════════════════════
@router.post("/gallery/publish/{job_id}")
async def publish_to_gallery(
    job_id: str,
    title:      str = Form(""),
    tags:       str = Form(""),        # comma-separated
    allow_edit: bool = Form(False),
):
    job = job_store.get(job_id)
    if not job or job.status != "done": raise HTTPException(400, "Job não concluído.")
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    entry = gallery_store.publish(
        job_id=job_id, job_result=job.result,
        user_request=job.user_request, title=title,
        tags=tag_list, allow_edit=allow_edit,
    )
    return entry


@router.get("/gallery")
async def list_gallery(ctype: str = "", search: str = ""):
    return {"entries": gallery_store.list_all(ctype=ctype, search=search)}


@router.get("/gallery/{gid}")
async def get_gallery_entry(gid: str):
    entry = gallery_store.get(gid)
    if not entry: raise HTTPException(404)
    return entry


@router.get("/gallery/{gid}/preview", response_class=HTMLResponse)
async def preview_gallery(gid: str):
    entry = gallery_store.get(gid)
    if not entry: raise HTTPException(404)
    content = entry.get("content","")
    if content.strip().lower().startswith("<!doctype"):
        return HTMLResponse(content)
    escaped = content.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    return HTMLResponse(f"<pre style='font-family:monospace;background:#0d1117;color:#e6edf3;padding:2rem;min-height:100vh'>{escaped}</pre>")


@router.get("/gallery/{gid}/download")
async def download_gallery(gid: str):
    entry = gallery_store.get(gid)
    if not entry: raise HTTPException(404)
    gallery_store.inc_downloads(gid)
    content = entry.get("content","")
    ct = entry.get("creation_type","other")
    ext_map = {"website":"index.html","landing_page":"index.html","game":"game.html",
               "dashboard":"dashboard.html","code":"output.py","document":"document.md"}
    filename = ext_map.get(ct,"output.txt")
    mt = "text/html" if filename.endswith(".html") else "text/plain"
    return Response(content=content.encode("utf-8"), media_type=mt,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.delete("/gallery/{gid}")
async def delete_gallery_entry(gid: str):
    if not gallery_store.delete(gid): raise HTTPException(404)
    return {"deleted": gid}


# ══════════════════════════════════════════════════════════════════════════════
# BUILT-IN TOOLS / CACHE / MODELS / HEALTH
# ══════════════════════════════════════════════════════════════════════════════
@router.get("/tools")
async def get_tools():
    builtin = list_tools()
    custom  = [{"name":t["name"],"description":t["description"],"custom":True}
               for t in custom_tool_store.list_all()]
    return {"tools": builtin + custom}


@router.get("/cache")
async def get_cache():
    from app.core.cache import cache_list
    return {"entries": cache_list()}


@router.delete("/cache")
async def clear_cache(key: Optional[str] = None):
    from app.core.cache import cache_clear
    return {"deleted": cache_clear(key)}


@router.get("/models")
async def get_models():
    return {"current": MODELS}


@router.get("/health")
async def health():
    import os
    return {"status":"ok","api_key_set":bool(os.getenv("OPENROUTER_API_KEY")),"models":MODELS}


# ══════════════════════════════════════════════════════════════════════════════
# LOGS
# ══════════════════════════════════════════════════════════════════════════════
@router.get("/logs/{job_id}")
async def get_logs(job_id: str, level: Optional[str] = None, agent_id: Optional[str] = None, limit: int = 200):
    job = job_store.get(job_id)
    if not job: raise HTTPException(404)
    logs = job.logs
    if level:    logs = [l for l in logs if l.get("level") == level]
    if agent_id: logs = [l for l in logs if l.get("agent_id") == agent_id]
    return {"job_id": job_id, "total": len(job.logs), "logs": logs[-limit:]}


@router.get("/logs/{job_id}/graph")
async def get_execution_graph(job_id: str):
    """Retorna o grafo de execução: stages, agentes, dependências e timings."""
    job = job_store.get(job_id)
    if not job: raise HTTPException(404)

    # Extrai timings dos logs
    timings = {}
    for log in job.logs:
        aid = log.get("agent_id","")
        if aid and log.get("level") == "output":
            dur = log.get("meta",{}).get("duration",0)
            if aid not in timings or dur > timings[aid].get("duration",0):
                timings[aid] = {"duration": dur, "chars": log.get("meta",{}).get("chars",0)}

    agents = []
    if job.role_assignment:
        for a in job.role_assignment.get("agents",[]):
            aid = a["id"]
            agents.append({
                "id":           aid,
                "role":         a.get("role",""),
                "responsibility": a.get("responsibility",""),
                "status":       "done" if aid in job.agent_outputs else "pending",
                "duration":     timings.get(aid,{}).get("duration",0),
                "output_chars": timings.get(aid,{}).get("chars",0),
                "quality_threshold": a.get("quality_threshold",0),
                "max_iterations":    a.get("max_iterations",1),
            })

    return {
        "job_id":   job_id,
        "mode":     job.result.get("mode","?") if job.result else "?",
        "topology": job.topology,
        "agents":   agents,
        "hil_enabled": job.hil_enabled,
        "checkpoints_count": len(job.checkpoints),
        "graph_logs": [l for l in job.logs if l.get("level") == "graph"],
    }


# ══════════════════════════════════════════════════════════════════════════════
# HUMAN IN THE LOOP
# ══════════════════════════════════════════════════════════════════════════════
@router.get("/hil/{job_id}")
async def get_hil_status(job_id: str):
    """Retorna o checkpoint ativo (se houver) e todos os checkpoints anteriores."""
    job = job_store.get(job_id)
    if not job: raise HTTPException(404)
    active = None
    if job.active_checkpoint_id:
        active = job.get_checkpoint(job.active_checkpoint_id)
    return {
        "job_id":               job_id,
        "status":               job.status,
        "hil_enabled":          job.hil_enabled,
        "hil_stages":           job.hil_stages,
        "active_checkpoint":    active,
        "checkpoints":          job.checkpoints,
    }


@router.post("/hil/{job_id}/resolve/{cp_id}")
async def resolve_checkpoint(
    job_id: str, cp_id: str,
    resolution:       str  = Form(...),   # approve | modify | skip | stop
    user_note:        str  = Form(""),
    modified_prompt:  Optional[str] = Form(None),
    modified_output:  Optional[str] = Form(None),
):
    """Resolve um checkpoint HIL — libera o pipeline para continuar."""
    job = job_store.get(job_id)
    if not job: raise HTTPException(404)
    if job.status != "paused": raise HTTPException(400, "Job não está pausado.")
    cp = job.get_checkpoint(cp_id)
    if not cp: raise HTTPException(404, f"Checkpoint {cp_id} não encontrado.")
    if cp.get("resolved"): raise HTTPException(400, "Checkpoint já resolvido.")

    valid = {"approve","modify","skip","stop"}
    if resolution not in valid:
        raise HTTPException(400, f"resolution deve ser um de: {valid}")

    job.resolve_checkpoint(cp_id, resolution=resolution, user_note=user_note,
                           modified_prompt=modified_prompt, modified_output=modified_output)
    job.add_log("hil", f"✅ Checkpoint {cp_id} resolvido: {resolution}",
                f"Nota: {user_note}" if user_note else "", stage=cp.get("stage",""), agent_id=cp.get("agent_id",""))
    job_store.update(job)
    return {"resolved": cp_id, "resolution": resolution}


@router.patch("/hil/{job_id}/config")
async def configure_hil(
    job_id: str,
    enabled: bool = Form(...),
    stages:  str  = Form("all"),   # comma-separated: all|role_assignment|topology_design|semantic_completion|generating
):
    """Ativa/desativa HIL e configura quais stages pausam."""
    job = job_store.get(job_id)
    if not job: raise HTTPException(404)
    job.hil_enabled = enabled
    job.hil_stages  = [s.strip() for s in stages.split(",") if s.strip()]
    job_store.update(job)
    return {"hil_enabled": job.hil_enabled, "hil_stages": job.hil_stages}


# ══════════════════════════════════════════════════════════════════════════════
# AGENT NETWORK
# ══════════════════════════════════════════════════════════════════════════════
@router.get("/network/{job_id}")
async def get_network(job_id: str):
    """Retorna os jobs conectados à rede deste job."""
    job = job_store.get(job_id)
    if not job: raise HTTPException(404)
    connected = []
    for jid in job.agent_network:
        j = job_store.get(jid)
        if j:
            ra = j.role_assignment or {}
            connected.append({
                "job_id":         jid,
                "status":         j.status,
                "creation_type":  ra.get("creation_type","?"),
                "project_summary":ra.get("project_summary",j.user_request[:60]),
                "detected_style": ra.get("detected_style",""),
                "agents":         [a["id"] for a in ra.get("agents",[])],
                "created_at":     j.created_at,
            })
    return {"job_id": job_id, "network": connected}


@router.post("/network/{job_id}/connect")
async def connect_to_network(job_id: str, source_job_id: str = Form(...)):
    """Conecta este job a outro — seus outputs/agentes ficam disponíveis como contexto."""
    job = job_store.get(job_id)
    if not job: raise HTTPException(404)
    source = job_store.get(source_job_id)
    if not source: raise HTTPException(404, f"Job fonte {source_job_id} não encontrado.")
    if source_job_id not in job.agent_network:
        job.agent_network.append(source_job_id)
        job_store.update(job)
    return {"connected": source_job_id, "network": job.agent_network}


@router.delete("/network/{job_id}/disconnect/{source_job_id}")
async def disconnect_from_network(job_id: str, source_job_id: str):
    job = job_store.get(job_id)
    if not job: raise HTTPException(404)
    job.agent_network = [j for j in job.agent_network if j != source_job_id]
    job_store.update(job)
    return {"disconnected": source_job_id, "network": job.agent_network}


# ══════════════════════════════════════════════════════════════════════════════
# TOOLS NA GALERIA
# ══════════════════════════════════════════════════════════════════════════════
@router.post("/gallery/publish-tool/{tid}")
async def publish_tool_to_gallery(
    tid:         str,
    description: str = Form(""),
    tags:        str = Form(""),
):
    """Publica uma custom tool na galeria para a comunidade."""
    tool = custom_tool_store.get(tid)
    if not tool: raise HTTPException(404, "Tool não encontrada.")
    gid = str(__import__('uuid').uuid4())[:8]
    entry = {
        "id":          gid,
        "type":        "tool",
        "tool_id":     tid,
        "title":       tool["name"],
        "description": description or tool.get("description",""),
        "code":        tool["code"],
        "tags":        [t.strip() for t in tags.split(",") if t.strip()],
        "views":       0,
        "downloads":   0,
        "published_at": __import__('datetime').datetime.utcnow().isoformat(),
    }
    gallery_store._entries[gid] = entry
    return {k: v for k, v in entry.items() if k != "code"}


@router.get("/gallery/{gid}/code")
async def get_gallery_tool_code(gid: str):
    """Retorna o código de uma tool publicada na galeria."""
    entry = gallery_store._entries.get(gid)
    if not entry: raise HTTPException(404)
    if entry.get("type") != "tool": raise HTTPException(400, "Esta entrada não é uma tool.")
    entry["downloads"] = entry.get("downloads", 0) + 1
    return {"code": entry.get("code",""), "title": entry["title"]}

# ══════════════════════════════════════════════════════════════════════════════
# MEDIA ASSETS — serve generated images/videos/audio + session file uploads
# Assets are accessible from preview iframes so sites can load generated media
# ══════════════════════════════════════════════════════════════════════════════
from app.core.registry import media_asset_store

@router.get("/assets/{asset_id}")
async def get_asset(asset_id: str):
    """
    Serve a media asset by ID.
    If stored as bytes: return directly.
    If stored as external URL: redirect.
    """
    from fastapi.responses import RedirectResponse, StreamingResponse
    asset = media_asset_store.get(asset_id)
    if not asset:
        raise HTTPException(404, f"Asset {asset_id} not found")

    # Locally stored bytes (uploaded files, fetched images)
    local = media_asset_store.get_bytes(asset_id)
    if local:
        data, mime = local
        return Response(content=data, media_type=mime,
            headers={"Cache-Control": "public, max-age=86400",
                     "Access-Control-Allow-Origin": "*"})

    # External URL redirect
    url = asset.get("url")
    if url and url.startswith("http"):
        return RedirectResponse(url=url, status_code=302)

    raise HTTPException(503, f"Asset {asset_id} is still generating (status: pending)")


@router.get("/assets")
async def list_assets():
    """List all media assets generated in this session."""
    return {"assets": media_asset_store.list_all()}


@router.post("/assets/upload")
async def upload_asset(file: UploadFile = File(...), job_id: Optional[str] = Form(None)):
    """
    Upload a file as a session asset (from annotation window or tool use).
    Returns asset_id + URL accessible in preview.
    """
    data = await file.read()
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(400, "File too large (max 50MB)")
    result = media_asset_store.upload_file(file.filename, data, file.content_type or "application/octet-stream")
    if job_id:
        asset = media_asset_store.get(result["id"])
        if asset:
            asset["meta"]["job_id"] = job_id
    return result


@router.delete("/assets/{asset_id}")
async def delete_asset(asset_id: str):
    if not media_asset_store.delete(asset_id):
        raise HTTPException(404)
    return {"deleted": asset_id}


# ══════════════════════════════════════════════════════════════════════════════
# SECTION-MARKED EDIT — edit only a specific marked section
# Receives section HTML + instruction + optional images + reference files
# Much more compact than sending the full document
# ══════════════════════════════════════════════════════════════════════════════
from app.agents.editor import run_section_edit_pipeline

@router.post("/edit-section/{job_id}")
async def edit_section(
    job_id:       str,
    background_tasks: BackgroundTasks,
    instruction:  str                       = Form(...),
    section_html: str                       = Form(...),
    section_path: str                       = Form(""),   # CSS selector hint
    images:       list[UploadFile]          = File(default=[]),
    ref_files:    list[UploadFile]          = File(default=[]),  # reference files from annotation window
):
    """
    Edit a specific marked section of the project.
    section_html: the outerHTML of the marked element
    instruction:  what to change in that section
    images:       optional reference images (uploaded in annotation window)
    ref_files:    optional reference documents/files for the agent to read
    """
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.status == "running":
        raise HTTPException(409, "Job is currently running")
    if not job.result:
        raise HTTPException(400, "No result to edit")
    if not section_html.strip():
        raise HTTPException(400, "section_html is required")

    processed_images = await _read_images(images)

    # Process reference files — extract text content for agent context
    processed_refs = []
    for rf in ref_files[:5]:  # max 5 ref files
        data = await rf.read()
        if len(data) > 10 * 1024 * 1024:
            continue
        mime = rf.content_type or "application/octet-stream"
        # Save as asset too so it's accessible
        asset_result = media_asset_store.upload_file(rf.filename, data, mime)
        # Extract text if possible
        text_content = ""
        if mime in ("text/plain", "text/markdown", "text/html", "text/x-python",
                    "application/json", "text/css", "text/csv"):
            try:
                text_content = data.decode("utf-8", errors="replace")[:5000]
            except Exception:
                pass
        processed_refs.append({
            "filename":  rf.filename,
            "mime_type": mime,
            "content":   text_content,
            "asset_id":  asset_result["id"],
            "asset_url": asset_result["url"],
        })

    background_tasks.add_task(
        run_section_edit_pipeline,
        job_id, instruction.strip(), section_html.strip(),
        section_path.strip(), processed_images, processed_refs,
    )
    return {
        "job_id":  job_id,
        "message": f"Editing section: '{instruction[:80]}'...",
        "section_path": section_path,
        "ref_files_count": len(processed_refs),
    }


# ══════════════════════════════════════════════════════════════════════════════
# SPECIALIST MODELS INFO — tells frontend which media tools are available
# ══════════════════════════════════════════════════════════════════════════════
@router.get("/specialist-models")
async def get_specialist_models():
    """Returns available OpenRouter specialist models for media generation."""
    from app.agents.tools import OPENROUTER_SPECIALIST_MODELS
    return {"models": OPENROUTER_SPECIALIST_MODELS}