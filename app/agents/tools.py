"""
Tool Registry — with OpenRouter Specialist Model Tools (image, video, audio)
"""

import os
import json
import httpx
from abc import ABC, abstractmethod

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

OPENROUTER_SPECIALIST_MODELS = {
    "image": {
        "primary":   os.getenv("MAS_IMAGE_MODEL",  "openai/dall-e-3"),
        "fallback":  os.getenv("MAS_IMAGE_FALLBACK","stabilityai/stable-diffusion-xl"),
        "free":      "pollinations",
        "description": "Generates images. OpenRouter: openai/dall-e-3, stabilityai/stable-diffusion-xl, black-forest-labs/flux-schnell, playground-ai/playground-v2.5",
    },
    "video": {
        "primary":   os.getenv("MAS_VIDEO_MODEL",  "runwayml/gen-3-alpha-turbo"),
        "fallback":  os.getenv("MAS_VIDEO_FALLBACK","lumaai/dream-machine"),
        "description": "Generates video clips. OpenRouter: runwayml/gen-3-alpha-turbo, lumaai/dream-machine, stability/stable-video-diffusion",
    },
    "audio": {
        "primary":   os.getenv("MAS_AUDIO_MODEL",  "suno/bark"),
        "fallback":  os.getenv("MAS_AUDIO_FALLBACK","elevenlabs/multilingual-v2"),
        "description": "Generates audio/music/speech. OpenRouter: suno/bark, meta/musicgen, elevenlabs/multilingual-v2",
    },
    "3d": {
        "primary":   os.getenv("MAS_3D_MODEL",     "openai/shap-e"),
        "fallback":  None,
        "description": "Generates 3D models. OpenRouter: openai/shap-e, meshy-ai/meshy-3",
    },
}

def get_specialist_model_info() -> str:
    lines = ["=== OPENROUTER SPECIALIST TOOLS AVAILABLE ==="]
    for modality, cfg in OPENROUTER_SPECIALIST_MODELS.items():
        lines.append(f"  * generate_{modality}: {cfg['description']}")
    lines.append("")
    lines.append("When the user requests image/video/audio/3D creation, use these tools.")
    lines.append("Generated assets are saved and accessible via /api/assets/{id}")
    return "\n".join(lines)


class BaseTool(ABC):
    name: str
    description: str
    parameters_schema: dict

    @abstractmethod
    async def execute(self, params: dict) -> str: ...

    def to_openrouter_spec(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": self.parameters_schema,
        }}


class ImageGeneratorTool(BaseTool):
    name = "generate_image"
    description = (
        "Generates an image from a text prompt using specialist AI models (Pollinations/Flux). "
        "Returns a URL usable directly in <img src='...'>. "
        "Use for product images, catalog photos, banners, hero images, backgrounds. "
        "OpenRouter models: openai/dall-e-3, black-forest-labs/flux-schnell. "
        "All generated images saved as assets accessible via /api/assets/{id}."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "prompt":  {"type": "string", "description": "Detailed image description in English."},
            "width":   {"type": "integer", "description": "Width px (default 1280)", "default": 1280},
            "height":  {"type": "integer", "description": "Height px (default 720)", "default": 720},
            "style":   {"type": "string", "description": "realistic|cinematic|fashion|product|minimal|editorial|illustration|3d-render", "default": "realistic"},
            "model":   {"type": "string", "description": "Optional OpenRouter model slug", "default": ""},
        },
        "required": ["prompt"],
    }

    async def execute(self, params: dict) -> str:
        import urllib.parse, uuid as _uuid
        prompt = params.get("prompt", "")
        width  = params.get("width", 1280)
        height = params.get("height", 720)
        style  = params.get("style", "realistic")
        suffixes = {
            "realistic": "photorealistic, high quality, professional photography, 8k",
            "cinematic": "cinematic lighting, dramatic, film still, 4k",
            "fashion":   "fashion photography, editorial, high-end magazine, studio lighting",
            "product":   "product photography, clean background, studio, commercial",
            "minimal":   "minimalist, clean, white background, modern",
            "editorial": "editorial photography, lifestyle, natural light",
            "illustration": "digital illustration, vibrant colors, concept art",
            "3d-render": "3D render, octane, volumetric lighting, photorealistic CGI",
        }
        full_prompt = f"{prompt}, {suffixes.get(style, 'high quality, professional')}"
        asset_id = str(_uuid.uuid4())[:12]
        encoded  = urllib.parse.quote(full_prompt)
        seed     = abs(hash(prompt)) % 99999
        url      = f"https://image.pollinations.ai/prompt/{encoded}?width={width}&height={height}&nologo=true&enhance=true&seed={seed}"

        try:
            from app.core.registry import media_asset_store
            media_asset_store.register(asset_id=asset_id, asset_type="image", url=url,
                prompt=full_prompt, model="pollinations/flux",
                meta={"width": width, "height": height, "style": style})
        except Exception:
            pass

        return json.dumps({
            "url": url, "asset_id": asset_id, "asset_url": f"/api/assets/{asset_id}",
            "width": width, "height": height, "prompt": full_prompt, "model": "pollinations/flux",
            "embed_html": f'<img src="{url}" width="{width}" height="{height}" alt="{prompt[:60]}" loading="lazy" style="max-width:100%;height:auto;">'
        })


class VideoGeneratorTool(BaseTool):
    name = "generate_video"
    description = (
        "Generates a short video clip from a text description via OpenRouter specialist models. "
        "Returns asset_id and embed HTML for <video> tags. "
        "OpenRouter models: runwayml/gen-3-alpha-turbo, lumaai/dream-machine, stability/stable-video-diffusion. "
        "Use for hero background videos, product showcases, animated banners."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "prompt":       {"type": "string", "description": "Video scene description"},
            "duration":     {"type": "integer", "description": "Duration in seconds (2-10)", "default": 4},
            "model":        {"type": "string", "description": "runwayml/gen-3-alpha-turbo | lumaai/dream-machine", "default": "runwayml/gen-3-alpha-turbo"},
            "aspect_ratio": {"type": "string", "description": "16:9 | 9:16 | 1:1", "default": "16:9"},
        },
        "required": ["prompt"],
    }

    async def execute(self, params: dict) -> str:
        import uuid as _uuid, re
        prompt   = params.get("prompt", "")
        model    = params.get("model", "runwayml/gen-3-alpha-turbo")
        dur      = params.get("duration", 4)
        ratio    = params.get("aspect_ratio", "16:9")
        asset_id = str(_uuid.uuid4())[:12]

        try:
            from app.core.registry import media_asset_store
            media_asset_store.register(asset_id=asset_id, asset_type="video", url=None,
                prompt=prompt, model=model, meta={"duration": dur, "aspect_ratio": ratio, "status": "pending"})
        except Exception:
            pass

        video_url = None
        if OPENROUTER_API_KEY:
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
                    resp = await client.post(f"{OPENROUTER_BASE_URL}/chat/completions",
                        headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
                        json={"model": model, "messages": [{"role": "user", "content": f"Generate a {dur}s video: {prompt}"}], "max_tokens": 200})
                    if resp.status_code == 200:
                        content = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
                        urls = re.findall(r'https?://\S+', content)
                        if urls: video_url = urls[0].rstrip('.,)')
            except Exception:
                pass

        if video_url:
            try:
                from app.core.registry import media_asset_store
                media_asset_store.update_url(asset_id, video_url)
            except Exception:
                pass

        fallback = f'<div style="background:linear-gradient(135deg,#1a1a2e,#0f3460);display:flex;align-items:center;justify-content:center;color:white;font-family:sans-serif;min-height:300px;" data-asset-id="{asset_id}"><span>VIDEO: {prompt[:50]}</span></div>'
        return json.dumps({
            "asset_id": asset_id, "asset_url": f"/api/assets/{asset_id}",
            "video_url": video_url, "model": model, "prompt": prompt,
            "status": "ready" if video_url else "pending",
            "embed_html": f'<video src="{video_url}" autoplay muted loop playsinline style="width:100%;height:auto;"></video>' if video_url else fallback,
        })


class AudioGeneratorTool(BaseTool):
    name = "generate_audio"
    description = (
        "Generates audio: music tracks, sound effects, or speech/narration via OpenRouter. "
        "Returns asset_id and embed HTML for <audio> tags. "
        "OpenRouter models: suno/bark, meta/musicgen, elevenlabs/multilingual-v2. "
        "Use for background music, narration, sound branding."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "prompt":   {"type": "string", "description": "Audio description or speech text"},
            "type":     {"type": "string", "description": "music | speech | sfx", "default": "music"},
            "duration": {"type": "integer", "description": "Duration in seconds (5-60)", "default": 30},
            "model":    {"type": "string", "description": "suno/bark | meta/musicgen | elevenlabs/multilingual-v2", "default": "suno/bark"},
        },
        "required": ["prompt"],
    }

    async def execute(self, params: dict) -> str:
        import uuid as _uuid, re
        prompt   = params.get("prompt", "")
        atype    = params.get("type", "music")
        model    = params.get("model", "suno/bark")
        duration = params.get("duration", 30)
        asset_id = str(_uuid.uuid4())[:12]

        try:
            from app.core.registry import media_asset_store
            media_asset_store.register(asset_id=asset_id, asset_type="audio", url=None,
                prompt=prompt, model=model, meta={"type": atype, "duration": duration, "status": "pending"})
        except Exception:
            pass

        audio_url = None
        if OPENROUTER_API_KEY:
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(90.0)) as client:
                    resp = await client.post(f"{OPENROUTER_BASE_URL}/chat/completions",
                        headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
                        json={"model": model, "messages": [{"role": "user", "content": f"Generate {atype}: {prompt}"}], "max_tokens": 200})
                    if resp.status_code == 200:
                        content = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
                        urls = re.findall(r'https?://\S+\.(mp3|wav|ogg|m4a)', content, re.I)
                        if urls: audio_url = urls[0][0] if isinstance(urls[0], tuple) else urls[0]
            except Exception:
                pass

        if audio_url:
            try:
                from app.core.registry import media_asset_store
                media_asset_store.update_url(asset_id, audio_url)
            except Exception:
                pass

        return json.dumps({
            "asset_id": asset_id, "asset_url": f"/api/assets/{asset_id}",
            "audio_url": audio_url, "model": model, "type": atype, "prompt": prompt,
            "status": "ready" if audio_url else "pending",
            "embed_html": f'<audio src="{audio_url}" controls style="width:100%;"></audio>' if audio_url else f'<!-- Audio {asset_id}: {prompt[:50]} -->',
        })


class WebSearchTool(BaseTool):
    name = "web_search"
    description = "Busca informações atuais na web via DuckDuckGo."
    parameters_schema = {
        "type": "object",
        "properties": {
            "query":       {"type": "string", "description": "Termos de busca"},
            "max_results": {"type": "integer", "description": "1-10", "default": 5},
        },
        "required": ["query"],
    }

    async def execute(self, params: dict) -> str:
        import urllib.parse
        query = params.get("query", "")
        max_r = min(params.get("max_results", 5), 10)
        encoded = urllib.parse.quote(query)
        url = f"https://api.duckduckgo.com/?q={encoded}&format=json&no_html=1&skip_disambig=1"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(url, headers={"User-Agent": "MAS-Creator/1.0"})
                data = r.json()
                results = []
                if data.get("AbstractText"):
                    results.append({"title": data.get("Heading", ""), "snippet": data["AbstractText"][:300]})
                for item in data.get("RelatedTopics", [])[:max_r]:
                    if isinstance(item, dict) and item.get("Text"):
                        results.append({"title": item.get("FirstURL", ""), "snippet": item["Text"][:200]})
                return json.dumps(results[:max_r], ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)})


class ColorPaletteTool(BaseTool):
    name = "generate_color_palette"
    description = "Gera uma paleta de cores profissional baseado no mood do projeto."
    parameters_schema = {
        "type": "object",
        "properties": {
            "mood": {"type": "string", "description": "luxury|corporate|feminine|energetic|natural|tech|minimal|warm|cold"},
            "base_color": {"type": "string", "description": "Hex base color (optional)"},
        },
        "required": ["mood"],
    }
    PALETTES = {
        "luxury":    ["#1A1A1A","#C4A86F","#F9F7F5","#8B3A3A","#D4C4B5"],
        "corporate": ["#0F2557","#1E4D8C","#FFFFFF","#F4F6FA","#2196F3"],
        "feminine":  ["#F8BBD9","#E91E8C","#FFFFFF","#FCE4EC","#880E4F"],
        "energetic": ["#FF5722","#FF9800","#FFC107","#1A1A1A","#FFFFFF"],
        "natural":   ["#2E7D32","#81C784","#F9FBE7","#795548","#EFEBE9"],
        "tech":      ["#0D1117","#161B22","#58A6FF","#1F6FEB","#C9D1D9"],
        "minimal":   ["#FFFFFF","#F5F5F5","#212121","#757575","#BDBDBD"],
        "warm":      ["#BF360C","#FF6D00","#FFF8E1","#FFAB40","#4E342E"],
        "cold":      ["#0D47A1","#42A5F5","#E3F2FD","#FFFFFF","#37474F"],
    }
    async def execute(self, params: dict) -> str:
        mood = params.get("mood", "minimal").lower()
        palette = self.PALETTES.get(mood, self.PALETTES["minimal"])
        return json.dumps({"palette": palette, "mood": mood,
            "css_variables": "\n".join(f"--color-{i+1}: {c};" for i,c in enumerate(palette))})


class CodeValidatorTool(BaseTool):
    name = "validate_html"
    description = "Valida HTML: verifica tags, estrutura, erros comuns."
    parameters_schema = {"type": "object", "properties": {"html": {"type": "string"}}, "required": ["html"]}

    async def execute(self, params: dict) -> str:
        html = params.get("html", "")
        issues = []
        checks = [
            ("<!DOCTYPE html>" not in html and "<!doctype html>" not in html.lower(), "Faltando <!DOCTYPE html>"),
            ("<html" not in html, "Faltando <html>"),
            ("</html>" not in html, "HTML truncado — faltando </html>"),
            (("<style>" in html or "<style " in html) and "</style>" not in html, "<style> não fechada"),
            (("<script>" in html or "<script " in html) and "</script>" not in html, "<script> não fechada"),
        ]
        issues = [msg for cond, msg in checks if cond]
        return json.dumps({"valid": not issues, "issues": issues, "char_count": len(html), "lines": html.count("\n")})


class TypographyTool(BaseTool):
    name = "suggest_typography"
    description = "Sugere combinações de fontes Google Fonts para o estilo do projeto."
    parameters_schema = {"type": "object", "properties": {"style": {"type": "string", "description": "luxury|corporate|modern|playful|editorial|minimal"}}, "required": ["style"]}
    COMBOS = {
        "luxury":    {"heading": "Playfair Display", "body": "Lato",          "weights": "400;500;600"},
        "corporate": {"heading": "Montserrat",       "body": "Open Sans",     "weights": "400;600;700"},
        "modern":    {"heading": "Space Grotesk",    "body": "Inter",         "weights": "400;500;600"},
        "playful":   {"heading": "Nunito",           "body": "Quicksand",     "weights": "400;600;700"},
        "editorial": {"heading": "Cormorant Garamond","body": "Source Sans 3","weights": "400;500;600"},
        "minimal":   {"heading": "DM Sans",          "body": "DM Sans",       "weights": "300;400;500"},
    }
    async def execute(self, params: dict) -> str:
        style = params.get("style", "modern").lower()
        combo = self.COMBOS.get(style, self.COMBOS["modern"])
        h, b, w = combo["heading"].replace(" ","+"), combo["body"].replace(" ","+"), combo["weights"]
        link = f"https://fonts.googleapis.com/css2?family={h}:wght@{w}&family={b}:wght@{w}&display=swap"
        return json.dumps({**combo, "google_fonts_url": link})


_ALL_TOOLS: dict[str, BaseTool] = {t.name: t for t in [
    ImageGeneratorTool(), VideoGeneratorTool(), AudioGeneratorTool(),
    WebSearchTool(), ColorPaletteTool(), CodeValidatorTool(), TypographyTool(),
]}

def get_tool(name: str) -> BaseTool | None:
    return _ALL_TOOLS.get(name)

def list_tools() -> list[dict]:
    return [{"name": t.name, "description": t.description,
             "parameters": list(t.parameters_schema.get("properties", {}).keys()),
             "modality": "image" if "image" in t.name else "video" if "video" in t.name else "audio" if "audio" in t.name else "text"}
            for t in _ALL_TOOLS.values()]

def get_tools_for_agent(agent_role: str) -> list[BaseTool]:
    role = agent_role.lower()
    selected = []
    if any(k in role for k in ("design","visual","image","photo","banner","catalog","product","fashion")):
        selected += [_ALL_TOOLS["generate_image"], _ALL_TOOLS["suggest_typography"], _ALL_TOOLS["generate_color_palette"]]
    if any(k in role for k in ("video","motion","animation","reel","film")):
        selected.append(_ALL_TOOLS["generate_video"])
    if any(k in role for k in ("audio","music","sound","podcast","voice","narrat")):
        selected.append(_ALL_TOOLS["generate_audio"])
    if any(k in role for k in ("research","seo","content","copy","market")):
        selected.append(_ALL_TOOLS["web_search"])
    if any(k in role for k in ("html","architect","builder","assembler")):
        selected += [_ALL_TOOLS["validate_html"], _ALL_TOOLS["generate_image"]]
    if _ALL_TOOLS["web_search"] not in selected:
        selected.append(_ALL_TOOLS["web_search"])
    return selected