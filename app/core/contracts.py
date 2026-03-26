"""
╔══════════════════════════════════════════════════════════╗
║  INPUT/OUTPUT CONTRACTS — inspirado no paper MASFactory  ║
║                                                          ║
║  Cada agente declara explicitamente:                     ║
║    input_fields:  o que ele precisa receber              ║
║    output_fields: o que ele vai produzir                 ║
║                                                          ║
║  Isso substitui passar strings brutas entre agentes.     ║
║  O context router entrega só o que cada agente precisa.  ║
╚══════════════════════════════════════════════════════════╝
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentContract:
    """Contrato de I/O de um agente."""
    agent_id:      str
    role:          str
    input_fields:  list[str]        # campos que este agente consome
    output_fields: list[str]        # campos que este agente produz
    instructions:  str = ""
    output_format: str = "text"


@dataclass
class WorkflowMessage:
    """
    Mensagem tipada que flui entre agentes.
    Substitui o dict[str, str] bruto de contexto.
    """
    fields: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.fields.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.fields[key] = value

    def merge(self, other: "WorkflowMessage") -> "WorkflowMessage":
        return WorkflowMessage(fields={**self.fields, **other.fields})

    def slice_for(self, contract: AgentContract) -> "WorkflowMessage":
        """Retorna apenas os campos que este agente declarou precisar."""
        return WorkflowMessage(
            fields={k: v for k, v in self.fields.items() if k in contract.input_fields}
        )

    def to_context_block(self, contract: AgentContract) -> str:
        """
        Formata o contexto para injetar no prompt do agente.
        Só inclui os input_fields declarados — nada a mais.
        """
        relevant = self.slice_for(contract)
        if not relevant.fields:
            return ""
        parts = []
        for field_name, value in relevant.fields.items():
            if value:
                parts.append(f"=== {field_name.upper()} ===\n{value}")
        return "\n\n".join(parts)


def build_contracts_from_semantics(
    role_assignment: dict,
    semantics: dict,
) -> dict[str, AgentContract]:
    """
    Constrói contratos a partir do role_assignment + semantic_config.
    Se o LLM não retornou input/output_fields explícitos,
    infere a partir das dependências do topology.
    """
    contracts: dict[str, AgentContract] = {}
    agent_configs = semantics.get("agent_configs", {})

    for agent in role_assignment.get("agents", []):
        aid     = agent["id"]
        config  = agent_configs.get(aid, {})

        # Tenta usar os campos do LLM; fallback para inferência
        in_fields  = config.get("input_fields")
        out_fields = config.get("output_fields")

        if not in_fields:
            # Infere: todo agente consome o output dos anteriores + user_request
            in_fields = ["user_request"]

        if not out_fields:
            # Infere: produz um campo com seu próprio id
            out_fields = [aid]

        contracts[aid] = AgentContract(
            agent_id      = aid,
            role          = agent.get("role", aid),
            input_fields  = in_fields,
            output_fields = out_fields,
            instructions  = config.get("system_prompt", agent.get("responsibility", "")),
            output_format = config.get("output_format", "text"),
        )

    return contracts


def route_context(
    message: WorkflowMessage,
    contract: AgentContract,
    max_chars: int = 80000,
) -> str:
    """
    Roteamento de contexto com contracts.
    Retorna string formatada para injetar no prompt — só os campos relevantes,
    truncado ao budget.
    """
    block = message.to_context_block(contract)
    if len(block) > max_chars:
        block = block[:max_chars] + f"\n[... truncado — {len(block)-max_chars} chars omitidos]"
    return block