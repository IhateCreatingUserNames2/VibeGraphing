"""
╔══════════════════════════════════════════════════════╗
║            EDITOR — Edição Iterativa de Projetos     ║
║                                                      ║
║  Permite editar projetos já concluídos com:          ║
║   - Instruções em linguagem natural                  ║
║   - Novas imagens de referência                      ║
║   - Uso de tools (gerar imagens, buscar, etc.)       ║
║   - Histórico de versões (undo)                      ║
╚══════════════════════════════════════════════════════╝
"""

import json
from app.core.llm import call_llm, parse_json_response
from app.core.models import Job, StageType, StageProgress, job_store
from app.agents.tools import get_tool, get_tools_for_agent, list_tools


async def run_edit_pipeline(job_id: str, instruction: str, images: list[dict]):
    """
    Edita um projeto existente aplicando a instrução do usuário.
    Salva a versão anterior antes de modificar.
    """
    job = job_store.get(job_id)
    if not job or not job.result:
        return

    # Salva versão atual antes de editar
    job.save_version(instruction)
    job.status = "running"
    job.stage = StageType.EDITING
    job.stage_progress = StageProgress(current=1, total=2, label=f"🖊️ Aplicando: {instruction[:60]}...")
    job_store.update(job)

    try:
        creation_type = job.result.get("creation_type", "other")
        current_content = job.result.get("content", "")
        is_html = creation_type in ("website", "landing_page", "game", "dashboard")

        # ── Decide se precisa usar tools ──────────────────────────────────
        tools_needed = await _decide_tools(instruction, creation_type)
        tool_results = {}

        if tools_needed:
            job.stage_progress = StageProgress(
                current=1, total=2,
                label=f"🔧 Executando tools: {', '.join(tools_needed)}..."
            )
            job_store.update(job)
            tool_results = await _run_tools(tools_needed, instruction, job)

        # ── Aplica a edição ───────────────────────────────────────────────
        job.stage_progress = StageProgress(current=2, total=2, label="✏️ Reescrevendo...")
        job_store.update(job)

        new_content = await _apply_edit(
            current_content=current_content,
            instruction=instruction,
            creation_type=creation_type,
            job=job,
            tool_results=tool_results,
            images=images,
        )

        # Valida HTML básico
        if is_html and not new_content.strip().lower().startswith("<!doctype"):
            idx = new_content.lower().find("<!doctype")
            if idx >= 0:
                new_content = new_content[idx:]

        # Atualiza resultado
        job.result["content"] = new_content
        job.result["last_edit"] = instruction
        job.result["version"] = len(job.versions)
        job.status = "done"
        job.stage = None
        job.stage_progress = StageProgress(current=2, total=2, label=f"✅ Editado — versão {len(job.versions)}")
        job_store.update(job)

    except Exception as e:
        import traceback
        job.status = "done"  # volta para done mesmo com erro de edição
        job.stage = None
        job.error = f"Erro na edição: {e}\n{traceback.format_exc()}"
        job_store.update(job)
        print(f"[Editor ERROR - Job {job_id}]", job.error)


async def _decide_tools(instruction: str, creation_type: str) -> list[str]:
    """
    Pergunta ao LLM quais tools são necessárias para essa instrução.
    Retorna lista de nomes de tools a executar.
    """
    available = list_tools()
    tools_desc = "\n".join(f"- {t['name']}: {t['description']}" for t in available)

    prompt = f"""Instrução de edição: "{instruction}"
Tipo de projeto: {creation_type}

Tools disponíveis:
{tools_desc}

Quais tools (se alguma) são necessárias para executar essa instrução?
Responda em JSON: {{"tools": ["tool_name_1", "tool_name_2"]}}
Se nenhuma tool for necessária: {{"tools": []}}
Apenas liste tools realmente necessárias — não exagere."""

    try:
        raw = await call_llm(
            messages=[{"role": "user", "content": prompt}],
            system="Você decide quais tools usar. Responda APENAS em JSON.",
            model_type="build",
            max_tokens=200,
            json_mode=True,
        )
        data = parse_json_response(raw)
        tools = data.get("tools", [])
        # Filtra apenas tools que existem
        return [t for t in tools if get_tool(t)]
    except Exception:
        return []


async def _run_tools(tool_names: list[str], instruction: str, job: Job) -> dict[str, str]:
    """
    Executa cada tool necessária.
    Para image generation, usa o LLM para criar os prompts certos.
    """
    results = {}
    project_summary = job.result.get("project_summary", job.user_request)

    for tool_name in tool_names:
        tool = get_tool(tool_name)
        if not tool:
            continue

        # Pede ao LLM os parâmetros corretos para essa tool
        param_prompt = f"""Tool: {tool_name}
Descrição: {tool.description}
Schema: {json.dumps(tool.parameters_schema, ensure_ascii=False)}

Projeto: {project_summary}
Instrução do usuário: {instruction}

Gere os parâmetros ideais para executar essa tool.
Responda APENAS em JSON com os parâmetros."""

        try:
            raw = await call_llm(
                messages=[{"role": "user", "content": param_prompt}],
                system="Gere parâmetros para a tool. Responda APENAS em JSON.",
                model_type="build",
                max_tokens=500,
                json_mode=True,
            )
            params = parse_json_response(raw)

            # Para image generation, pode precisar gerar múltiplas imagens
            if tool_name == "generate_image" and "images" in instruction.lower():
                # Tenta gerar para cada seção mencionada
                img_results = []
                sections = job.result.get("sections", [])
                if sections:
                    for section in sections[:4]:  # max 4 imagens
                        p = {**params, "prompt": f"{params.get('prompt','')} for {section} section"}
                        res = await tool.execute(p)
                        img_results.append(res)
                    results[tool_name] = json.dumps(img_results)
                else:
                    results[tool_name] = await tool.execute(params)
            else:
                results[tool_name] = await tool.execute(params)

        except Exception as e:
            results[tool_name] = json.dumps({"error": str(e)})

    return results


async def _apply_edit(
    current_content: str,
    instruction: str,
    creation_type: str,
    job: Job,
    tool_results: dict[str, str],
    images: list[dict],
) -> str:
    is_html = creation_type in ("website", "landing_page", "game", "dashboard")

    # Formata resultados das tools para o prompt
    tools_block = ""
    if tool_results:
        parts = []
        for tool_name, result in tool_results.items():
            parts.append(f"=== RESULTADO DA TOOL '{tool_name}' ===\n{result}")
        tools_block = "\n\n".join(parts)

    if is_html:
        format_rule = """REGRAS ABSOLUTAS:
1. Retorne APENAS o HTML completo — comece com <!DOCTYPE html>
2. CSS dentro de <style>, JS dentro de <script>
3. Incorpore os resultados das tools (URLs de imagens, paletas, etc.) diretamente no HTML
4. Mantenha tudo que não foi pedido para mudar
5. Arquivo completo — não truncar"""
    else:
        format_rule = "Retorne o conteúdo completo com as modificações aplicadas. Não truncar."

    # Para HTMLs grandes, envia só um resumo do conteúdo atual + instrução
    content_to_send = current_content
    if len(current_content) > 40000:
        # Envia começo e fim para o modelo ter contexto
        content_to_send = current_content[:20000] + "\n\n[... CONTEÚDO INTERMEDIÁRIO OMITIDO ...]\n\n" + current_content[-10000:]

    tool_section = f"\nRESULTADOS DAS TOOLS (use estas informações):\n{tools_block}\n" if tools_block else ""

    user_prompt = f"""INSTRUÇÃO DE EDIÇÃO: {instruction}

PROJETO: {job.result.get("project_summary", job.user_request)}
TIPO: {creation_type}
VERSÃO ATUAL: {len(job.versions)} (você está criando a versão {len(job.versions) + 1})
{tool_section}
CONTEÚDO ATUAL PARA EDITAR:
{content_to_send}

{format_rule}

Aplique a instrução de edição agora. Comece diretamente com o conteúdo editado."""

    return await call_llm(
        messages=[{"role": "user", "content": user_prompt}],
        system=f"""Você é um editor especialista em {creation_type}.
Sua função é aplicar precisamente a instrução de edição no conteúdo existente.
Mantenha tudo que não foi pedido para mudar. {format_rule}""",
        model_type="build",
        max_tokens=16000,
        images=images if images else None,
        retries=3,
        retry_delay=5.0,
    )

# ══════════════════════════════════════════════════════════════════════════════
# SECTION-MARKED EDIT — edits only a marked section of the document
# Much more compact context: sends only the marked HTML/text + instruction
# ══════════════════════════════════════════════════════════════════════════════
async def run_section_edit_pipeline(
    job_id:          str,
    instruction:     str,
    section_html:    str,     # the marked section's outerHTML
    section_path:    str,     # CSS selector or xpath hint (for context)
    images:          list[dict],
    reference_files: list[dict],  # [{filename, content_b64, mime_type}]
):
    """
    Applies an edit instruction to a specific marked section of the project.
    Uses compact context: only sends the section, not the full document.
    Then surgically replaces the section in the full document.
    """
    job = job_store.get(job_id)
    if not job or not job.result:
        return

    job.save_version(f"[section edit] {instruction[:60]}")
    job.status = "running"
    job.stage  = StageType.EDITING
    job.stage_progress = StageProgress(current=1, total=3, label=f"🎯 Editando seção: {instruction[:50]}...")
    job_store.update(job)

    try:
        creation_type   = job.result.get("creation_type", "other")
        current_content = job.result.get("content", "")

        # ── Step 1: Decide tools ──────────────────────────────────────────
        tools_needed = await _decide_tools(instruction, creation_type)
        tool_results = {}
        if tools_needed:
            job.stage_progress = StageProgress(current=1, total=3, label=f"🔧 Executando tools...")
            job_store.update(job)
            tool_results = await _run_tools(tools_needed, instruction, job)

        # ── Step 2: Build reference context from uploaded files ───────────
        ref_context = ""
        if reference_files:
            parts = []
            for rf in reference_files[:5]:
                fname   = rf.get("filename", "file")
                mime    = rf.get("mime_type", "")
                content = rf.get("content", "")  # text content or description
                if content:
                    parts.append(f"=== REFERENCE FILE: {fname} ===\n{content[:3000]}")
            if parts:
                ref_context = "\n\n".join(parts)

        # ── Step 3: Edit only the section ─────────────────────────────────
        job.stage_progress = StageProgress(current=2, total=3, label="✏️ Reescrevendo seção...")
        job_store.update(job)

        edited_section = await _edit_section(
            section_html=section_html,
            section_path=section_path,
            instruction=instruction,
            creation_type=creation_type,
            job=job,
            tool_results=tool_results,
            images=images,
            ref_context=ref_context,
        )

        # ── Step 4: Splice edited section back into full document ─────────
        job.stage_progress = StageProgress(current=3, total=3, label="🔗 Integrando seção...")
        job_store.update(job)

        new_content = _splice_section(current_content, section_html, edited_section)

        # Validate
        is_html = creation_type in ("website", "landing_page", "game", "dashboard")
        if is_html and not new_content.strip().lower().startswith("<!doctype"):
            idx = new_content.lower().find("<!doctype")
            if idx >= 0:
                new_content = new_content[idx:]

        job.result["content"]    = new_content
        job.result["last_edit"]  = instruction
        job.result["version"]    = len(job.versions)
        job.status = "done"
        job.stage  = None
        job.stage_progress = StageProgress(current=3, total=3, label=f"✅ Seção editada — v{len(job.versions)}")
        job_store.update(job)

    except Exception as e:
        import traceback
        job.status = "done"
        job.stage  = None
        job.error  = f"Erro na edição de seção: {e}\n{traceback.format_exc()}"
        job_store.update(job)
        print(f"[SectionEditor ERROR - Job {job_id}]", job.error)


async def _edit_section(
    section_html: str,
    section_path: str,
    instruction:  str,
    creation_type: str,
    job: "Job",
    tool_results: dict,
    images: list[dict],
    ref_context:  str,
) -> str:
    """Edit only the provided section HTML according to the instruction."""
    is_html = creation_type in ("website", "landing_page", "game", "dashboard")

    tools_block = ""
    if tool_results:
        parts = [f"=== TOOL '{k}' RESULT ===\n{v}" for k, v in tool_results.items()]
        tools_block = "\n\n".join(parts)

    ref_block = f"\n\nREFERENCE FILES (use as inspiration/source):\n{ref_context}" if ref_context else ""
    tool_block = f"\n\nTOOL RESULTS (embed these in the output):\n{tools_block}" if tools_block else ""

    if is_html:
        format_rule = """RULES:
1. Return ONLY the edited HTML for this section — no full document wrapper
2. Keep all existing classes, IDs and structure unless instructed to change them
3. Embed tool results (image URLs, etc.) directly
4. Do not add <!DOCTYPE> or <html>/<body> tags — this is a section only"""
    else:
        format_rule = "Return only the edited section content, nothing else."

    prompt = f"""SECTION EDIT TASK
Project type: {creation_type}
Section selector: {section_path}
Edit instruction: {instruction}
{ref_block}{tool_block}

CURRENT SECTION HTML TO EDIT:
{section_html[:20000]}

{format_rule}

Output the edited section now:"""

    return await call_llm(
        messages=[{"role": "user", "content": prompt}],
        system=f"You are a specialist editor for {creation_type} projects. Edit precisely only what is requested. {format_rule}",
        model_type="build",
        max_tokens=8192,
        images=images if images else None,
        retries=3,
        retry_delay=3.0,
    )


def _splice_section(full_content: str, original_section: str, edited_section: str) -> str:
    """
    Replace the original section in the full document with the edited version.
    Falls back to simple string replacement. For large docs this is efficient
    since we only need to find+replace one section.
    """
    # Clean edited section (strip markdown fences if any)
    edited = edited_section.strip()
    for fence in ("```html", "```"):
        if edited.lower().startswith(fence):
            edited = edited[len(fence):].lstrip("\n")
            break
    if edited.endswith("```"):
        edited = edited[:-3].rstrip()

    # Try exact replacement first
    if original_section in full_content:
        return full_content.replace(original_section, edited, 1)

    # Fallback: try with normalized whitespace on a trimmed version
    orig_stripped = original_section.strip()
    if orig_stripped in full_content:
        return full_content.replace(orig_stripped, edited, 1)

    # Last resort: return full content unchanged but log issue
    print("[SectionEditor] WARNING: could not find original section for splice — returning unchanged")
    return full_content