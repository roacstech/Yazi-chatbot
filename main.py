import os
import time
import re
from typing import List, Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, text
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime

# Import Gemini SDK
from google import genai
from google.genai import types
import requests
import redis
import json

load_dotenv()

app = FastAPI(title="Yazi Chatbot API")

# Setup Database Connection
MYSQL_URL = os.getenv("MYSQL_URL")
if not MYSQL_URL:
    raise ValueError("MYSQL_URL is not set in environment variables.")

engine = create_engine(MYSQL_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
try:
    redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    redis_client.ping()
    print("Connected to Redis successfully.")
except Exception as e:
    print(f"Failed to connect to Redis: {e}")
    redis_client = None

class ChatHistory(Base):
    __tablename__ = "chat_histories"
    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String(255), index=True, nullable=False)
    role = Column(String(50), nullable=False)
    message = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class Booking(Base):
    __tablename__ = "bookings"
    id = Column(Integer, primary_key=True, index=True)
    pnr = Column(String(20))
    status = Column(String(50))
    origin_iata = Column(String(3))
    destination_iata = Column(String(3))
    departure_date = Column(DateTime)
    total = Column(String(50)) # Keeping simple as String
    currency = Column(String(3))

# Auto-create tables if they don't exist
Base.metadata.create_all(bind=engine)

def get_pnr_status(pnr: str) -> str:
    """Queries the database to get the flight booking status for a given PNR."""
    db = SessionLocal()
    try:
        booking = db.query(Booking).filter(Booking.pnr.like(f"%{pnr}%")).first()
        if not booking:
            return f"No booking found with PNR: {pnr}"
        return f"PNR {pnr} is {booking.status}. Flight from {booking.origin_iata} to {booking.destination_iata} on {booking.departure_date}. Total cost: {booking.total} {booking.currency}."
    except Exception as e:
        return f"Error retrieving PNR status: {str(e)}"
    finally:
        db.close()

def search_flights(origin: str, destination: str, departure_date: str, return_date: str = None, adults: int = 1, children: int = 0, infants: int = 0) -> str:
    """Searches for available flights between two locations on specific dates. 
    Args:
        origin: IATA code of origin (e.g., MSP)
        destination: IATA code of destination (e.g., NBO)
        departure_date: Date in YYYY-MM-DD format
        return_date: Optional return date in YYYY-MM-DD format for round trips.
        adults: Number of adult passengers.
        children: Number of child passengers.
        infants: Number of infant passengers.
    """
    try:
        # Sanitize origin & destination IATA codes to uppercase 3-letter codes
        origin = re.sub(r'[^A-Za-z]', '', str(origin))[-3:].upper()
        destination = re.sub(r'[^A-Za-z]', '', str(destination))[-3:].upper()

        url = "http://127.0.0.1:5000/api/amadeus/flights/flight-offers"
        
        adults = int(adults)
        children = int(children)
        infants = int(infants)
        
        travelers = []
        traveler_id = 1
        for _ in range(adults):
            travelers.append({"id": str(traveler_id), "travelerType": "ADULT"})
            traveler_id += 1
        for _ in range(children):
            travelers.append({"id": str(traveler_id), "travelerType": "CHILD"})
            traveler_id += 1
        for _ in range(infants):
            travelers.append({"id": str(traveler_id), "travelerType": "HELD_INFANT", "associatedAdultId": "1"})
            traveler_id += 1
            
        origin_destinations = [
            {
                "id": "1",
                "originLocationCode": origin,
                "destinationLocationCode": destination,
                "departureDateTimeRange": {"date": departure_date}
            }
        ]
        if return_date:
            origin_destinations.append({
                "id": "2",
                "originLocationCode": destination,
                "destinationLocationCode": origin,
                "departureDateTimeRange": {"date": return_date}
            })
            
        payload = {
            "currencyCode": "USD",
            "originDestinations": origin_destinations,
            "travelers": travelers,
            "searchCriteria": {"maxFlightOffers": 250}
        }
        
        response = requests.post(url, json=payload, timeout=60)
        
        if response.status_code != 200:
            return f"Failed to search flights. Server returned {response.status_code}."
            
        res_json = response.json()
        data = res_json.get("data", {}) if isinstance(res_json, dict) else {}
        offers = data.get("outboundOffers", []) or data.get("roundTripOffers", []) or data.get("flightOffers", [])
        if not offers and isinstance(data, list):
            offers = data
            
        if not offers:
            return "[]"
            
        import json
        structured_offers = []
        for offer in offers[:6]:
            price_obj = offer.get("price", {})
            price = price_obj.get("grandTotal") or price_obj.get("total", "Unknown")
            currency = price_obj.get("currency", "USD")
            
            itineraries = offer.get("itineraries", [])
            if itineraries:
                segments = itineraries[0].get("segments", [])
                if segments:
                    departure_time = segments[0].get("departure", {}).get("at", "Unknown")
                    arrival_time = segments[-1].get("arrival", {}).get("at", "Unknown")
                    airline = segments[0].get("carrierCode", "Unknown")
                    stops = max(0, len(segments) - 1)
                    
                    structured_offers.append({
                        "airline": airline,
                        "origin": origin,
                        "destination": destination,
                        "departure_time": departure_time,
                        "arrival_time": arrival_time,
                        "stops": stops,
                        "price": str(price),
                        "currency": currency,
                        "adults": adults,
                        "children": children,
                        "infants": infants,
                        "raw_offer": offer  # Full Amadeus offer object (includes id, itineraries, travelerPricings, etc.)
                    })
                    
        return json.dumps(structured_offers)
    except Exception as e:
        return f"Error connecting to flight search service: {str(e)}"

# Initialize Gemini Client
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY or GEMINI_API_KEY == "YOUR_GEMINI_API_KEY_HERE":
    print("WARNING: GEMINI_API_KEY is not set correctly.")

# We pass the api key directly to the client if provided, else it looks in environment
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY and GEMINI_API_KEY != "YOUR_GEMINI_API_KEY_HERE" else None

class ChatRequest(BaseModel):
    message: str
    senderId: str = "default"

class RasaResponse(BaseModel):
    recipient_id: str
    text: str

@app.post("/api/chat", response_model=List[RasaResponse])
async def chat_endpoint(request: ChatRequest):
    if not client:
        raise HTTPException(status_code=500, detail="Gemini API Key is missing. Please configure it in .env")

    db = SessionLocal()
    try:
        session_id = request.senderId
        user_message = request.message
        
        # Save user message and fetch history
        if redis_client:
            user_msg_dict = {"role": "user", "message": user_message, "created_at": datetime.utcnow().isoformat()}
            redis_client.rpush(f"chat_session:{session_id}", json.dumps(user_msg_dict))
            redis_client.ltrim(f"chat_session:{session_id}", -40, -1)
            redis_client.expire(f"chat_session:{session_id}", 86400)
            
            raw_history = redis_client.lrange(f"chat_session:{session_id}", 0, -1)
            history = [json.loads(msg) for msg in raw_history]
        else:
            db_user_msg = ChatHistory(session_id=session_id, role="user", message=user_message)
            db.add(db_user_msg)
            db.commit()
            
            history_objs = db.query(ChatHistory).filter(ChatHistory.session_id == session_id).order_by(ChatHistory.id.desc()).limit(20).all()
            history_objs.reverse()
            history = [{"role": msg.role, "message": msg.message} for msg in history_objs]

        # Check for simple greetings/casual chat first
        GREETINGS = {"hi", "hello", "hey", "hlo", "hi there", "hello yazi", "good morning", "good afternoon", "good evening", "greetings", "help"}
        cleaned_msg = user_message.strip().lower()

        if cleaned_msg in GREETINGS:
            bot_text = "Hello! How can I assist you today with your flight search, booking, or PNR inquiry?"
        else:
            # Build contents for Gemini history
            contents = []
            
            # System instruction context
            system_instruction = (
                "You are an expert travel assistant for Yazi. Answer questions concisely and politely about flights, bookings, and travel policies.\n\n"
                "CRITICAL INSTRUCTIONS FOR CHAT:\n"
                "- If the user's message is a simple greeting, thank you, or general chat (e.g., 'hi', 'hello', 'hey', 'thanks', 'ok', 'how are you'), DO NOT call search_flights or get_pnr_status. Respond politely asking how you can help them.\n"
                "- If a user asks for a PNR, ALWAYS use the get_pnr_status tool.\n"
                "- If a user wants to search for a flight, use the search_flights tool with origin IATA code, destination IATA code, and departure date (YYYY-MM-DD).\n\n"
                "CRITICAL MANDATORY INSTRUCTION FOR FLIGHT SEARCH:\n"
                "Whenever search_flights returns flight options, you MUST ALWAYS output the returned JSON flight options array inside a markdown code block tagged with `flight_options`.\n"
                "Example:\n"
                "Here are the available flight options:\n"
                "```flight_options\n"
                "[...json array here...]\n"
                "```"
            )
            
            # We don't want to include the very last user message in the history parameter of chats.create
            history_for_chat = history[:-1] if history else []
            last_user_msg = history[-1]["message"] if history else user_message

            for msg in history_for_chat:
                role = "user" if msg["role"] == "user" else "model"
                msg_text = msg["message"]
                if role == "model":
                    msg_text = re.sub(r'```flight_options[\s\S]*?```', '[Flight options listed]', msg_text)
                    msg_text = re.sub(r'\[\s*\{[\s\S]*?"airline"[\s\S]*?\}\s*\]', '[Flight options listed]', msg_text)
                contents.append(
                    types.Content(role=role, parts=[types.Part.from_text(text=msg_text)])
                )

            # Initialize Chat session with history and tools
            models_to_try = ['gemini-3.6-flash', 'gemini-3.5-flash', 'gemini-3.5-flash-lite']
            bot_text = None
            
            for model_name in models_to_try:
                try:
                    print(f"Trying model: {model_name}")
                    chat = client.chats.create(
                        model=model_name,
                        history=contents,
                        config=types.GenerateContentConfig(
                            system_instruction=system_instruction,
                            temperature=0.7,
                            tools=[get_pnr_status, search_flights]
                        )
                    )
                    
                    response = chat.send_message(last_user_msg)
                    bot_text = response.text
                    print(f"Success with model: {model_name}")
                    break
                    
                except Exception as api_err:
                    print(f"Failed on {model_name}: {api_err}. Waiting 2s before trying next...")
                    time.sleep(2)
                    continue
        
        if bot_text is None:
            bot_text = "I'm currently busy due to high demand. Please wait a moment and try again."
        
        # Save bot response
        if redis_client:
            bot_msg_dict = {"role": "model", "message": bot_text, "created_at": datetime.utcnow().isoformat()}
            redis_client.rpush(f"chat_session:{session_id}", json.dumps(bot_msg_dict))
        else:
            db_bot_msg = ChatHistory(session_id=session_id, role="model", message=bot_text)
            db.add(db_bot_msg)
            db.commit()

        return [{"recipient_id": session_id, "text": bot_text}]
        
    except Exception as e:
        import traceback
        print(f"Error in chat endpoint: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to process chat request: {str(e)}")
    finally:
        db.close()

@app.get("/api/chat/history/{session_id}")
async def get_chat_history(session_id: str):
    if redis_client:
        raw_history = redis_client.lrange(f"chat_session:{session_id}", 0, -1)
        history = [json.loads(msg) for msg in raw_history]
        return {"success": True, "history": history}
    else:
        db = SessionLocal()
        try:
            history_objs = db.query(ChatHistory).filter(ChatHistory.session_id == session_id).order_by(ChatHistory.id.desc()).limit(40).all()
            history_objs.reverse()
            history = [{"role": msg.role, "message": msg.message, "created_at": str(msg.created_at)} for msg in history_objs]
            return {"success": True, "history": history}
        finally:
            db.close()


@app.delete("/api/chat/history/{session_id}")
async def delete_chat_history(session_id: str):
    if redis_client:
        redis_client.delete(f"chat_session:{session_id}")
        return {"success": True, "message": "Deleted from Redis"}
    else:
        db = SessionLocal()
        try:
            db.execute(text(f"DELETE FROM chat_histories WHERE session_id='{session_id}'"))
            db.commit()
            return {"success": True, "message": "Deleted from Database"}
        except Exception as e:
            db.rollback()
            return {"success": False, "error": str(e)}
        finally:
            db.close()

@app.get("/api/chat/sessions")
async def get_all_sessions():
    sessions = []
    if redis_client:
        keys = redis_client.keys("chat_session:*")
        for key in keys:
            session_id = key.replace("chat_session:", "")
            raw = redis_client.lindex(key, 0)
            if raw:
                msg = json.loads(raw)
                sessions.append({"id": session_id, "preview": msg.get("message", "")[:40] + "...", "timestamp": msg.get("created_at")})
        return {"success": True, "sessions": sessions}
    else:
        db = SessionLocal()
        try:
            # Using simple query to get sessions
            result = db.execute(text("SELECT session_id, MIN(created_at) as timestamp FROM chat_histories GROUP BY session_id ORDER BY timestamp DESC LIMIT 50"))
            for row in result:
                s_id = row[0]
                ts = row[1]
                msg_row = db.execute(text(f"SELECT message FROM chat_histories WHERE session_id='{s_id}' ORDER BY id ASC LIMIT 1")).fetchone()
                preview = msg_row[0][:40] + "..." if msg_row else "Previous Chat"
                sessions.append({"id": s_id, "preview": preview, "timestamp": str(ts)})
            return {"success": True, "sessions": sessions}
        except Exception as e:
            return {"success": False, "error": str(e)}
        finally:
            db.close()
            

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8001)
