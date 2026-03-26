"""
Models e Job Store — com suporte a logs, HIL (Human in the Loop) e agent network
"""

from pydantic import BaseModel, Field
from typing import Any, Optional
from datetime import datetime
from enum import Enum
import uuid


class JobStatus(str, Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    PAUSED    = "paused"     # Human-in-the-loop pause
    DONE      = "done"
    ERROR     = "error"


class StageType(str, Enum):
    ROLE_ASSIGNMENT     = "role_assignment"
    TOPOLOGY_DESIGN     = "topology_design"
    SEMANTIC_COMPLETION = "semantic_completion"
    GENERATING          = "generating"
    EDITING             = "editing"
    TOOL_RUNNING        = "tool_running"
    WAITING_HUMAN       = "waiting_human"


class StageProgress(BaseModel):
    current: int = 0
    total:   int = 4
    label:   str = ""


# ── Log entry — cada evento do pipeline ──────────────────────────────────────
class LogEntry(BaseModel):
    id:         str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    timestamp:  str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    level:      str = "info"    # info | prompt | output | tool | hil | error | graph
    stage:      str = ""
    agent_id:   str = ""
    title:      str = ""
    content:    str = ""        # Prompt completo, output, tool result, etc.
    meta:       dict = {}       # tokens, model, duração, etc.


# ── HIL Checkpoint — ponto de pausa ──────────────────────────────────────────
class HILCheckpoint(BaseModel):
    id:           str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    timestamp:    str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    stage:        str = ""
    agent_id:     str = ""
    title:        str = ""
    prompt:       str = ""      # prompt que será enviado (editável)
    output:       str = ""      # output atual (editável, se já rodou)
    context:      dict = {}     # contexto disponível neste ponto
    resolved:     bool = False
    resolution:   str = ""      # approve | modify | skip | stop
    user_note:    str = ""
    modified_prompt: Optional[str] = None
    modified_output: Optional[str] = None


class Job(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))

    # Status
    status:         JobStatus          = JobStatus.PENDING
    stage:          Optional[StageType] = None
    stage_progress: StageProgress      = Field(default_factory=StageProgress)

    # Input original
    user_request:       str = ""
    creation_type_hint: str = ""
    style_hint:         str = ""
    has_images:         bool = False
    image_count:        int  = 0

    # Config de execução
    hil_enabled:     bool = False    # Human-in-the-loop ativo
    hil_stages:      list[str] = []  # quais stages pausam: ["all","role_assignment","generating",...]
    agent_network:   list[str] = []  # IDs de jobs/agentes usados como referência

    # Pipeline intermediates
    role_assignment: Optional[dict] = None
    topology:        Optional[dict] = None
    semantic_config: Optional[dict] = None
    agent_outputs:   dict[str, str] = {}

    # Logs e checkpoints
    logs:        list[dict] = []      # serialized LogEntry list
    checkpoints: list[dict] = []      # serialized HILCheckpoint list
    active_checkpoint_id: Optional[str] = None   # ID do checkpoint aguardando resolução

    # Resultado final
    result: Optional[dict] = None
    error:  Optional[str]  = None

    # Histórico de versões
    versions: list[dict] = []

    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())

    def touch(self):
        self.updated_at = datetime.utcnow().isoformat()

    def add_log(self, level: str, title: str, content: str = "",
                stage: str = "", agent_id: str = "", meta: dict = {}):
        entry = {
            "id":        str(uuid.uuid4())[:8],
            "timestamp": datetime.utcnow().isoformat(),
            "level":     level,
            "stage":     stage,
            "agent_id":  agent_id,
            "title":     title,
            "content":   content,
            "meta":      meta,
        }
        self.logs.append(entry)
        print(f"[LOG {level.upper()}] [{stage}]{(' ['+agent_id+']') if agent_id else ''} {title}")

    def add_checkpoint(self, stage: str, agent_id: str, title: str,
                       prompt: str, context: dict = {}) -> str:
        cp = {
            "id":        str(uuid.uuid4())[:8],
            "timestamp": datetime.utcnow().isoformat(),
            "stage":     stage,
            "agent_id":  agent_id,
            "title":     title,
            "prompt":    prompt,
            "output":    "",
            "context":   context,
            "resolved":  False,
            "resolution": "",
            "user_note":  "",
            "modified_prompt": None,
            "modified_output": None,
        }
        self.checkpoints.append(cp)
        self.active_checkpoint_id = cp["id"]
        return cp["id"]

    def resolve_checkpoint(self, cp_id: str, resolution: str,
                           user_note: str = "", modified_prompt: str = None,
                           modified_output: str = None):
        for cp in self.checkpoints:
            if cp["id"] == cp_id:
                cp["resolved"]         = True
                cp["resolution"]       = resolution
                cp["user_note"]        = user_note
                cp["modified_prompt"]  = modified_prompt
                cp["modified_output"]  = modified_output
                break
        if self.active_checkpoint_id == cp_id:
            self.active_checkpoint_id = None

    def get_checkpoint(self, cp_id: str) -> Optional[dict]:
        return next((c for c in self.checkpoints if c["id"] == cp_id), None)

    def save_version(self, instruction: str = ""):
        if self.result:
            self.versions.append({
                "version":     len(self.versions) + 1,
                "timestamp":   datetime.utcnow().isoformat(),
                "content":     self.result.get("content", ""),
                "instruction": instruction,
            })


class JobStore:
    def __init__(self):
        self._jobs: dict[str, Job] = {}

    def create(self, **kwargs) -> Job:
        job = Job(**kwargs)
        self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def update(self, job: Job):
        job.touch()
        self._jobs[job.id] = job

    def list_recent(self, limit: int = 30) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)[:limit]


job_store = JobStore()