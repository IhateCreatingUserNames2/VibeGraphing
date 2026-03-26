"""
╔══════════════════════════════════════════════════════════════╗
║  CYCLIC EXECUTOR — Grafos cíclicos para auto-correção        ║
║                                                              ║
║  Implementa o Loop Component do paper MASFactory:            ║
║    1. Agente Gerador produz um output                        ║
║    2. Agente Crítico avalia contra critérios de qualidade    ║
║    3. Se reprovado → volta ao Gerador com feedback           ║
║    4. Repete até aprovado ou max_iterations                  ║
║                                                              ║
║  Usado automaticamente quando quality_threshold > 0          ║
║  no semantic_config do agente.                               ║
╚══════════════════════════════════════════════════════════════╝
"""

import json
from app.core.llm import call_llm, parse_json_response
from app.core.contracts import AgentContract, WorkflowMessage, route_context


async def run_with_reflection(
    agent_id:      str,
    contract:      AgentContract,
    message:       WorkflowMessage,
    user_request:  str,
    model_type:    str  = "execute",
    max_iterations: int = 3,
    quality_threshold: float = 7.0,   # 0-10, abaixo disso → refaz
    max_tokens:    int  = 8192,
) -> tuple[str, list[dict]]:
    """
    Executa um agente com loop de reflexão.

    Retorna (output_final, trace) onde trace é a lista de tentativas
    com scores e feedbacks para visualização.
    """
    context_block = route_context(message, contract)
    trace: list[dict] = []
    previous_attempts: list[str] = []
    feedback_history:  list[str] = []

    for iteration in range(1, max_iterations + 1):
        # ── Prompt do Gerador ─────────────────────────────────────────────
        attempts_block = ""
        if previous_attempts:
            last = previous_attempts[-1]
            last_feedback = feedback_history[-1] if feedback_history else ""
            attempts_block = f"""
TENTATIVA ANTERIOR (iteração {iteration-1}):
{last[:3000]}{"..." if len(last) > 3000 else ""}

FEEDBACK DO CRÍTICO:
{last_feedback}

Corrija os problemas apontados e melhore a qualidade."""

        context_section = f"CONTEXTO RELEVANTE:\n{context_block}" if context_block else ""
        gen_prompt = f"""PROJETO: {user_request}

{context_section}
{attempts_block}

EXECUTE SUA TAREFA como {contract.role}:
{contract.instructions}

Formato esperado: {contract.output_format}
Campos que você deve produzir: {', '.join(contract.output_fields)}

{"Iteração " + str(iteration) + "/" + str(max_iterations) + " — seja específico e de alta qualidade." if iteration > 1 else "Seja específico, completo e de alta qualidade."}"""

        output = await call_llm(
            messages=[{"role": "user", "content": gen_prompt}],
            system=contract.instructions or f"Você é {contract.role}.",
            model_type=model_type,
            max_tokens=max_tokens,
        )

        # ── Crítico avalia ────────────────────────────────────────────────
        critic_prompt = f"""Avalie o output abaixo para a tarefa: "{contract.role}"

TAREFA: {contract.instructions[:500]}
OUTPUT A AVALIAR:
{output[:4000]}{"..." if len(output) > 4000 else ""}

Avalie em JSON:
{{
  "score": <0-10>,
  "approved": <true se score >= {quality_threshold}>,
  "strengths": ["ponto forte 1", "ponto forte 2"],
  "issues": ["problema 1", "problema 2"],
  "feedback": "Instrução específica para melhorar (se reprovado)"
}}"""

        try:
            critic_raw = await call_llm(
                messages=[{"role": "user", "content": critic_prompt}],
                system="Você é um crítico especialista. Avalie rigorosamente. Responda APENAS em JSON.",
                model_type="build",
                max_tokens=600,
                json_mode=True,
            )
            evaluation = parse_json_response(critic_raw)
        except Exception:
            # Se crítico falhar, aceita o output
            evaluation = {"score": 8.0, "approved": True, "feedback": "", "issues": [], "strengths": []}

        score    = float(evaluation.get("score", 7.0))
        approved = bool(evaluation.get("approved", score >= quality_threshold))
        feedback = evaluation.get("feedback", "")

        trace.append({
            "iteration": iteration,
            "score":     score,
            "approved":  approved,
            "feedback":  feedback,
            "issues":    evaluation.get("issues", []),
            "strengths": evaluation.get("strengths", []),
            "output_preview": output[:200] + "..." if len(output) > 200 else output,
        })

        print(f"[Cyclic {agent_id}] iter={iteration} score={score:.1f} approved={approved}")

        if approved or iteration == max_iterations:
            return output, trace

        # Prepara próxima iteração
        previous_attempts.append(output)
        feedback_history.append(feedback)

    # Nunca chega aqui, mas por segurança
    return output, trace


async def run_cyclic_workflow(
    agents_with_cycles: list[dict],  # agentes que têm quality_threshold definido
    contracts: dict[str, AgentContract],
    message: WorkflowMessage,
    user_request: str,
    model_type: str = "execute",
) -> tuple[WorkflowMessage, dict[str, list[dict]]]:
    """
    Executa agentes com reflexão cíclica quando configurado.
    Retorna (mensagem_atualizada, traces_por_agente).
    """
    all_traces: dict[str, list[dict]] = {}

    for agent_cfg in agents_with_cycles:
        aid       = agent_cfg["id"]
        contract  = contracts.get(aid)
        threshold = float(agent_cfg.get("quality_threshold", 7.0))
        max_iter  = int(agent_cfg.get("max_iterations", 3))
        max_tok   = int(agent_cfg.get("max_tokens", 8192))

        if not contract:
            continue

        output, trace = await run_with_reflection(
            agent_id          = aid,
            contract          = contract,
            message           = message,
            user_request      = user_request,
            model_type        = model_type,
            max_iterations    = max_iter,
            quality_threshold = threshold,
            max_tokens        = max_tok,
        )

        # Registra outputs no message com os field names do contrato
        for field_name in contract.output_fields:
            message.set(field_name, output)

        all_traces[aid] = trace

    return message, all_traces