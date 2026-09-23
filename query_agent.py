"""
query_agent.py
---------------
The agent brain. Given a natural-language question and a Connector (from
connectors.py), this module:

  1. Builds a grounded system prompt (schema + query language + rules).
  2. Calls Groq (via the OpenAI-compatible chat completions API) with TWO
     tools defined:
       - `run_query`               -> fetch real data
       - `ask_user_clarification`  -> pause and ask the human a question
         when the request is genuinely ambiguous
  3. When the model calls `run_query`, we validate the query (guardrails),
     execute it against the real connector, and feed the result -- or the
     error -- back as a tool_result message.
  4. When it calls `ask_user_clarification`, we pass its question to
     `clarify_fn` (by default, a terminal prompt), get a human answer, and
     feed that answer back so it can continue.
  5. It repeats step 3/4 as needed, or replies with a final natural-language
     answer.
  6. We cap this loop at MAX_TURNS so nothing can loop forever.

This is the "ReAct" pattern: Reason (the model's text) -> Act (tool call) ->
Observe (tool result or human answer) -> repeat.

WHY THIS FILE CHANGED FROM THE GEMINI VERSION
-----------------------------------------------------
Groq's API is OpenAI-compatible (same style as Kimi was), which shapes
conversations and tool calls differently from Gemini's google-genai SDK:

  Gemini shape                            Groq (OpenAI-style) shape
  ---------------------------------       ---------------------------------
  contents = [types.Content(role=..,      messages = [{"role":.., "content":..}]
    parts=[...])]
  config=types.GenerateContentConfig(     system="..." is just another
    system_instruction="...")               message in the list, role="system"
  response.candidates[0].content          response.choices[0].message
  content.parts, each maybe holding       message.tool_calls (a list, or None)
    a .function_call
  function_call.args (already a dict)     tool_call.function.arguments (a
                                           JSON *string* -- needs json.loads)
  types.Part.from_function_response(      {"role": "tool",
    name=.., response={<a dict>})           "tool_call_id": ...,
                                             "content": "<json string>"}
  tools=[{"function_declarations":        tools=[{"type": "function",
    [{"name":.., "parameters":..}]}]        "function": {"name":.., "parameters":..}}]

Everything else -- the guardrail, the ReAct loop, MAX_TURNS, the connector
interface, the clarification feature, and the out-of-scope handling -- is
unchanged in behavior, only in how it's expressed for this API shape.
"""

import json                                  # to parse tool call arguments and serialize query results
import os
from openai import OpenAI                    # Groq's API is OpenAI SDK-compatible
from connectors import BaseConnector         # the unified backend interface from connectors.py

# Groq model name. Groq deprecates/replaces models periodically -- check
# console.groq.com/docs/models or your platform dashboard if this stops
# working, and override it via GROQ_MODEL in .env instead of editing this
# file. openai/gpt-oss-120b is Groq's current flagship model with tool-use
# support as of this writing.
MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")

# Slightly higher than the original 4-turn version: a clarification
# exchange now also consumes one turn, so ambiguous questions get a fair
# shot at resolving AND still getting their data query answered.
MAX_TURNS = 6


# ---------------------------------------------------------------------------
# Default clarification handler: a simple terminal prompt.
# Swap this out (pass a different `clarify_fn` to ask()) for non-CLI use --
# main.py's FastAPI version already does this with a thread-based pause.
# ---------------------------------------------------------------------------
def _default_clarify(question: str) -> str:
    print(f"\n[clarification needed] {question}")
    return input("Your answer: ").strip()


# ---------------------------------------------------------------------------
# Guardrail: a hard, code-level safety net (never trust the prompt alone)
# ---------------------------------------------------------------------------
def _validate_query(query_language: str, query) -> None:
    """Raises ValueError if the query looks unsafe. This runs BEFORE execution,
    regardless of what the LLM was told to do in the prompt -- prompts can be
    ignored or jailbroken, code-level checks cannot."""
    if query_language == "sql":
        import sqlglot                                   # SQL parser, lets us inspect the query safely
        parsed = sqlglot.parse_one(query)                 # turns the SQL string into an AST
        if parsed.key.upper() != "SELECT":                # only allow read-only statements
            raise ValueError("Only SELECT queries are permitted (guardrail).")
        if "limit" not in query.lower():                  # force a row cap so results stay small
            raise ValueError("Query must include a LIMIT clause (guardrail).")
    elif query_language == "spark-sql":
        if not query.strip().lower().startswith("select"):
            raise ValueError("Only SELECT queries are permitted (guardrail).")
    # nosql / vector queries are dict-shaped filters, not executable code,
    # so there's no injection risk the same way -- but you could add field
    # allow-lists here for extra safety in production.


# ---------------------------------------------------------------------------
# Tool definitions the model sees -- OpenAI-style "function" tool shape.
# TWO tools: run_query (fetch data) and ask_user_clarification (pause and
# ask the human).
# ---------------------------------------------------------------------------
def _build_tool_schema(query_language: str) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": "run_query",
                "description": (
                    f"Execute a {query_language} query against the connected database "
                    f"and return the results. Call this whenever you need real data "
                    f"to answer the user's question, AND you have enough information "
                    f"to write a precise, unambiguous query."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": f"A valid {query_language} query as a string. "
                                             f"For mongo-json or es-dsl, pass the JSON body as a string.",
                        }
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "ask_user_clarification",
                "description": (
                    "Call this INSTEAD of run_query when the user's question is genuinely "
                    "ambiguous -- for example, it references a filter (like a region, category, "
                    "or date range) without specifying which value to use, and the dataset has "
                    "multiple possible values for it. Ask one clear, specific question that would "
                    "let you write a precise query once answered. Do not guess a value on your own "
                    "when a real ambiguity exists."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "The clarifying question to ask the user, in plain language.",
                        }
                    },
                    "required": ["question"],
                },
            },
        },
    ]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def ask(
    question: str,
    connector: BaseConnector,
    client: OpenAI,
    clarify_fn=_default_clarify,
) -> str:
    """Runs the full agent loop for one user question and returns the
    model's final natural-language answer. If the question is ambiguous,
    it may call `ask_user_clarification` one or more times first -- each
    time, `clarify_fn` is called with its question and must return the
    human's answer as a string."""

    schema_text = connector.get_schema()          # grounds the LLM in the REAL schema (schema linking)
    tools = _build_tool_schema(connector.query_language)

    system_prompt = f"""You are a data-retrieval agent. You answer questions using
ONLY the connected dataset below -- never invent field/table names, values, or numbers.

Query language to use: {connector.query_language}
Schema:
{schema_text}

Rules:
- Always call the `run_query` tool to fetch real data before answering,
  UNLESS the question is out of scope (see below).
- If a question is ambiguous -- e.g. it mentions a filter like a region, category,
  product, or date range without saying which one, and the dataset has multiple
  possible values -- call `ask_user_clarification` and ask a specific question
  BEFORE calling run_query. Do not guess or pick an arbitrary value yourself.
- Once the user has answered your clarifying question, use that answer to write
  a precise query.
- OUT-OF-SCOPE QUESTIONS: if the question is unrelated to this dataset -- general
  knowledge (e.g. "what's the capital of France"), casual conversation (e.g. "how
  are you", "tell me a joke"), or something about columns/entities that simply do
  not exist in the schema above -- do NOT call run_query and do NOT guess an
  answer. Instead, reply directly with plain text: politely say you can only
  answer questions about this connected dataset, and briefly mention (in one
  sentence) what the dataset actually contains, based on the schema above, so
  the user knows what they can ask instead.
- If a query fails, read the error message and try a corrected query.
- Keep result sets small (use limits).
- Give a concise, factual final answer grounded only in the returned rows.
- A result of 0 records, a count of 0, or an empty result set is a valid and meaningful answer.
- NEVER retry a successful query merely because its result is 0 or empty.
- If the query executed successfully and the returned result is 0/empty, accept that result and answer the user directly."""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": question},
    ]

    for turn in range(MAX_TURNS):
        response = client.chat.completions.create(
            model=MODEL,
            max_tokens=1024,
            tools=tools,
            tool_choice="auto",
            messages=messages,
        )

        message = response.choices[0].message   # the model's reply lives here

        # Record this turn in the conversation, using model_dump() so the
        # tool_calls structure round-trips correctly on the next request.
        messages.append(message.model_dump(exclude_unset=True))

        tool_calls = message.tool_calls          # a list of tool calls, or None/empty if it's done

        if not tool_calls:
            # No tool call -> final answer in plain text (this covers both
            # a real data answer AND the out-of-scope explanation).
            return message.content or ""

        tool_call = tool_calls[0]                # handle one call per turn, same as before

        # ------------------------------------------------------------
        # Branch 1: the model needs clarification from the human, not data.
        # ------------------------------------------------------------
        if tool_call.function.name == "ask_user_clarification":
            args = json.loads(tool_call.function.arguments)
            clarifying_question = args["question"]
            print(f"[turn {turn}] asking for clarification:\n{clarifying_question}\n")

            user_answer = clarify_fn(clarifying_question)

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": json.dumps({"user_response": user_answer}),
            })
            continue   # go straight to the next turn -- the model now has the human's answer

        # ------------------------------------------------------------
        # Branch 2: the model wants to run a query.
        # ------------------------------------------------------------
        args = json.loads(tool_call.function.arguments)
        raw_query = args["query"]
        print(f"[turn {turn}] proposed {connector.query_language} query:\n{raw_query}\n")

        try:
            _validate_query(connector.query_language, raw_query)   # guardrail check FIRST
            executable = json.loads(raw_query) if connector.query_language in ("mongo-json", "es-dsl") else raw_query
            rows = connector.run_query(executable)                  # actually hits the database
            safe_rows = json.loads(json.dumps(rows, default=str))   # sanitize Decimal/datetime -> JSON-safe
            tool_output = json.dumps({"result": safe_rows})
        except Exception as exc:
            # Feed the error back instead of crashing -- this is the
            # "self-repair" step: the model sees exactly why it failed and retries.
            tool_output = json.dumps({"error": str(exc)})

        messages.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": tool_output,
        })

    return "Agent could not produce a valid query within the turn limit."