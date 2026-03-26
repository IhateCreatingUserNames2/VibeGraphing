"""
╔══════════════════════════════════════════════════════════════╗
║      VIBE GRAPHING PIPELINE v4 — com HIL + Logs + Network   ║
╚══════════════════════════════════════════════════════════════╝
"""

import os, asyncio, time

from app.core.llm       import call_llm, parse_json_response
from app.core.models    import Job, StageType, StageProgress, job_store
from app.core.cache     import cache_load, cache_save
from app.core.contracts import AgentContract, WorkflowMessage, build_contracts_from_semantics, route_context
from app.core.hil       import hil_gate
from app.core.network   import build_network_prompt_block
from app.agents.cyclic  import run_with_reflection
from app.agents.tools   import get_specialist_model_info

HTML_TYPES = {"website","landing_page","game","dashboard"}
DEEP_TYPES = {"api","mobile_app","code"}

TYPE_HINTS = {
    "website":      "HTML completo com CSS em <style> e JS em <script>. Responsivo, moderno, profissional.",
    "landing_page": "Landing page HTML completa, persuasiva, CTA claro, CSS e JS embutidos.",
    "game":         "Jogo HTML5 completo e jogável, CSS e JS embutidos.",
    "dashboard":    "Dashboard HTML interativo com gráficos, CSS e JS embutidos.",
    "code":         "Código completo, bem comentado, pronto para rodar.",
    "api":          "API completa com todos endpoints, documentação inline.",
    "document":     "Documento completo em Markdown bem formatado.",
    "presentation": "Apresentação em Markdown com slides claros.",
    "script":       "Roteiro completo e bem formatado.",
    "mobile_app":   "Código completo do app mobile, estruturado e comentado.",
}

HTML_FORMAT = """REGRAS ABSOLUTAS:
1. Comece EXATAMENTE com <!DOCTYPE html> — nada antes
2. Todo CSS dentro de <style> no <head>
3. Todo JavaScript dentro de <script> antes de </body>
4. Imagens: Use generate_image tool para imagens reais (retorna URL do Pollinations), ou URLs do Unsplash como fallback
5. Google Fonts via CDN
6. Design responsivo com media queries
7. Textos REAIS e específicos — ZERO placeholder
8. Termine com </html> — arquivo 100% completo
9. NÃO escreva explicações — retorne SOMENTE o HTML"""


def _est(text: str) -> int: return max(1, len(text)//4)
def _safe_tok(sys, usr, ctx=150000, want=8192, margin=4096):
    return max(1024, min(want, ctx - _est(sys) - _est(usr) - margin))

def _clean(raw: str) -> str:
    s = raw.strip()
    for f in ("```html","```python","```javascript","```js","```markdown","```"):
        if s.lower().startswith(f): s=s[len(f):].lstrip("\n"); break
    if s.rstrip().endswith("```"): s=s.rstrip()[:-3]
    return s.strip()

def _fix_html(c: str) -> str:
    if not c.lower().startswith("<!doctype"):
        i=c.lower().find("<!doctype")
        if i>=0: c=c[i:]
    return c

def _should_fast(job: Job) -> bool:
    h=(job.creation_type_hint or "").lower()
    return h!="deep" and h not in DEEP_TYPES


# ══════════════════════════════════════════════════════════════════════════════
# FAST PIPELINE
# ══════════════════════════════════════════════════════════════════════════════
async def run_fast_pipeline(job_id: str, images: list[dict]):
    job = job_store.get(job_id)
    if not job: return
    try:
        job.status = "running"
        job.stage  = StageType.ROLE_ASSIGNMENT
        job.stage_progress = StageProgress(current=1, total=2, label="⚡ Analisando pedido...")
        job.add_log("info","🚀 Fast pipeline iniciado", f"Request: {job.user_request[:100]}", stage="role_assignment")
        job_store.update(job)

        # HIL pre-detect
        detect_prompt = f"""Analise e responda APENAS em JSON:
{{
  "creation_type": "website|landing_page|game|dashboard|code|api|document|presentation|script|other",
  "project_name": "Nome curto",
  "detected_style": "Estilo em 3-5 palavras",
  "color_palette": ["#hex1","#hex2","#hex3","#hex4","#hex5"],
  "sections": ["seção1","seção2","seção3","seção4","seção5"],
  "summary": "Resumo em 1 frase"
}}
PEDIDO: {job.user_request}
TIPO HINT: {job.creation_type_hint or 'auto'} | ESTILO: {job.style_hint or 'auto'}"""

        final_detect_prompt, res = await hil_gate(
            job, "role_assignment", "detector",
            "⚡ Prompt de detecção de tipo", detect_prompt,
            {"stage": "detect", "mode": "fast"}
        )
        if res == "stop": raise Exception("Pipeline interrompido pelo usuário")

        job.add_log("prompt","📤 Prompt de detecção enviado", final_detect_prompt, stage="role_assignment", agent_id="detector")
        t0=time.time()
        raw_detect = await call_llm(
            messages=[{"role":"user","content":final_detect_prompt}],
            system="Analise e retorne APENAS JSON puro.", model_type="build", max_tokens=400, json_mode=True,
        )
        job.add_log("output",f"📥 Detecção concluída ({time.time()-t0:.1f}s)", raw_detect, stage="role_assignment", agent_id="detector", meta={"duration":round(time.time()-t0,2)})
        job_store.update(job)

        meta  = parse_json_response(raw_detect)
        ctype = meta.get("creation_type","other")
        is_html = ctype in HTML_TYPES
        style = meta.get("detected_style", job.style_hint or "")
        palette = ", ".join(meta.get("color_palette", []))
        sections = ", ".join(meta.get("sections", []))

        job.role_assignment = {
            "creation_type": ctype, "project_summary": meta.get("summary",job.user_request[:100]),
            "detected_style": style, "color_palette": meta.get("color_palette",[]),
            "estimated_sections": meta.get("sections",[]),
            "agents":[{"id":"fast_agent","role":"Master Creator","responsibility":"Gera o projeto completo"}],
        }
        job.stage = StageType.GENERATING
        job.stage_progress = StageProgress(current=2,total=2,label=f"⚡ Gerando {ctype}...")
        job.add_log("graph","🗺️ Grafo Fast Mode", f"Tipo: {ctype} | Estilo: {style} | Paleta: {palette}", stage="generating")
        job_store.update(job)

        format_rules = HTML_FORMAT if is_html else f"Retorne {TYPE_HINTS.get(ctype,'o conteúdo')} — sem introdução."
        system = f"Você é expert em criar {ctype} de altíssima qualidade.\n{format_rules}"
        user_prompt = f"""PROJETO: {meta.get("summary",job.user_request)}
PEDIDO: {job.user_request}
TIPO: {ctype} | ESTILO: {style} | PALETA: {palette}
SEÇÕES: {sections} | NOME: {meta.get("project_name","")}
{build_network_prompt_block(job.agent_network)}
{format_rules}
Crie agora o {ctype} completo e profissional."""

        # HIL pre-generate
        final_gen_prompt, res2 = await hil_gate(
            job, "generating", "fast_agent",
            "⚡ Prompt de geração final", user_prompt,
            {"type": ctype, "style": style}
        )
        if res2 == "stop": raise Exception("Pipeline interrompido pelo usuário")
        if res2 == "skip":
            job.add_log("hil","⏭️ Geração pulada pelo usuário",stage="generating")
            return

        job.add_log("prompt","📤 Prompt de geração enviado", final_gen_prompt, stage="generating", agent_id="fast_agent")
        build_ctx = int(os.getenv("MAS_BUILD_CTX","190000"))
        safe_tok  = _safe_tok(system, final_gen_prompt, build_ctx, 16000, 8192)
        t0=time.time()

        raw = await call_llm(
            messages=[{"role":"user","content":final_gen_prompt}],
            system=system, model_type="build", max_tokens=safe_tok,
            images=images or None, retries=3, retry_delay=5.0,
        )
        dur=round(time.time()-t0,2)
        job.add_log("output",f"📥 Geração concluída ({dur}s) — {len(raw)} chars", raw[:500]+"...", stage="generating", agent_id="fast_agent", meta={"duration":dur,"chars":len(raw)})

        content = _clean(raw)
        if is_html: content = _fix_html(content)

        # HIL pós-geração — revisar output antes de finalizar
        post_prompt, res3 = await hil_gate(
            job, "generating", "fast_agent",
            "📋 Output gerado — revisar antes de finalizar",
            content, {"preview": content[:2000]}
        )
        if res3 == "stop": raise Exception("Pipeline interrompido pelo usuário")
        if res3 == "modify" and post_prompt:
            content = post_prompt  # usuário editou o output diretamente
            job.add_log("hil","✏️ Output modificado pelo usuário", stage="generating", agent_id="fast_agent")

        job.result = {
            "content":content, "creation_type":ctype,
            "project_summary":meta.get("summary",job.user_request[:100]),
            "detected_style":style, "color_palette":meta.get("color_palette",[]),
            "sections":meta.get("sections",[]),
            "agents_used":["fast_agent"], "agent_outputs":{"fast_agent":content}, "mode":"fast",
        }
        job.status="done"; job.stage=None
        job.stage_progress=StageProgress(current=2,total=2,label="✅ Concluído!")
        job.add_log("info","✅ Fast pipeline concluído", f"Tipo: {ctype} | {len(content)} chars", stage="done")
        job_store.update(job)

    except Exception as e:
        import traceback
        job.status="error"; job.error=f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        job.add_log("error","❌ Erro no pipeline", str(e))
        job_store.update(job)


# ══════════════════════════════════════════════════════════════════════════════
# DEEP PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

async def stage_role_assignment(job: Job, images: list[dict]) -> dict:
    network_block = build_network_prompt_block(job.agent_network)
    prompt = f"""Você é o Role Assigner de um sistema Vibe Graphing.

PEDIDO: {job.user_request}
TIPO HINT: {job.creation_type_hint or 'auto'} | ESTILO: {job.style_hint or 'auto'} | IMAGENS: {job.image_count}
{network_block}

Responda APENAS em JSON:
{{
  "project_summary": "Resumo em 1-2 frases",
  "creation_type": "website|code|game|document|api|mobile_app|landing_page|dashboard|other",
  "detected_style": "Estilo detectado",
  "color_palette": ["#hex1","#hex2","#hex3","#hex4"],
  "agents": [
    {{
      "id": "snake_case_id",
      "role": "Nome do Papel",
      "responsibility": "O que faz especificamente",
      "output_type": "content|design_spec|code|other",
      "input_fields": ["user_request"],
      "output_fields": ["nome_do_output"],
      "quality_threshold": 7.5,
      "max_iterations": 2
    }}
  ],
  "estimated_sections": ["seção1","seção2","seção3"]
}}
Máximo 4 agentes. input_fields e output_fields explícitos."""

    final_prompt, res = await hil_gate(
        job, "role_assignment", "role_assigner",
        "👥 Prompt de Role Assignment", prompt,
        {"network_jobs": job.agent_network}
    )
    if res == "stop": raise Exception("Pipeline interrompido pelo usuário")

    job.add_log("prompt","📤 Role Assignment prompt", final_prompt, stage="role_assignment", agent_id="role_assigner")
    t0=time.time()
    raw = await call_llm(
        messages=[{"role":"user","content":final_prompt}],
        system="Arquiteto de MAS. APENAS JSON.", model_type="build", max_tokens=2000,
        images=images or None, json_mode=True,
    )
    dur=round(time.time()-t0,2)
    job.add_log("output",f"📥 Role Assignment ({dur}s)", raw, stage="role_assignment", agent_id="role_assigner", meta={"duration":dur})
    return parse_json_response(raw)


async def stage_topology_design(job: Job, role_assignment: dict) -> dict:
    agents = [a["id"] for a in role_assignment["agents"]]
    prompt = f"""Defina topologia de execução para estes agentes: {agents}

Retorne APENAS JSON:
{{
  "stages": [
    {{"type": "parallel", "agents": ["id1","id2"]}},
    {{"type": "sequential", "agents": ["id3"]}}
  ]
}}
Paralelize tudo que puder. Máximo 2 estágios."""

    final_prompt, res = await hil_gate(
        job, "topology_design", "topology_designer",
        "🕸️ Prompt de Topology Design", prompt,
        {"agents": agents}
    )
    if res == "stop": raise Exception("Pipeline interrompido pelo usuário")

    job.add_log("prompt","📤 Topology prompt", final_prompt, stage="topology_design", agent_id="topology_designer")
    t0=time.time()
    raw = await call_llm(
        messages=[{"role":"user","content":final_prompt}],
        system="Define topologia. APENAS JSON.", model_type="build", max_tokens=400, json_mode=True,
    )
    dur=round(time.time()-t0,2)
    job.add_log("output",f"📥 Topology ({dur}s)", raw, stage="topology_design", agent_id="topology_designer", meta={"duration":dur})
    # Log do grafo visual
    try:
        topo = parse_json_response(raw)
        graph_str = "\n".join(f"  Stage {i+1} [{s['type']}]: {' → '.join(s['agents'])}" for i,s in enumerate(topo.get("stages",[])))
        job.add_log("graph","🗺️ Grafo de execução definido", graph_str, stage="topology_design")
    except: pass
    try: return parse_json_response(raw)
    except: return {"stages":[{"type":"parallel","agents":agents}]}


async def stage_semantic_completion(job: Job, role_assignment: dict, topology: dict, images: list[dict]) -> dict:
    agents_desc = "\n".join(
        f'- {a["id"]}: {a["role"]} — {a["responsibility"]} | in:{a.get("input_fields",[])} out:{a.get("output_fields",[])}'
        for a in role_assignment["agents"]
    )
    prompt = f"""Configure system prompts para os agentes do projeto "{role_assignment['project_summary']}":
{agents_desc}

Retorne APENAS JSON:
{{
  "agent_configs": {{
    "agent_id": {{
      "system_prompt": "System prompt completo e específico",
      "output_format": "Formato esperado",
      "quality_criteria": ["critério 1","critério 2"],
      "input_fields": ["campo1"],
      "output_fields": ["campo_output"]
    }}
  }},
  "assembler_instructions": "Como montar o resultado final"
}}"""

    final_prompt, res = await hil_gate(
        job, "semantic_completion", "semantic_completer",
        "🧠 Prompt de Semantic Completion", prompt,
        {"agents": [a["id"] for a in role_assignment["agents"]]}
    )
    if res == "stop": raise Exception("Pipeline interrompido pelo usuário")

    job.add_log("prompt","📤 Semantics prompt", final_prompt, stage="semantic_completion", agent_id="semantic_completer")
    for use_json in (True, False):
        try:
            t0=time.time()
            raw = await call_llm(
                messages=[{"role":"user","content":final_prompt}],
                system="Configure agentes. APENAS JSON.", model_type="build", max_tokens=3000,
                images=images or None, json_mode=use_json,
            )
            dur=round(time.time()-t0,2)
            job.add_log("output",f"📥 Semantics ({dur}s)", raw, stage="semantic_completion", agent_id="semantic_completer", meta={"duration":dur})
            return parse_json_response(raw)
        except Exception: continue
    return {"agent_configs":{},"assembler_instructions":"Monte o resultado final coeso."}


async def execute_agent_deep(agent_def: dict, contract: AgentContract,
                              message: WorkflowMessage, job: Job, images: list[dict]) -> str:
    aid = agent_def["id"]
    model_ctx = int(os.getenv("MAS_EXECUTE_CTX","150000"))
    ctx_budget = max(4000, model_ctx*4 - 8000 - 3000 - 8192*4 - 4096*4)
    context_block = route_context(message, contract, max_chars=ctx_budget)
    context_section = f"CONTEXTO RELEVANTE:\n{context_block}" if context_block else ""

    quality_threshold = float(agent_def.get("quality_threshold",0))
    max_iterations    = int(agent_def.get("max_iterations",1))
    use_cyclic        = quality_threshold > 0 and max_iterations > 1

    system_prompt = contract.instructions or f"Você é {contract.role}."
    user_prompt = f"""PROJETO: {job.user_request}
{context_section}

EXECUTE SUA TAREFA como {contract.role}:
{agent_def.get('responsibility','')}

Output ({contract.output_format}): {', '.join(contract.output_fields)}
Seja específico, completo e de alta qualidade."""

    # HIL antes de cada agente
    final_prompt, res = await hil_gate(
        job, "generating", aid,
        f"⚙️ Prompt do agente [{aid}] — {contract.role}",
        user_prompt,
        {"agent": aid, "role": contract.role, "contract_inputs": contract.input_fields}
    )
    if res == "stop": raise Exception(f"Pipeline interrompido no agente {aid}")
    if res == "skip":
        job.add_log("hil",f"⏭️ Agente {aid} pulado pelo usuário", stage="generating", agent_id=aid)
        return f"[Agente {aid} pulado pelo revisor]"

    job.add_log("prompt",f"📤 Agente [{aid}] prompt", final_prompt, stage="generating", agent_id=aid,
                meta={"role":contract.role,"cyclic":use_cyclic,"threshold":quality_threshold})
    t0=time.time()

    if use_cyclic:
        msg_ctx = WorkflowMessage(fields={**message.fields,"context_block":context_section})
        safe_tok = _safe_tok(system_prompt, final_prompt, model_ctx, 8192, 4096)
        output, trace = await run_with_reflection(
            agent_id=aid, contract=contract, message=msg_ctx, user_request=final_prompt,
            model_type="execute", max_iterations=max_iterations,
            quality_threshold=quality_threshold, max_tokens=safe_tok,
        )
        dur=round(time.time()-t0,2)
        job.add_log("output",f"📥 [{aid}] Cyclic concluído ({dur}s) — {len(trace)} iterações",
                    f"Score final: {trace[-1]['score'] if trace else '?'}\nOutput: {output[:400]}...",
                    stage="generating", agent_id=aid,
                    meta={"duration":dur,"iterations":len(trace),"score":trace[-1]["score"] if trace else 0})
        # Log cada iteração do cyclic
        for t in trace:
            job.add_log("info",
                f"{'✅' if t['approved'] else '🔄'} Cyclic iter {t['iteration']} — score {t['score']:.1f}",
                f"Feedback: {t.get('feedback','')}\nProblemas: {', '.join(t.get('issues',[]))}",
                stage="generating", agent_id=aid)
    else:
        safe_tok = _safe_tok(system_prompt, final_prompt, model_ctx, 8192, 4096)
        output = await call_llm(
            messages=[{"role":"user","content":final_prompt}],
            system=system_prompt, model_type="execute", max_tokens=safe_tok,
            images=images or None, retries=3, retry_delay=4.0,
        )
        dur=round(time.time()-t0,2)
        job.add_log("output",f"📥 [{aid}] concluído ({dur}s) — {len(output)} chars",
                    output[:500]+"...", stage="generating", agent_id=aid, meta={"duration":dur,"chars":len(output)})

    # HIL pós-agente — aprovar output antes de passar pro próximo
    final_output, res2 = await hil_gate(
        job, "generating", aid,
        f"📋 Output do agente [{aid}] — revisar",
        output, {"output_preview": output[:2000]}
    )
    if res2 == "stop": raise Exception(f"Pipeline interrompido após agente {aid}")
    if res2 == "modify" and final_output:
        job.add_log("hil",f"✏️ Output de [{aid}] modificado pelo usuário", stage="generating", agent_id=aid)
        return final_output

    job_store.update(job)
    return output


async def execute_workflow_deep(job: Job, role_assignment: dict, topology: dict,
                                 semantics: dict, images: list[dict]) -> dict[str, str]:
    contracts = build_contracts_from_semantics(role_assignment, semantics)
    message   = WorkflowMessage(fields={"user_request": job.user_request})
    agents_by_id = {a["id"]: a for a in role_assignment["agents"]}
    raw_outputs: dict[str, str] = {}

    topo_str = "\n".join(f"  Stage {i+1} [{s['type']}]: {' → '.join(s['agents'])}" for i,s in enumerate(topology.get("stages",[])))
    job.add_log("graph","🗺️ Iniciando execução do grafo",topo_str, stage="generating")

    for idx, stage in enumerate(topology["stages"]):
        aids = stage["agents"]
        job.add_log("info",f"🔄 Stage {idx+1} [{stage['type']}]: {', '.join(aids)}", stage="generating")

        if stage["type"]=="parallel":
            async def _run(aid: str) -> tuple[str,str]:
                adef = agents_by_id.get(aid,{"id":aid,"role":aid,"responsibility":"","quality_threshold":0,"max_iterations":1})
                contract = contracts.get(aid) or AgentContract(
                    agent_id=aid,role=aid,input_fields=["user_request"],output_fields=[aid],
                    instructions=adef.get("responsibility",""))
                out = await execute_agent_deep(adef, contract, message, job, images)
                return aid, out
            results = await asyncio.gather(*[_run(aid) for aid in aids])
            for aid, out in results:
                raw_outputs[aid] = out
                c = contracts.get(aid)
                for f in (c.output_fields if c else [aid]): message.set(f, out)
        else:
            for aid in aids:
                adef = agents_by_id.get(aid,{"id":aid,"role":aid,"responsibility":"","quality_threshold":0,"max_iterations":1})
                contract = contracts.get(aid) or AgentContract(
                    agent_id=aid,role=aid,input_fields=["user_request"],output_fields=[aid],
                    instructions=adef.get("responsibility",""))
                out = await execute_agent_deep(adef, contract, message, job, images)
                raw_outputs[aid] = out
                c = contracts.get(aid)
                for f in (c.output_fields if c else [aid]): message.set(f, out)

    return raw_outputs


async def assemble_final_output(job: Job, role_assignment: dict, semantics: dict,
                                  agent_outputs: dict[str,str], images: list[dict]) -> dict:
    ctype   = role_assignment.get("creation_type","other")
    is_html = ctype in HTML_TYPES
    all_outputs = "\n\n---\n\n".join(f"=== {k.upper()} ===\n{v}" for k,v in agent_outputs.items())
    assembler_instr = semantics.get("assembler_instructions","")
    format_instr = HTML_FORMAT if is_html else TYPE_HINTS.get(ctype,"Monte o output final.")

    system_prompt = f"Você é o Master Assembler.\n{assembler_instr}\n{format_instr}"
    user_prompt   = f"""PROJETO: {role_assignment['project_summary']}
PEDIDO: {job.user_request} | TIPO: {ctype}
ESTILO: {role_assignment['detected_style']} | PALETA: {', '.join(role_assignment.get('color_palette',[]))}
SEÇÕES: {', '.join(role_assignment.get('estimated_sections',[]))}

OUTPUTS DOS AGENTES:
{all_outputs}

{format_instr}
Comece diretamente pelo conteúdo."""

    # HIL antes do assembler
    final_prompt, res = await hil_gate(
        job, "generating", "assembler",
        "🔧 Prompt do Assembler Final", user_prompt,
        {"agents_used": list(agent_outputs.keys())}
    )
    if res == "stop": raise Exception("Pipeline interrompido no assembler")

    job.add_log("prompt","📤 Assembler prompt", final_prompt, stage="generating", agent_id="assembler")
    build_ctx = int(os.getenv("MAS_BUILD_CTX","190000"))
    safe_tok  = _safe_tok(system_prompt, final_prompt, build_ctx, 16000, 8192)
    t0=time.time()

    raw = await call_llm(
        messages=[{"role":"user","content":final_prompt}],
        system=system_prompt, model_type="build", max_tokens=safe_tok,
        images=images or None, retries=3, retry_delay=5.0,
    )
    dur=round(time.time()-t0,2)
    job.add_log("output",f"📥 Assembler concluído ({dur}s) — {len(raw)} chars",
                raw[:500]+"...", stage="generating", agent_id="assembler", meta={"duration":dur})

    content = _clean(raw)
    if is_html: content = _fix_html(content)

    # HIL final
    final_content, res2 = await hil_gate(
        job, "generating", "assembler",
        "✅ Output final — revisar antes de concluir",
        content, {"preview": content[:2000]}
    )
    if res2 == "stop": raise Exception("Pipeline interrompido na revisão final")
    if res2 == "modify" and final_content:
        content = final_content
        job.add_log("hil","✏️ Output final modificado pelo usuário", stage="generating", agent_id="assembler")

    return {
        "content":content, "creation_type":ctype,
        "project_summary":role_assignment["project_summary"],
        "detected_style":role_assignment["detected_style"],
        "color_palette":role_assignment.get("color_palette",[]),
        "sections":role_assignment.get("estimated_sections",[]),
        "agents_used":list(agent_outputs.keys()),
        "agent_outputs":agent_outputs, "mode":"deep",
    }


async def run_deep_pipeline(job_id: str, images: list[dict]):
    job = job_store.get(job_id)
    if not job: return
    try:
        job.status="running"
        job.add_log("info","🔬 Deep pipeline iniciado", f"HIL: {job.hil_enabled} | Network: {job.agent_network}", stage="role_assignment")
        job_store.update(job)

        # ── Cache check ────────────────────────────────────────────────
        cached = cache_load(job.creation_type_hint or "auto", job.style_hint or "", job.user_request)
        if cached:
            role_assignment = cached["role_assignment"]
            topology        = cached["topology"]
            semantics       = cached["semantics"]
            job.role_assignment = role_assignment
            job.topology        = topology
            job.semantic_config = semantics
            job.stage = StageType.GENERATING
            job.stage_progress = StageProgress(current=2,total=3,label="⚡ Cache hit! Pulando setup...")
            job.add_log("info","⚡ Cache hit — setup pulado", f"Tipo: {role_assignment.get('creation_type')}", stage="role_assignment")
            job_store.update(job)
        else:
            job.stage = StageType.ROLE_ASSIGNMENT
            job.stage_progress = StageProgress(current=1,total=4,label="👥 Definindo equipe...")
            job_store.update(job)
            role_assignment = await stage_role_assignment(job, images)
            job.role_assignment = role_assignment
            job_store.update(job)

            job.stage = StageType.TOPOLOGY_DESIGN
            job.stage_progress = StageProgress(current=2,total=4,label="🕸️ Desenhando grafo...")
            job_store.update(job)
            topology = await stage_topology_design(job, role_assignment)
            job.topology = topology
            job_store.update(job)

            job.stage = StageType.SEMANTIC_COMPLETION
            job.stage_progress = StageProgress(current=3,total=4,label="🧠 Configurando prompts...")
            job_store.update(job)
            semantics = await stage_semantic_completion(job, role_assignment, topology, images)
            job.semantic_config = semantics
            job_store.update(job)

            cache_save(
                role_assignment.get("creation_type","other"),
                role_assignment.get("detected_style",""),
                job.user_request,
                {"role_assignment":role_assignment,"topology":topology,"semantics":semantics}
            )

        # ── Runtime ────────────────────────────────────────────────────
        job.stage = StageType.GENERATING
        agent_count = len(role_assignment.get("agents",[]))
        job.stage_progress = StageProgress(current=4,total=4,label=f"⚙️ Executando {agent_count} agentes...")
        job_store.update(job)

        agent_outputs = await execute_workflow_deep(job, role_assignment, topology, semantics, images)
        job.agent_outputs = agent_outputs
        job_store.update(job)

        job.stage_progress = StageProgress(current=4,total=4,label="✨ Montando resultado...")
        job_store.update(job)
        result = await assemble_final_output(job, role_assignment, semantics, agent_outputs, images)

        job.result = result
        job.status = "done"; job.stage = None
        job.stage_progress = StageProgress(current=4,total=4,label="✅ Concluído! (Deep)")
        job.add_log("info","✅ Deep pipeline concluído",
                    f"Agentes: {', '.join(agent_outputs.keys())} | {len(result.get('content',''))} chars",stage="done")
        job_store.update(job)

    except Exception as e:
        import traceback
        job.status="error"; job.error=f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        job.add_log("error","❌ Erro no Deep pipeline", str(e))
        job_store.update(job)


# ── Entry point ────────────────────────────────────────────────────────────────
async def run_vibe_graphing_pipeline(job_id: str, images: list[dict]):
    job = job_store.get(job_id)
    if not job: return
    mode = "fast" if _should_fast(job) else "deep"
    print(f"[Pipeline] {job_id} → {mode.upper()} | HIL={'ON' if job.hil_enabled else 'off'}")
    if mode=="fast": await run_fast_pipeline(job_id, images)
    else:            await run_deep_pipeline(job_id, images)