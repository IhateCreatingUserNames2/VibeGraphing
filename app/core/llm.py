"""
Core LLM Client — OpenRouter
Usa STREAMING para evitar RemoteProtocolError em respostas longas.
Modelos com contexto grande (150k/190k) funcionam sem corte de contexto.
"""

import os
import re
import json
import asyncio
import httpx
from dotenv import load_dotenv

load_dotenv()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

MODELS = {
    "build":   os.getenv("MAS_BUILD_MODEL",   "anthropic/claude-sonnet-4-5"),
    "execute": os.getenv("MAS_EXECUTE_MODEL", "anthropic/claude-haiku-4-5"),
    "vision":  os.getenv("MAS_VISION_MODEL",  "anthropic/claude-sonnet-4-5"),
}

_RETRYABLE = (
    httpx.RemoteProtocolError,
    httpx.ReadTimeout,
    httpx.ConnectTimeout,
    httpx.ReadError,
    httpx.ConnectError,
)


async def _call_streaming(
    payload: dict,
    headers: dict,
    timeout: httpx.Timeout,
) -> str:
    """
    Faz a chamada via SSE streaming e monta a resposta completa.
    Evita RemoteProtocolError porque os tokens chegam incrementalmente —
    a conexao fica ativa e nunca fica 'parada' tempo suficiente para dropar.
    """
    chunks = []

    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST",
            f"{OPENROUTER_BASE_URL}/chat/completions",
            json={**payload, "stream": True},
            headers=headers,
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                raise RuntimeError(
                    f"OpenRouter erro {resp.status_code}: {body.decode()[:500]}"
                )

            async for line in resp.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data_str = line[6:]  # remove "data: "
                if data_str.strip() == "[DONE]":
                    break
                try:
                    data = json.loads(data_str)
                    delta = data["choices"][0].get("delta", {})
                    token = delta.get("content")
                    if token:
                        chunks.append(token)
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue  # chunk malformado — ignora

    return "".join(chunks)


async def call_llm(
    messages: list[dict],
    system: str = "",
    model_type: str = "execute",
    max_tokens: int = 180192,
    images: list[dict] | None = None,
    json_mode: bool = False,
    retries: int = 3,
    retry_delay: float = 5.0,
) -> str:
    if not OPENROUTER_API_KEY:
        raise ValueError("OPENROUTER_API_KEY nao configurada. Adicione no arquivo .env")

    model = MODELS["vision"] if images else MODELS[model_type]

    # Injeta imagens no ultimo user message
    if images and messages:
        last_msg = messages[-1]
        if last_msg["role"] == "user":
            img_content = []
            for img in images:
                img_content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{img['media_type']};base64,{img['data']}"}
                })
            img_content.append({
                "type": "text",
                "text": last_msg["content"] if isinstance(last_msg["content"], str) else str(last_msg["content"])
            })
            messages = messages[:-1] + [{"role": "user", "content": img_content}]

    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.7,
    }

    if system:
        payload["messages"] = [{"role": "system", "content": system}] + payload["messages"]

    # json_mode desativa streaming (incompativel com alguns modelos no OpenRouter)
    use_streaming = not json_mode

    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://localhost:8000",
        "X-Title": "MAS Creator",
    }

    # Timeout: connect rapido, read generoso (10 min para modelos lentos)
    timeout = httpx.Timeout(connect=30.0, read=600.0, write=60.0, pool=10.0)

    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            if use_streaming:
                result = await _call_streaming(payload, headers, timeout)
                if result:
                    return result
                # Se streaming retornou vazio, tenta sem streaming
                use_streaming = False
                continue

            else:
                # Fallback: chamada normal (usada com json_mode=True)
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(
                        f"{OPENROUTER_BASE_URL}/chat/completions",
                        json=payload,
                        headers=headers,
                    )

                if resp.status_code == 429:
                    wait = retry_delay * attempt * 2
                    print(f"[LLM] Rate limit. Aguardando {wait}s (tentativa {attempt}/{retries})")
                    await asyncio.sleep(wait)
                    last_exc = RuntimeError("Rate limit (429)")
                    continue

                if resp.status_code >= 500:
                    wait = retry_delay * attempt
                    print(f"[LLM] Erro {resp.status_code}. Retry {attempt}/{retries} em {wait}s")
                    await asyncio.sleep(wait)
                    last_exc = RuntimeError(f"Erro servidor {resp.status_code}")
                    continue

                if resp.status_code != 200:
                    raise RuntimeError(f"OpenRouter erro {resp.status_code}: {resp.text[:500]}")

                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                if content is None:
                    finish = data["choices"][0].get("finish_reason", "?")
                    raise RuntimeError(f"content=null, finish_reason={finish}")
                return content

        except _RETRYABLE as e:
            last_exc = e
            wait = retry_delay * attempt
            print(f"[LLM] Conexao perdida: {type(e).__name__}. Retry {attempt}/{retries} em {wait}s")
            await asyncio.sleep(wait)
            # Na proxima tentativa, desativa streaming (pode ser o causador)
            use_streaming = False
            continue

        except RuntimeError:
            raise

    raise RuntimeError(f"Falhou apos {retries} tentativas. Ultimo erro: {last_exc}")


def parse_json_response(raw: str) -> dict:
    """Parser JSON robusto — lida com fences, texto extra, multiplos objetos."""
    text = raw.strip()

    # Remove markdown fences
    fence_match = re.search(r'```(?:json|JSON)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if fence_match:
        candidate = fence_match.group(1).strip()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    # Tenta o texto todo
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Extrai primeiro objeto { } por contagem de chaves (resolve "Extra data")
    start = None
    depth = 0
    in_string = False
    escape_next = False

    for i, ch in enumerate(text):
        if escape_next:
            escape_next = False
            continue
        if ch == '\\' and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start is not None:
                candidate = text[start:i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    start = None

    # Regex ultimo recurso
    for match in re.finditer(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL):
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            continue

    preview = text[:400] + ("..." if len(text) > 400 else "")
    raise ValueError(f"Nao foi possivel extrair JSON.\nPrevia:\n{preview}")


async def list_available_models() -> list[dict]:
    if not OPENROUTER_API_KEY:
        return []
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        resp = await client.get(
            f"{OPENROUTER_BASE_URL}/models",
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
        )
        if resp.status_code == 200:
            return resp.json().get("data", [])
    return []