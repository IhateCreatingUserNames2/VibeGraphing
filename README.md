# 🤖 MAS Creator — Vibe Graphing Universal Agent

> Cria **qualquer coisa** (website, jogo, código, dashboard, documento...) a partir de linguagem natural + imagens, usando um pipeline Multi-Agent System de 3 estágios.
<img width="1341" height="844" alt="image" src="https://github.com/user-attachments/assets/0a3e3ac7-4b48-45e4-89bf-6d497dbe61d0" />

---

## ⚡ Quickstart (5 minutos)

```bash
# 1. Clone / entre na pasta
cd mas-creator

# 2. Crie e ative o virtualenv
python -m venv .venv
source .venv/bin/activate      # Linux/Mac
# .venv\Scripts\activate       # Windows

# 3. Instale dependências
pip install -r requirements.txt

# 4. Configure sua chave
cp .env.example .env
# Edite .env e coloque sua OPENROUTER_API_KEY

# 5. Rode!
python main.py
```

Acesse: **http://localhost:8000**

---

## 🗂️ Estrutura do Projeto

```
mas-creator/
├── main.py                    # Entry point FastAPI
├── requirements.txt
├── .env.example               # Template de configuração
├── frontend/
│   └── index.html             # UI completa (HTML single-file, sem build)
└── app/
    ├── api/
    │   └── routes.py          # Endpoints REST
    ├── agents/
    │   └── pipeline.py        # Pipeline Vibe Graphing (3 estágios + runtime)
    └── core/
        ├── llm.py             # Cliente OpenRouter
        └── models.py          # Modelos Pydantic + Job Store
```

---

## 🧠 Como funciona o Vibe Graphing

```
Seu pedido (texto + imagens)
           │
           ▼
┌─────────────────────────┐
│  Stage 1: Role Assigner │  → Define quais agentes são necessários
│  (modelo BUILD)         │  → Detecta tipo, estilo, paleta
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐
│  Stage 2: Topology      │  → Cria o grafo de execução
│  Designer (BUILD)       │  → Define paralelo vs sequencial
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐
│  Stage 3: Semantic      │  → Escreve o system prompt de cada agente
│  Expert (BUILD)         │  → Configura critérios de qualidade
└──────────┬──────────────┘
           │
           ▼
┌───────────────────────────────────────────────────┐
│  Runtime: Execução do Grafo                       │
│                                                   │
│  [Agente A] ──┐                                  │
│  [Agente B] ──┤→ [Agente D] → [Assembler Final] │
│  [Agente C] ──┘                                  │
│  (paralelo)     (sequencial)                     │
└───────────────────────────────────────────────────┘
           │
           ▼
    Output Final ✅  (HTML / Python / Markdown / etc.)
```

**Por que é 10x mais barato que Vibe Coding?**
- O modelo **caro** (BUILD) roda apenas **3 vezes** para montar o grafo
- Os agentes usam o modelo **barato** (EXECUTE) para gerar o conteúdo
- O resultado reutilizável pode ser cacheado

---

## 📡 API Endpoints

| Método | Endpoint | Descrição |
|--------|----------|-----------|
| `POST` | `/api/create` | Cria um job (multipart: request, style?, creation_type?, images[]) |
| `GET` | `/api/status/:id` | Status + pipeline intermediates |
| `GET` | `/api/result/:id` | Resultado completo |
| `GET` | `/api/preview/:id` | Renderiza HTML no browser |
| `GET` | `/api/download/:id` | Download do arquivo gerado |
| `GET` | `/api/jobs` | Histórico de jobs |
| `GET` | `/api/models` | Modelos configurados |
| `GET` | `/api/health` | Health check |
| `GET` | `/docs` | Swagger UI automático |

---

## 🎯 Exemplos de pedidos

### Website de Restaurante
```
Crie uma landing page para o restaurante "Sabor da Serra".
Comida mineira tradicional. Quero: hero com foto de fundo, 
menu do dia, galeria de pratos, reservas e localização.
Estilo: rústico, tons de madeira e verde.
```

### Jogo Snake
```
Crie um jogo Snake completo em HTML5.
Visual neon/cyberpunk, placar com recorde salvo, 
velocidade aumenta com o tempo, efeitos de partículas quando come.
```

### Dashboard de Vendas
```
Dashboard de vendas com: gráfico de vendas por mês (barras),
top 5 produtos (pizza), mapa de calor por região,
cards com KPIs (receita, conversão, ticket médio).
Dados fictícios realistas. Dark mode.
```

### API FastAPI
```
API REST em FastAPI para um sistema de blog:
- Posts (CRUD completo)
- Comentários
- Tags/categorias
- Autenticação JWT
- Documentação OpenAPI automática
Com SQLite + SQLAlchemy.
```

---

## ⚙️ Configuração de Modelos

Edite o `.env` para trocar os modelos:

```env
# Modelos disponíveis no OpenRouter:
# https://openrouter.ai/models

# Claude (recomendado)
MAS_BUILD_MODEL=anthropic/claude-sonnet-4-5
MAS_EXECUTE_MODEL=anthropic/claude-haiku-4-5

# GPT-4
MAS_BUILD_MODEL=openai/gpt-4o
MAS_EXECUTE_MODEL=openai/gpt-4o-mini

# Gemini (muito barato)
MAS_BUILD_MODEL=google/gemini-pro-1.5
MAS_EXECUTE_MODEL=google/gemini-flash-1.5

# Llama (open source)
MAS_BUILD_MODEL=meta-llama/llama-3.1-70b-instruct
MAS_EXECUTE_MODEL=meta-llama/llama-3.1-8b-instruct
```

---

## 🏠 Hospedar em casa (produção)

```bash
# Com PM2 (Node.js process manager)
pip install uvicorn[standard]
pm2 start "python main.py" --name mas-creator

# Ou com systemd
# Crie /etc/systemd/system/mas-creator.service

# Ou simples com nohup
nohup python main.py > logs.txt 2>&1 &
```

Para expor na internet com túnel (sem porta aberta):
```bash
# Cloudflare Tunnel (gratuito)
cloudflared tunnel --url http://localhost:8000

# Ou ngrok
ngrok http 8000
```
