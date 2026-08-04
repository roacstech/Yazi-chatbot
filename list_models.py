import os
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

api_key = os.getenv("GEMINI_API_KEY")
print(f"API Key (first 10 chars): {api_key[:10]}...")

client = genai.Client(api_key=api_key)

def get_pnr_status(pnr: str) -> str:
    """Queries the database to get the flight booking status for a given PNR."""
    return f"PNR {pnr} is held. Flight from BLR to DXB on 2026-08-15."

try:
    chat = client.chats.create(
        model='gemini-2.0-flash',
        config=types.GenerateContentConfig(
            system_instruction="You are a travel assistant.",
            temperature=0.7,
            tools=[get_pnr_status]
        )
    )
    response = chat.send_message("hello")
    print(f"SUCCESS! Response: {response.text}")
except Exception as e:
    print(f"ERROR: {type(e).__name__}: {e}")
