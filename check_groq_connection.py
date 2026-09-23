"""
check_gemini_connection.py
----------------------------
Standalone sanity check -- confirms your GEMINI_API_KEY and Google's API
are reachable, before you run the full agent in main.py.

Run:
    python check_gemini_connection.py
"""

import os
from google import genai
from dotenv import load_dotenv

load_dotenv()

api_key = os.environ.get("GEMINI_API_KEY")
if not api_key:
    raise SystemExit("GEMINI_API_KEY not found. Check your .env file exists and has GEMINI_API_KEY=...")

model = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")

print("Checking Gemini connection...")

try:
    client = genai.Client(api_key=api_key)

    response = client.models.generate_content(
        model=model,
        contents="Reply with exactly: Gemini connection OK",
    )

    print("Gemini replied successfully!")
    print(response.text)

except Exception as exc:
    # Never print the API key itself -- only the error Google's SDK raised.
    print("Gemini connection FAILED.")
    print(f"Error: {exc}")
