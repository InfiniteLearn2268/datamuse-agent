import html
import time

import requests
import streamlit as st

st.set_page_config(
    page_title="DataMuse",
    page_icon="✦",
    layout="centered"
)

# ---------- Backend connection ----------
# This is the FastAPI server from main.py. Change this if your backend runs
# somewhere other than your own machine on port 8000.
BACKEND_URL = "http://127.0.0.1:8000"


# ---------------------------------------------------------------------------
# Backend call helpers -- plain functions, no Streamlit calls inside, so
# they're easy to test/reuse and easy to reason about independently of the
# UI code below.
# ---------------------------------------------------------------------------
def backend_create_session(file_bytes: bytes, filename: str, content_type: str) -> dict:
    """POST /session with the uploaded file. Returns the response dict, or
    raises an exception the caller should catch and show to the user."""
    files = {"file": (filename, file_bytes, content_type or "application/octet-stream")}
    resp = requests.post(f"{BACKEND_URL}/session", files=files, timeout=120)
    resp.raise_for_status()
    return resp.json()


def backend_ask(session_id: str, question: str) -> dict:
    """POST /ask. Returns immediately with {"status": "processing"} on
    success -- the caller must then poll backend_poll_status()."""
    resp = requests.post(
        f"{BACKEND_URL}/ask",
        json={"session_id": session_id, "question": question},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def backend_clarify(session_id: str, answer: str) -> dict:
    """POST /clarify with the user's answer to a pending clarifying question."""
    resp = requests.post(
        f"{BACKEND_URL}/clarify",
        json={"session_id": session_id, "answer": answer},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def backend_poll_status(session_id: str, timeout_seconds: int = 90, interval_seconds: float = 1.0) -> dict:
    """Repeatedly calls GET /status/{session_id} until the backend reports
    something other than 'processing' (i.e. 'done', 'needs_clarification',
    or 'error'), or until timeout_seconds elapses. Always returns a dict
    with a 'status' key -- 'timeout' or 'connection_error' are added here
    for cases the backend itself can't report."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            resp = requests.get(f"{BACKEND_URL}/status/{session_id}", timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.RequestException as exc:
            return {"status": "connection_error", "error": str(exc)}

        if data.get("status") in ("done", "needs_clarification", "error"):
            return data

        time.sleep(interval_seconds)

    return {"status": "timeout"}


# ---------- Simple styling ----------

st.markdown("""
<style>

.stApp {
    background-color: #FBF7F1;
}

.block-container {
    max-width: 850px;
    padding-top: 80px;
}

#MainMenu {
    visibility: hidden;
}

footer {
    visibility: hidden;
}

header {
    visibility: hidden;
}

.brand {
    text-align: center;
    font-size: 18px;
    font-weight: 600;
    color: #4A4038;
    margin-bottom: 70px;
}

.brand span {
    color: #D96B3B;
}

.title {
    text-align: center;
    font-family: Georgia, serif;
    font-size: 52px;
    line-height: 1.1;
    color: #302A26;
    margin-bottom: 15px;
}

.title span {
    color: #D96B3B;
}

.subtitle {
    text-align: center;
    color: #80756C;
    font-size: 17px;
    margin-bottom: 50px;
}

.upload-box {
    background: white;
    border: 1px solid #E7DDD3;
    border-radius: 20px;
    padding: 30px;
    text-align: center;
    margin-bottom: 25px;
}

.upload-title {
    font-size: 20px;
    font-weight: 600;
    color: #3D352F;
    margin-bottom: 7px;
}

.upload-text {
    color: #8A8179;
    font-size: 14px;
}

.ready {
    background: #F2E9DF;
    border-radius: 15px;
    padding: 16px 20px;
    color: #594D44;
    text-align: center;
    margin-bottom: 20px;
}

.ready-desc {
    text-align: center;
    color: #8A8179;
    font-size: 13px;
    margin-bottom: 35px;
}

.question-title {
    text-align: center;
    font-family: Georgia, serif;
    font-size: 25px;
    color: #302A26;
    margin-bottom: 18px;
}

.user-message {
    background: #EADDD1;
    padding: 14px 18px;
    border-radius: 18px 18px 4px 18px;
    margin: 15px 0 15px auto;
    width: fit-content;
    max-width: 75%;
    color: #3D352F;
    white-space: pre-wrap;
}

.ai-message {
    background: white;
    border: 1px solid #E7DDD3;
    padding: 17px 20px;
    border-radius: 18px 18px 18px 4px;
    margin: 15px 0;
    max-width: 80%;
    color: #3D352F;
    white-space: pre-wrap;
}

.ai-message.clarify {
    border-color: #D96B3B;
    background: #FFF6EF;
}

.ai-message.error {
    border-color: #C9564A;
    background: #FCEEEC;
    color: #7A3B34;
}

[data-testid="stFileUploader"] {
    background: transparent;
}

</style>
""", unsafe_allow_html=True)


# ---------- Session state ----------

if "file_uploaded" not in st.session_state:
    st.session_state.file_uploaded = False

if "file_name" not in st.session_state:
    st.session_state.file_name = ""

if "session_id" not in st.session_state:
    st.session_state.session_id = None

if "dataset_description" not in st.session_state:
    st.session_state.dataset_description = ""

if "messages" not in st.session_state:
    st.session_state.messages = []

if "awaiting_clarification" not in st.session_state:
    # True after the backend has asked a clarifying question -- the NEXT
    # thing the user types goes to /clarify instead of a fresh /ask.
    st.session_state.awaiting_clarification = False

if "upload_error" not in st.session_state:
    st.session_state.upload_error = None


def reset_session():
    """Starts over with a new file -- clears everything, including the
    backend session_id (the old one is abandoned; the backend just keeps
    it in memory until the server restarts, which is fine)."""
    st.session_state.file_uploaded = False
    st.session_state.file_name = ""
    st.session_state.session_id = None
    st.session_state.dataset_description = ""
    st.session_state.messages = []
    st.session_state.awaiting_clarification = False
    st.session_state.upload_error = None


def render_message(role: str, text: str, kind: str = "normal"):
    """Renders one chat bubble. `text` is HTML-escaped so a question or
    answer containing <, >, & etc. can't break the page layout."""
    safe_text = html.escape(text)
    if role == "user":
        st.markdown(f'<div class="user-message">{safe_text}</div>', unsafe_allow_html=True)
    else:
        css_class = "ai-message"
        if kind == "clarify":
            css_class += " clarify"
        elif kind == "error":
            css_class += " error"
        st.markdown(f'<div class="{css_class}">{safe_text}</div>', unsafe_allow_html=True)


def handle_backend_result(result: dict):
    """Given a dict from backend_poll_status() (or a connection error dict),
    appends the right assistant message and updates awaiting_clarification."""
    status = result.get("status")

    if status == "needs_clarification":
        question = result.get("question") or "Could you clarify your question?"
        st.session_state.messages.append({"role": "assistant", "text": question, "kind": "clarify"})
        st.session_state.awaiting_clarification = True

    elif status == "done":
        answer = result.get("answer") or "I couldn't produce an answer for that."
        st.session_state.messages.append({"role": "assistant", "text": answer, "kind": "normal"})
        st.session_state.awaiting_clarification = False

    elif status == "error":
        err = result.get("error") or "Something went wrong while answering that."
        st.session_state.messages.append(
            {"role": "assistant", "text": f"Sorry, I ran into a problem: {err}", "kind": "error"}
        )
        st.session_state.awaiting_clarification = False

    elif status == "timeout":
        st.session_state.messages.append(
            {
                "role": "assistant",
                "text": "This is taking longer than expected. Please try asking again in a moment.",
                "kind": "error",
            }
        )
        st.session_state.awaiting_clarification = False

    elif status == "connection_error":
        st.session_state.messages.append(
            {
                "role": "assistant",
                "text": (
                    "I can't reach the backend server right now. Make sure it's running "
                    "(uvicorn main:app --reload --port 8000) and try again."
                ),
                "kind": "error",
            }
        )
        st.session_state.awaiting_clarification = False


# ---------- Brand ----------

st.markdown(
    """
    <div class="brand">
        ✦ <span>DataMuse</span>
    </div>
    """,
    unsafe_allow_html=True
)


# ---------- Before upload ----------

if not st.session_state.file_uploaded:

    st.markdown(
        """
        <div class="title">
            Ask your <span>data</span><br>
            anything.
        </div>

        <div class="subtitle">
            Upload your data and ask questions in plain English.
        </div>
        """,
        unsafe_allow_html=True
    )

    st.markdown(
        """
        <div class="upload-box">
            <div class="upload-title">
                Bring your data
            </div>

            <div class="upload-text">
                CSV, Excel, JSON or Parquet
            </div>
        </div>
        """,
        unsafe_allow_html=True
    )

    if st.session_state.upload_error:
        st.error(st.session_state.upload_error)

    uploaded_file = st.file_uploader(
        "Choose your file",
        type=["csv", "xlsx", "xls", "json", "parquet"],
        label_visibility="collapsed"
    )

    if uploaded_file:
        with st.spinner("Reading your data..."):
            try:
                result = backend_create_session(
                    uploaded_file.getvalue(),
                    uploaded_file.name,
                    uploaded_file.type,
                )
                st.session_state.session_id = result["session_id"]
                st.session_state.dataset_description = result.get("description", "")
                st.session_state.file_uploaded = True
                st.session_state.file_name = uploaded_file.name
                st.session_state.upload_error = None
            except requests.exceptions.ConnectionError:
                st.session_state.upload_error = (
                    "Can't reach the backend server. Make sure it's running: "
                    "uvicorn main:app --reload --port 8000"
                )
            except requests.exceptions.HTTPError as exc:
                # The backend returns a clear "detail" message for bad files
                # (wrong format, corrupt file, etc.) -- show that directly.
                try:
                    detail = exc.response.json().get("detail", str(exc))
                except Exception:
                    detail = str(exc)
                st.session_state.upload_error = f"Couldn't read that file: {detail}"
            except Exception as exc:
                st.session_state.upload_error = f"Something went wrong: {exc}"

        st.rerun()


# ---------- After upload ----------

else:

    st.markdown(
        f"""
        <div class="title">
            Your data is <span>ready.</span>
        </div>

        <div class="subtitle">
            {html.escape(st.session_state.file_name)}
        </div>
        """,
        unsafe_allow_html=True
    )

    st.markdown(
        """
        <div class="ready">
            ✓ Your file is ready to explore
        </div>
        """,
        unsafe_allow_html=True
    )

    if st.session_state.dataset_description:
        st.markdown(
            f'<div class="ready-desc">{html.escape(st.session_state.dataset_description)}</div>',
            unsafe_allow_html=True,
        )

    if st.button("Upload a different file"):
        reset_session()
        st.rerun()

    for message in st.session_state.messages:
        render_message(message["role"], message["text"], message.get("kind", "normal"))

    if len(st.session_state.messages) == 0:

        st.markdown(
            """
            <div class="question-title">
                What would you like to know?
            </div>
            """,
            unsafe_allow_html=True
        )

    placeholder = (
        "Type your answer..." if st.session_state.awaiting_clarification else "Ask your data..."
    )
    question = st.chat_input(placeholder)

    if question:

        st.session_state.messages.append({"role": "user", "text": question, "kind": "normal"})

        with st.spinner("Thinking..."):
            try:
                if st.session_state.awaiting_clarification:
                    # This message is the user's answer to a pending
                    # clarifying question -- send it to /clarify, not /ask.
                    backend_clarify(st.session_state.session_id, question)
                else:
                    backend_ask(st.session_state.session_id, question)

                result = backend_poll_status(st.session_state.session_id)
                handle_backend_result(result)

            except requests.exceptions.ConnectionError:
                handle_backend_result({"status": "connection_error"})
            except requests.exceptions.HTTPError as exc:
                try:
                    detail = exc.response.json().get("detail", str(exc))
                except Exception:
                    detail = str(exc)
                st.session_state.messages.append(
                    {"role": "assistant", "text": f"Sorry, that didn't work: {detail}", "kind": "error"}
                )
                st.session_state.awaiting_clarification = False
            except Exception as exc:
                st.session_state.messages.append(
                    {"role": "assistant", "text": f"Something unexpected happened: {exc}", "kind": "error"}
                )
                st.session_state.awaiting_clarification = False

        st.rerun()