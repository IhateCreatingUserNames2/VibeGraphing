"""
╔══════════════════════════════════════════════════════════╗
║  BUILD CACHE — inspirado no build_cache_path do paper    ║
║                                                          ║
║  Salva em disco os 3 estágios de setup do Deep Mode:     ║
║    Stage1: Role Assignment                               ║
║    Stage2: Topology Design                               ║
║    Stage3: Semantic Completion                           ║
║                                                          ║
║  Chave: hash(creation_type + style + request_summary)    ║
║  Se o mesmo tipo de projeto for criado novamente,        ║
║  pula os ~3min de setup e vai direto ao Runtime.         ║
╚══════════════════════════════════════════════════════════╝
"""

import os
import json
import hashlib
import time
from pathlib import Path
from typing import Optional

CACHE_DIR = Path(os.getenv("MAS_CACHE_DIR", "./mas_cache"))
CACHE_TTL  = int(os.getenv("MAS_CACHE_TTL", str(7 * 24 * 3600)))  # 7 dias


def _cache_key(creation_type: str, style: str, request: str) -> str:
    """
    Gera chave de cache baseada no tipo + estilo + primeiras palavras do pedido.
    Dois pedidos do mesmo tipo/estilo compartilham a mesma estrutura de agentes.
    Ex: "website" + "elegante" → mesmo Role Assignment para qualquer website elegante.
    """
    # Normaliza o request para as primeiras 8 palavras (captura o domínio, ignora detalhes)
    words = request.lower().split()[:8]
    fingerprint = f"{creation_type.lower()}|{style.lower()}|{' '.join(words)}"
    return hashlib.sha256(fingerprint.encode()).hexdigest()[:16]


def _cache_path(key: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{key}.json"


def cache_load(creation_type: str, style: str, request: str) -> Optional[dict]:
    """
    Tenta carregar cache dos 3 estágios.
    Retorna None se não existir ou estiver expirado.
    """
    key  = _cache_key(creation_type, style, request)
    path = _cache_path(key)

    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        age  = time.time() - data.get("saved_at", 0)
        if age > CACHE_TTL:
            path.unlink(missing_ok=True)
            print(f"[Cache] Expirado ({age/3600:.1f}h) — key={key[:8]}")
            return None
        print(f"[Cache] HIT — type={creation_type} key={key[:8]} age={age/3600:.1f}h")
        return data["stages"]
    except Exception as e:
        print(f"[Cache] Erro ao ler: {e}")
        return None


def cache_save(creation_type: str, style: str, request: str, stages: dict) -> None:
    """Salva os 3 estágios em disco."""
    key  = _cache_key(creation_type, style, request)
    path = _cache_path(key)
    try:
        path.write_text(
            json.dumps({"saved_at": time.time(), "creation_type": creation_type,
                        "style": style, "stages": stages}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"[Cache] SAVED — type={creation_type} key={key[:8]}")
    except Exception as e:
        print(f"[Cache] Erro ao salvar: {e}")


def cache_list() -> list[dict]:
    """Lista todos os caches salvos."""
    if not CACHE_DIR.exists():
        return []
    entries = []
    for f in CACHE_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            age  = time.time() - data.get("saved_at", 0)
            entries.append({
                "key":           f.stem,
                "creation_type": data.get("creation_type", "?"),
                "style":         data.get("style", ""),
                "age_hours":     round(age / 3600, 1),
                "expired":       age > CACHE_TTL,
            })
        except Exception:
            continue
    return sorted(entries, key=lambda x: x["age_hours"])


def cache_clear(key: Optional[str] = None) -> int:
    """Limpa cache. Se key=None, limpa tudo."""
    if not CACHE_DIR.exists():
        return 0
    count = 0
    for f in CACHE_DIR.glob("*.json"):
        if key is None or f.stem == key:
            f.unlink(missing_ok=True)
            count += 1
    return count