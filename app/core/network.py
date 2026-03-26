"""
╔══════════════════════════════════════════════════════════════╗
║  AGENT NETWORK — Rede de Agentes e Projetos                  ║
║                                                              ║
║  Permite que um novo pipeline:                               ║
║  1. Use agentes de jobs anteriores como exemplos/templates   ║
║  2. Herde configurações (roles, topology, semantics)         ║
║  3. Receba outputs de outros projetos como contexto RAG      ║
║  4. Conecte-se a uma "rede" de agentes especializados        ║
╚══════════════════════════════════════════════════════════════╝
"""

from typing import Optional
from app.core.models import job_store


def get_network_context(job_ids: list[str], max_chars: int = 30000) -> str:
    """
    Busca outputs e configurações de jobs anteriores.
    Retorna string de contexto para injetar no pipeline.
    """
    if not job_ids:
        return ""

    parts = []
    used  = 0
    per   = max(3000, max_chars // max(len(job_ids), 1))

    for jid in job_ids:
        job = job_store.get(jid)
        if not job or not job.result:
            continue

        ra      = job.role_assignment or {}
        content = job.result.get("content", "")
        summary = job.result.get("project_summary", job.user_request[:80])
        ctype   = job.result.get("creation_type", "?")
        agents  = list(job.agent_outputs.keys()) if job.agent_outputs else []

        header = f"""=== PROJETO REFERÊNCIA [{jid[:8]}] ===
Tipo: {ctype} | Sumário: {summary}
Estilo: {ra.get('detected_style','')} | Agentes: {', '.join(agents)}
"""
        # Adiciona trecho do resultado como exemplo
        snippet = content[:per - len(header)]
        block = header + snippet
        if used + len(block) > max_chars:
            break
        parts.append(block)
        used += len(block)

    return "\n\n".join(parts)


def get_network_agents(job_ids: list[str]) -> list[dict]:
    """
    Extrai definições de agentes de jobs anteriores para reusar como templates.
    """
    agents_seen = {}
    for jid in job_ids:
        job = job_store.get(jid)
        if not job or not job.role_assignment:
            continue
        for agent in job.role_assignment.get("agents", []):
            aid = agent.get("id","")
            if aid and aid not in agents_seen:
                agents_seen[aid] = {
                    **agent,
                    "from_job": jid[:8],
                    "source":   "network",
                }
    return list(agents_seen.values())


def build_network_prompt_block(job_ids: list[str]) -> str:
    """
    Bloco pronto para injetar em prompts do pipeline.
    """
    if not job_ids:
        return ""

    ctx     = get_network_context(job_ids, max_chars=20000)
    agents  = get_network_agents(job_ids)

    if not ctx and not agents:
        return ""

    block = "=== REDE DE AGENTES E PROJETOS CONECTADOS ===\n"
    if agents:
        block += f"Agentes disponíveis na rede: {', '.join(a['id'] for a in agents[:8])}\n"
        for a in agents[:5]:
            block += f"  • {a['id']} ({a['role']}): {a.get('responsibility','')[:80]}\n"
    if ctx:
        block += f"\nCONTEXTO DOS PROJETOS CONECTADOS:\n{ctx[:15000]}"

    return block