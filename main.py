"""
main.py
-------
This is the ONLY file that changed to let a frontend talk to your backend.
query_agent.py, connectors.py, ingest.py, and dataset_config.py are all
UNCHANGED -- this file just wraps them in a small web API instead of a
terminal input()/print() loop.

WHY A SIMPLE "convert input() to an HTTP request" SWAP DOESN'T WORK
-----------------------------------------------------------------------
Your terminal version could just call input() and the whole program would
pause and wait -- easy. A web server can't do that: each HTTP request must
get a response and finish. If Gemini asks a clarifying question mid-way
through answering, the server can't just "wait" inside one HTTP request
forever; a browser might time out, and no other user could be served while
waiting.

So instead of one blocking call, this file exposes a small "job" pattern
that any frontend (a website, a mobile app, anything that can make HTTP
requests) can follow:

    1. POST /session       -> upload a file (or skip it) and get a session_id
    2. POST /ask           -> send a question, get back "processing" instantly
    3. GET  /status/{id}   -> poll this: "processing" | "needs_clarification"
                               (with the question) | "done" (with the answer)
                               | "error"
    4. POST /clarify       -> if status was "needs_clarification", send the
                               user's answer here, then go back to polling
                               /status until "done"

Internally, each question runs in its own background thread. If Gemini
calls the `ask_user_clarification` tool (a feature already built into
query_agent.py -- see its `clarify_fn` parameter), our clarify_fn here
pauses ONLY that one background thread using a threading.Event, and
updates the session's status so a poll immediately reveals the question.
Once /clarify is called, the event is set, that thread wakes up and
continues the agent loop -- all without blocking the rest of the server
or needing to change anything in query_agent.py itself.

RUN THIS SERVER:
    pip install fastapi uvicorn python-multipart
    uvicorn main:app --reload --port 8000

Your frontend then talks to http://localhost:8000
"""

import os
import tempfile
import threading
import uuid

from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import OpenAI   # Groq's API is OpenAI SDK-compatible
from dotenv import load_dotenv

from dataset_config import DATASET_CONFIG   # fallback: used only if no file is uploaded
from connectors import get_connector
from query_agent import ask
from ingest import load_file_to_dataset_config

load_dotenv()

# Gemini client: built once, reused across every session/question -- same
# as your original main.py did.
client = OpenAI(
    api_key=os.environ["GROQ_API_KEY"],
    base_url="https://api.groq.com/openai/v1",
)

app = FastAPI(title="Text-to-SQL Agent API")

# Lets a frontend running on a different origin (e.g. localhost:3000, or a
# deployed site) call this API from the browser. Tighten allow_origins to
# your actual frontend's URL before going to production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# In-memory session store.
# Each session holds a connector (the dataset the user picked) and the
# current state of whatever question is being processed. This is fine for
# a single-server setup / development; if you ever run multiple server
# processes behind a load balancer, this in-memory dict would need to move
# to something shared like Redis -- not needed for now.
# ---------------------------------------------------------------------------
sessions: dict[str, dict] = {}


def _new_session_state(connector, description: str) -> dict:
    return {
        "connector": connector,
        "description": description,
        "status": "idle",             # idle | processing | needs_clarification | done | error
        "question": None,             # the clarifying question, when status == needs_clarification
        "answer": None,               # the final answer, when status == done
        "error": None,                # the error message, when status == error
        "clarify_event": None,        # threading.Event, set once the user answers
        "clarify_answer": None,       # the user's answer, read once by the waiting thread
    }


def _run_ask_in_background(session_id: str, question: str):
    """Runs query_agent.ask() in a background thread so the /ask endpoint
    can return immediately instead of blocking the whole server."""
    session = sessions[session_id]

    def clarify_fn(clarifying_question: str) -> str:
        # Called from inside ask() (in query_agent.py, unchanged) when
        # Gemini needs more information. We pause THIS thread only.
        session["question"] = clarifying_question
        session["status"] = "needs_clarification"
        session["clarify_event"] = threading.Event()
        session["clarify_event"].wait()   # blocks until /clarify sets this
        answer = session["clarify_answer"]
        session["clarify_answer"] = None
        session["question"] = None
        session["status"] = "processing"
        return answer

    try:
        result = ask(
            question=question,
            connector=session["connector"],
            client=client,
            clarify_fn=clarify_fn,
        )
        session["answer"] = result
        session["status"] = "done"
    except Exception as exc:
        session["error"] = str(exc)
        session["status"] = "error"


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------
class AskRequest(BaseModel):
    session_id: str
    question: str


class ClarifyRequest(BaseModel):
    session_id: str
    answer: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/")
def health_check():
    """Simple endpoint a frontend can call to confirm the API is up."""
    return {"status": "ok"}


@app.post("/session")
async def create_session(file: UploadFile = File(None)):
    """Starts a new session. Upload a file to query it, or call this with
    no file to fall back to the default configured dataset (DATASET_CONFIG),
    exactly like pressing Enter in the old terminal version."""
    if file is not None:
        suffix = os.path.splitext(file.filename)[1]
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(await file.read())
            tmp_path = tmp.name

        try:
            dataset_config = load_file_to_dataset_config(tmp_path)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Couldn't load file: {exc}")
    else:
        dataset_config = DATASET_CONFIG

    try:
        connector = get_connector(dataset_config)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Couldn't connect to dataset: {exc}")

    session_id = str(uuid.uuid4())
    description = dataset_config.get("description", "")
    sessions[session_id] = _new_session_state(connector, description)

    return {
        "session_id": session_id,
        "backend_type": dataset_config["type"],
        "description": description,
    }


@app.post("/ask")
def ask_question(req: AskRequest):
    """Starts answering a question in the background and returns instantly.
    The frontend should poll GET /status/{session_id} to see progress."""
    session = sessions.get(req.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Unknown session_id. Call POST /session first.")

    if session["status"] in ("processing", "needs_clarification"):
        raise HTTPException(status_code=409, detail="This session is already processing a question.")

    session["status"] = "processing"
    session["answer"] = None
    session["error"] = None

    thread = threading.Thread(target=_run_ask_in_background, args=(req.session_id, req.question), daemon=True)
    thread.start()

    return {"status": "processing"}


@app.get("/status/{session_id}")
def get_status(session_id: str):
    """Poll this after /ask (and after /clarify) to see current progress."""
    session = sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Unknown session_id.")

    return {
        "status": session["status"],
        "question": session["question"],
        "answer": session["answer"],
        "error": session["error"],
    }


@app.post("/clarify")
def clarify(req: ClarifyRequest):
    """Call this when GET /status showed status == 'needs_clarification',
    passing the user's answer. Then go back to polling /status."""
    session = sessions.get(req.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Unknown session_id.")

    if session["status"] != "needs_clarification" or session["clarify_event"] is None:
        raise HTTPException(status_code=409, detail="This session isn't waiting for clarification right now.")

    session["clarify_answer"] = req.answer
    session["clarify_event"].set()

    return {"status": "ok"}