# DataMuse — Text-to-SQL Agentic Data Assistant

Upload a dataset (CSV, Excel, JSON, or Parquet), ask questions about it in
plain English, and get real, grounded answers back — powered by an
agentic ReAct loop with a tool-calling LLM (Groq), a safety-guardrailed
SQL execution layer, and a self-repair + clarification loop for messy or
ambiguous questions.

```
User question
    ↓
Streamlit frontend (app.py)
    ↓  HTTP
FastAPI backend (main.py)
    ↓
query_agent.py  ←→  Groq (LLM reasoning + tool calls)
    ↓
connectors.py  →  PostgreSQL / SQLite (uploaded file)
    ↓
results back to Groq → final natural-language answer
```

---

## Features

- **Upload your own dataset** — CSV, Excel (.xlsx/.xls), JSON, or Parquet.
  Large CSVs are streamed in chunks so big files don't blow up memory.
- **No file? No problem** — falls back to a default configured PostgreSQL
  dataset (`dataset_config.py`) if you skip the upload.
- **Ask questions in plain English** — the agent writes and runs real SQL
  behind the scenes; you never see or write SQL yourself.
- **Self-repair** — if the generated query fails or returns something the
  model isn't confident about, it automatically retries with a corrected
  query (up to `MAX_TURNS`, default 6).
- **Clarification instead of guessing** — if a question is ambiguous (e.g.
  *"total sales in region"* when the dataset has multiple regions), the
  agent asks which one instead of picking one at random.
- **Out-of-scope detection** — general knowledge or off-topic questions
  (*"what's the capital of France"*) get a polite "I can only answer
  questions about this dataset" response instead of a broken or
  hallucinated query.
- **Hard-coded safety guardrail** — independent of what the LLM is told:
  only `SELECT` queries are ever executed, and every query must include a
  `LIMIT`. This is enforced in code, not just prompted.
- **Swappable LLM backend** — the architecture has already been proven to
  work with Anthropic Claude, Kimi (Moonshot AI), Google Gemini, and now
  Groq, by changing only two files (`query_agent.py` and `main.py`).

---

## Architecture / file guide

| File | Role | Changes when you swap LLM providers? |
|---|---|---|
| `main.py` | FastAPI server — exposes `/session`, `/ask`, `/status/{id}`, `/clarify` for the frontend | ✅ Yes (client setup only) |
| `query_agent.py` | The agent brain — builds the prompt, calls the LLM, runs the ReAct loop, guardrails, clarification | ✅ Yes (response parsing + tool schema) |
| `connectors.py` | Unified interface to query any backend (SQL today; NoSQL/vector/big-data scaffolding included but untested) | ❌ Never |
| `ingest.py` | Loads an uploaded file into a local SQLite database so it becomes queryable | ❌ Never |
| `dataset_config.py` | Fallback dataset config used only if no file is uploaded | ❌ Never |
| `app.py` | Streamlit frontend — talks only to `main.py`'s API, never to the LLM directly | ❌ Never |

**Why the LLM provider only ever touches 2 files:** every other file talks
to *an interface* (`connector.get_schema()`, `connector.run_query()`, or
the FastAPI `/ask` → `/status` → `/clarify` cycle), never to a specific
LLM SDK. The provider-specific code (how tool calls are shaped, how
responses are parsed) is isolated entirely inside `query_agent.py`, and
`main.py` only needs to know how to construct that provider's client.

---

## Current LLM provider: Groq

This project currently uses **Groq** (`https://api.groq.com/openai/v1`),
which exposes an OpenAI-compatible API — so `query_agent.py` and `main.py`
use the standard `openai` Python package pointed at Groq's endpoint,
rather than a Groq-specific SDK.

- **Default model:** `openai/gpt-oss-120b` (Groq's flagship model with
  tool-calling support, as of this writing)
- **Why Groq:** free tier with meaningfully higher daily limits than some
  alternatives (e.g. Gemini's free tier caps at 20 requests/day per
  project, which is easy to exhaust during active development)
- Model name is configurable via `.env` (`GROQ_MODEL`) — check
  [console.groq.com/docs/models](https://console.groq.com/docs/models) if
  the default above is ever deprecated

---

## Setup

### 1. Install dependencies
```bash
pip install fastapi uvicorn python-multipart openai python-dotenv pandas openpyxl sqlalchemy sqlglot streamlit requests
```

### 2. Get a free Groq API key
1. Go to [console.groq.com](https://console.groq.com)
2. Sign up / log in (no credit card required)
3. Go to **API Keys** → **Create API Key**
4. Copy the key (starts with `gsk_`) — it's only shown once

### 3. Create your `.env` file
Copy `.env.example` to `.env` and fill in your real key:
```
GROQ_API_KEY=gsk_your_real_key_here
GROQ_MODEL=openai/gpt-oss-120b
```
**Never commit `.env` to git** — it's already excluded via `.gitignore`.

### 4. Run the backend
```bash
python -m uvicorn main:app --reload --port 8000
```
Leave this terminal running. Visit `http://127.0.0.1:8000/docs` to test
the API directly (upload, ask, clarify) before touching the frontend.

### 5. Run the frontend (separate terminal)
```bash
streamlit run app.py
```
Opens at `http://localhost:8501`.

---

## API reference (what the frontend talks to)

| Endpoint | Method | Purpose |
|---|---|---|
| `/` | GET | Health check |
| `/session` | POST | Upload a file (optional) → returns a `session_id` |
| `/ask` | POST | `{session_id, question}` → returns `{"status": "processing"}` instantly |
| `/status/{session_id}` | GET | Poll this: `processing` \| `needs_clarification` (+ question) \| `done` (+ answer) \| `error` |
| `/clarify` | POST | `{session_id, answer}` → answers a pending clarifying question |

This is a **polling-based** API, not push/websocket-based: after `/ask`,
the frontend must repeatedly call `/status` (roughly every 1 second) until
it stops returning `"processing"`. Each question runs in its own
background thread, so one session's clarification pause never blocks
other users.

---

## Known limitations (honest list)

- **In-memory sessions only** — restarting the backend wipes all active
  sessions. Fine for local dev; would need a shared store (e.g. Redis)
  for a multi-process production deployment.
- **NoSQL / vector / big-data connectors are untested scaffolding** —
  `connectors.py` supports `nosql`, `vector`, and `bigdata` config types,
  but only `sql` (Postgres/SQLite via SQLAlchemy) has been built and
  tested end-to-end.
- **Out-of-scope detection relies on the LLM's judgment**, guided by a
  system prompt instruction — not a hardcoded rule. It generalizes well in
  testing but isn't mathematically guaranteed to catch every edge case.
- **No conversation memory across sessions** — each uploaded file starts a
  fresh session; there's no long-term history or saved chats.
- **Free-tier rate limits apply** — Groq's free tier is generous but not
  unlimited; heavy testing can still hit limits.

---

## Testing

- `test_connector.py` — confirms the database connector alone works
- `check_gemini_connection.py` — leftover from an earlier provider phase;
  not applicable to the current Groq setup (safe to ignore or delete)
- Manual testing flow: upload a file → ask a straightforward question →
  ask an ambiguous one (should trigger clarification) → ask an off-topic
  one (should get the "I can only answer..." response)

---

## Provider history

This project has been built and re-verified against multiple LLM
providers over its development, proving the architecture's swappability:

1. Anthropic Claude (original design)
2. Kimi (Moonshot AI) — OpenAI-compatible API
3. Google Gemini — google-genai SDK
4. **Groq (current)** — OpenAI-compatible API

Each swap only required changes to `query_agent.py` (response parsing +
tool schema) and `main.py` (client construction) — every other file in
the project was untouched across all four providers.
