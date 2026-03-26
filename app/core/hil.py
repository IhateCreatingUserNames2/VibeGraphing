"""
╔══════════════════════════════════════════════════════════════╗
║  HUMAN IN THE LOOP (HIL)                                     ║
║                                                              ║
║  Permite pausar o pipeline em qualquer estágio para:         ║
║  - Visualizar o prompt que será enviado                      ║
║  - Editar o prompt antes de enviar                           ║
║  - Aprovar/rejeitar outputs dos agentes                      ║
║  - Injetar sugestões e contexto extra                        ║
║  - Parar completamente o loop                                ║
╚══════════════════════════════════════════════════════════════╝
"""

import asyncio
from typing import Optional
from app.core.models import Job, job_store


# Timeout para aguardar resolução humana (30 minutos)
HIL_TIMEOUT = int(30 * 60)
HIL_POLL    = 1.0   # segundos entre verificações


async def hil_gate(
    job:        Job,
    stage:      str,
    agent_id:   str,
    title:      str,
    prompt:     str,
    context:    dict = {},
) -> tuple[str, str]:
    """
    Cria um checkpoint HIL e bloqueia até o humano resolver.

    Retorna (prompt_final, resolution) onde:
    - prompt_final: prompt original ou editado pelo humano
    - resolution: "approve" | "modify" | "skip" | "stop"

    Se "stop": o caller deve interromper o pipeline.
    Se "skip": o caller deve pular este agente.
    Se "modify": usa modified_prompt e/ou injeta user_note.
    """
    if not job.hil_enabled:
        return prompt, "approve"

    # Verifica se este stage está configurado para pausar
    hil_stages = job.hil_stages or ["all"]
    if "all" not in hil_stages and stage not in hil_stages:
        return prompt, "approve"

    # Cria checkpoint e muda status para PAUSED
    cp_id = job.add_checkpoint(stage=stage, agent_id=agent_id,
                                title=title, prompt=prompt, context=context)
    job.status = "paused"
    job.add_log("hil", f"⏸️ Aguardando aprovação: {title}",
                f"Stage: {stage} | Agent: {agent_id}", stage=stage, agent_id=agent_id)
    job_store.update(job)

    # Polling até resolução ou timeout
    elapsed = 0
    while elapsed < HIL_TIMEOUT:
        await asyncio.sleep(HIL_POLL)
        elapsed += HIL_POLL

        # Recarrega do store (resolução vem de outro request HTTP)
        fresh = job_store.get(job.id)
        if not fresh:
            return prompt, "stop"

        cp = fresh.get_checkpoint(cp_id)
        if cp and cp.get("resolved"):
            resolution = cp.get("resolution", "approve")
            job.status = "running"
            # Atualiza job local com dados resolvidos
            for attr in ["logs","checkpoints","active_checkpoint_id","hil_enabled","hil_stages"]:
                setattr(job, attr, getattr(fresh, attr))
            job.status = "running"
            job_store.update(job)

            if resolution == "modify" and cp.get("modified_prompt"):
                note = cp.get("user_note", "")
                final_prompt = cp["modified_prompt"]
                if note:
                    final_prompt += f"\n\n[NOTA DO REVISOR]: {note}"
                return final_prompt, "modify"
            elif resolution == "skip":
                return prompt, "skip"
            elif resolution == "stop":
                return prompt, "stop"
            else:
                # approve — pode ter nota adicional
                final = prompt
                note = cp.get("user_note","")
                if note:
                    final += f"\n\n[CONTEXTO ADICIONAL DO REVISOR]: {note}"
                return final, "approve"

    # Timeout — continua com prompt original
    job.add_log("hil", f"⏱️ HIL timeout para '{title}' — continuando automaticamente",
                stage=stage, agent_id=agent_id)
    job.status = "running"
    job_store.update(job)
    return prompt, "approve"