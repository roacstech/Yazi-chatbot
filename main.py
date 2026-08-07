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

def get_agent_dashboard_stats() -> str:
    """Queries the database to retrieve live Agent Dashboard metrics (Total Bookings, Today's Sales, Active Queue Tickets, Pending PNRs, Confirmed, Cancelled). CALL THIS TOOL IMMEDIATELY whenever the user asks for total bookings, tickets in queue, queue list count, today's sales, pending PNRs, confirmed, cancelled, or dashboard stats."""
    db = SessionLocal()
    try:
        total_trips = db.execute(text("SELECT COUNT(*) FROM bookings")).scalar() or 0
        
        # Only count bookings as active queue tickets if status is 'held' or 'pending' AND not expired
        active_pending_pnrs = db.execute(text(
            "SELECT COUNT(*) FROM bookings WHERE LOWER(status) IN ('held', 'pending') AND (expires_at IS NULL OR expires_at > NOW())"
        )).scalar() or 0
        
        expired_pnrs = db.execute(text(
            "SELECT COUNT(*) FROM bookings WHERE LOWER(status) = 'expired' OR (LOWER(status) IN ('held', 'pending') AND expires_at <= NOW())"
        )).scalar() or 0

        confirmed = db.execute(text("SELECT COUNT(*) FROM bookings WHERE LOWER(status) IN ('ticketed', 'confirmed')")).scalar() or 0
        cancelled = db.execute(text("SELECT COUNT(*) FROM bookings WHERE LOWER(status) IN ('cancelled', 'void')")).scalar() or 0
        
        # Today's sales calculation
        sales_val = db.execute(text("SELECT SUM(CAST(total AS DECIMAL(10,2))) FROM bookings WHERE LOWER(status) IN ('ticketed', 'confirmed') AND DATE(created_at) = CURDATE()")).scalar()
        today_sales = float(sales_val) if sales_val is not None else 0.0
        
        stats = {
            "today_sales": f"${today_sales:.2f} USD",
            "total_trips": int(total_trips),
            "active_queue_tickets": int(active_pending_pnrs),
            "pending_pnrs": int(active_pending_pnrs),
            "expired_pnrs": int(expired_pnrs),
            "confirmed_bookings": int(confirmed),
            "cancelled_bookings": int(cancelled)
        }
        return json.dumps(stats)
    except Exception as e:
        return json.dumps({
            "today_sales": "$0.00 USD",
            "total_trips": 0,
            "active_queue_tickets": 0,
            "pending_pnrs": 0,
            "expired_pnrs": 0,
            "confirmed_bookings": 0,
            "cancelled_bookings": 0,
            "note": "Unable to calculate live stats directly from database."
        })
    finally:
        db.close()

_CURRENT_USER_ID = None

def get_queue_list_status(queue_id: int = 8, userid: Optional[int] = None) -> str:
    """Queries the live Amadeus / Backend API to fetch real queue items and actual PNR count for a specific queue ID (e.g. Queue 8 for Ticketing Time Limits, Queue 5 for Ticketing Arrangements, Queue 0 for General Messages, Queue 2 for Schedule Changes, Queue 12 for Cancellations, Queue 23 for Quality Control). ALWAYS call this tool when user asks how many tickets/PNRs are in queue 8, queue 5, or any queue list."""
    try:
        global _CURRENT_USER_ID
        effective_userid = userid if userid is not None else _CURRENT_USER_ID
        q_id = int(queue_id)
        url = f"http://127.0.0.1:5000/api/amadeus/flights/queues/{q_id}?category=0&max=250"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            res_data = response.json()
            data_field = res_data.get("data", {})
            if isinstance(data_field, dict):
                pnr_list = data_field.get("data", [])
            elif isinstance(data_field, list):
                pnr_list = data_field
            else:
                pnr_list = []
            
            queue_names = {
                5: "Ticketing Arrangements (Queue 5)",
                8: "Ticketing Time Limits (Queue 8)",
                0: "General Messages (Queue 0)",
                2: "Schedule Changes (Queue 2)",
                12: "Cancellations (Queue 12)",
                23: "Quality Control (Queue 23)"
            }
            q_name = queue_names.get(q_id, f"Queue {q_id}")
            
            agent_pnrs = []
            all_pnrs = []
            
            if isinstance(pnr_list, list):
                for item in pnr_list:
                    ref = item.get("reference")
                    if ref:
                        all_pnrs.append(ref)
                        agent_info = item.get("agentInfo", {})
                        a_id = agent_info.get("agent_userid")
                        if a_id and (effective_userid is None or str(a_id) == str(effective_userid)):
                            agent_pnrs.append(ref)
                        elif agent_info and agent_info.get("agent_name") and agent_info.get("agent_name") != "Unknown Agent":
                            agent_pnrs.append(f"{ref} ({agent_info.get('agent_name')})")

            if effective_userid:
                final_pnrs = agent_pnrs
            else:
                final_pnrs = agent_pnrs if agent_pnrs else all_pnrs
                
            count = len(final_pnrs)
            
            return json.dumps({
                "queue_id": q_id,
                "queue_name": q_name,
                "total_tickets": count,
                "pnr_list": final_pnrs
            })
        else:
            return json.dumps({
                "queue_id": q_id,
                "total_tickets": 0,
                "pnr_list": [],
                "note": f"Queue API returned status {response.status_code}"
            })
    except Exception as e:
        return json.dumps({
            "queue_id": queue_id,
            "total_tickets": 0,
            "pnr_list": [],
            "error": str(e)
        })

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
            
        payload = {
            "currencyCode": "USD",
            "travelers": travelers
        }
        
        if return_date:
            payload["originDestinations"] = [
                {
                    "id": "1",
                    "originLocationCode": origin,
                    "destinationLocationCode": destination,
                    "departureDateTimeRange": {"date": departure_date}
                },
                {
                    "id": "2",
                    "originLocationCode": destination,
                    "destinationLocationCode": origin,
                    "departureDateTimeRange": {"date": return_date}
                }
            ]
        else:
            payload["originLocationCode"] = origin
            payload["destinationLocationCode"] = destination
            payload["departureDate"] = departure_date
        response = requests.post(url, json=payload, timeout=60)
        
        if response.status_code != 200:
            return f"Failed to search flights. Server returned {response.status_code}."
            
        data = response.json().get("data", {})
        offers = data.get("outboundOffers", []) or data.get("roundTripOffers", [])
        
        if not offers:
            return "[]"
            
        import json
        structured_offers = []
        for offer in offers[:5]:
            price = offer.get("price", {}).get("total", "Unknown")
            currency = offer.get("price", {}).get("currency", "USD")
            
            itineraries = offer.get("itineraries", [])
            if itineraries:
                segments = itineraries[0].get("segments", [])
                if segments:
                    departure_time = segments[0].get("departure", {}).get("at", "Unknown")
                    arrival_time = segments[-1].get("arrival", {}).get("at", "Unknown")
                    airline = segments[0].get("carrierCode", "Unknown")
                    stops = len(segments) - 1
                    
                    structured_offers.append({
                        "airline": airline,
                        "origin": origin,
                        "destination": destination,
                        "departure_time": departure_time,
                        "arrival_time": arrival_time,
                        "stops": stops,
                        "price": price,
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

def extract_queue_info_from_message(user_message: str, userid: Optional[str] = None) -> Optional[str]:
    lower_msg = user_message.lower()
    if "queue" in lower_msg or "tcket" in lower_msg or "ticket" in lower_msg or "pnr" in lower_msg:
        queue_id = 5
        if "queue 8" in lower_msg or "queue8" in lower_msg:
            queue_id = 8
        elif "queue 5" in lower_msg or "queue5" in lower_msg:
            queue_id = 5
        elif "queue 0" in lower_msg or "queue0" in lower_msg:
            queue_id = 0
        elif "queue 2" in lower_msg or "queue2" in lower_msg:
            queue_id = 2
        elif "queue 12" in lower_msg or "queue12" in lower_msg:
            queue_id = 12
        elif "queue 23" in lower_msg or "queue23" in lower_msg:
            queue_id = 23
            
        try:
            return get_queue_list_status(queue_id=queue_id, userid=userid)
        except Exception as e:
            return None
    return None

class ChatRequest(BaseModel):
    message: str
    senderId: str = "default"
    auth: Optional[str] = None
    roleid: Optional[str] = None
    userid: Optional[str] = None

class RasaResponse(BaseModel):
    recipient_id: str
    text: str

@app.post("/api/chat", response_model=List[RasaResponse])
async def chat_endpoint(request: ChatRequest):
    if not client:
        raise HTTPException(status_code=500, detail="Gemini API Key is missing. Please configure it in .env")

    global _CURRENT_USER_ID
    _CURRENT_USER_ID = request.userid

    db = SessionLocal()
    try:
        session_id = request.senderId
        user_message = request.message
        
        # Check if user message is asking about queue list or tickets in queue
        live_queue_context = ""
        if any(w in user_message.lower() for w in ["queue", "tcket", "ticket", "pnrs"]):
            q_info = extract_queue_info_from_message(user_message, request.userid)
            if q_info:
                live_queue_context = (
                    f"\n\n=== LIVE REAL-TIME QUEUE DATA FROM AMADEUS API ===\n"
                    f"{q_info}\n"
                    f"CRITICAL INSTRUCTION: Use the above live data numbers and PNR list to answer the user's question directly!"
                )
        
        # Save user message and fetch history
        if redis_client:
            user_msg_dict = {"role": "user", "message": user_message, "created_at": datetime.utcnow().isoformat()}
            redis_client.rpush(f"chat_session:{session_id}", json.dumps(user_msg_dict))
            redis_client.ltrim(f"chat_session:{session_id}", -40, -1)
            redis_client.expire(f"chat_session:{session_id}", 86400)
            
            raw_history = redis_client.lrange(f"chat_session:{session_id}", 0, -1)
            history = [json.loads(msg) for msg in raw_history]
        else:
            history_objs = db.query(ChatHistory).filter(ChatHistory.session_id == session_id).order_by(ChatHistory.id.desc()).limit(20).all()
            history_objs.reverse()
            history = [{"role": msg.role, "message": msg.message} for msg in history_objs]

        # Build contents for Gemini history
        contents = []
        
        # Comprehensive System instruction context trained on all Yazi Agent Menus
        system_instruction = (
            "You are the official AI Assistant for Yazi Traveler, a premier B2B Travel Management Platform dedicated to Travel Agents.\n"
            "Your main goal is to assist Travel Agents in understanding and using all Agent Menus, navigation paths, flight search, booking workflows, and policies.\n\n"
            "=== AGENT MENUS & APPLICATION STRUCTURE ===\n"
            "The Yazi Traveler Agent portal consists of the following menus and sections in the sidebar navigation:\n\n"
            "1. BOOKINGS\n"
            "   - Flights (Path: /dashboard or /dashboard/flights):\n"
            "     * Search live flights (One-Way, Round-Trip, Multi-City) across global airlines.\n"
            "     * Filter results by departure/arrival times, stops (Non-stop, 1 stop, 2+ stops), and price in USD.\n"
            "     * Select Fare Brands:\n"
            "       - YBASIC: Economy Basic (Standard carry-on/checked baggage, change fees apply).\n"
            "       - YVALUE: Economy Value (Standard baggage, date changes allowed).\n"
            "       - YCOMFORT: Economy Comfort (Extra baggage, priority check-in & boarding, fully refundable options).\n"
            "     * Review leg & layover breakdowns.\n"
            "     * Enter passenger details (First Name, Last Name, Email, Phone, Passport Number, Passport Expiry).\n"
            "     * Place Flight on Hold: Generates a PNR / Booking Reference and Hold Reference. Held bookings are automatically submitted to Queue List Management for Yazi Admin ticket approval and issuance.\n\n"
            "2. TRIPS\n"
            "   - Booking Details (Path: /dashboard/trips):\n"
            "     * Overview of all agent flight bookings and trip reservations.\n"
            "     * View booking status (e.g., ON HOLD, CONFIRMED, TICKETED, CANCELLED, EXPIRED).\n"
            "     * Inspect detailed flight itineraries, passenger lists, and payment totals.\n"
            "     * Actions: Print/Download E-Tickets & Itineraries, request trip modifications or cancellations.\n\n"
            "   - Queue List Management (Path: /dashboard/queue):\n"
            "     * Real-time monitoring of all queued PNR bookings.\n"
            "     * Queue Categories:\n"
            "       - Ticketing Arrangements (Queue 5): Bookings on hold awaiting ticket approval & issuance.\n"
            "       - General Messages (Queue 0): System & airline notifications.\n"
            "       - Schedule Changes (Queue 2): Airline-initiated flight time or route schedule changes.\n"
            "       - Ticketing Time Limits (Queue 8): Deadlines for hold expiry to prevent cancellation.\n"
            "       - Cancellations (Queue 12): Cancelled flight segments.\n"
            "       - Quality Control (Queue 23): QC review records.\n"
            "     * Track status chips (ON HOLD, PENDING, ISSUED) and hold expiry countdowns.\n\n"
            "   - Reports (Path: /dashboard/overview or /dashboard/reports):\n"
            "     * Business analytics and performance reports for the Travel Agent.\n"
            "     * View total booking volume, revenue breakdown, commission earnings, monthly/yearly sales charts, and transaction history.\n\n"
            "3. SETTINGS & STATUS\n"
            "   - Settings (Path: /dashboard/settings):\n"
            "     * The Settings page is dedicated to changing your account Password (Current Password, New Password, Confirm Password -> Save Changes).\n\n"
            "   - Profile & Business Details (Path: /dashboard/profile):\n"
            "     * Click your Avatar in the top right corner of the header and select 'View Profile' (or go to /dashboard/profile).\n"
            "     * To update Profile Image: Click the camera icon badge on your avatar picture at the top of the profile page and upload your image.\n"
            "     * To update Business Details & Address: Under the 'Business Information' section, click the 'Edit' button to update your Legal Business Name, Email, Phone, Country, State/Province, City, and Business Address.\n\n"
            "   - Support (Path: /dashboard/support):\n"
            "     * Customer help desk and agent assistance.\n"
            "     * Submit support tickets, contact Yazi support team, view baggage policies, visa/passport guidance, and travel terms.\n\n"
            "=== OFFICIAL YAZI TRAVELER CONTACT INFORMATION ===\n"
            "If a user asks for contact details, phone number, email, or head office address for Yazi Admin or Support:\n"
            "- Email: support@yazitravels.com\n"
            "- Phone: +1 (320) 406-6287\n"
            "- Head Office: 3417 3rd St N, Saint Cloud MN 56303, United States\n\n"
            "=== CRITICAL MANDATORY DIRECT RESPONSE RULES ===\n"
            "1. DIRECT ANSWERS FIRST: ALWAYS answer the user's question directly with the exact requested information (email, phone, stats, numbers, policies, answers).\n"
            "2. DO NOT GIVE NAVIGATION INSTRUCTIONS: Do NOT output navigation steps ('Go to sidebar navigation... Click on X...'), UNLESS the user explicitly asks 'how do I navigate to...' or 'where is the menu located'.\n"
            "3. CONTACT INQUIRIES: When a user asks how to contact admin, or asks for email/phone (e.g. 'how to contact admin give me a phone and email'), IMMEDIATELY answer directly:\n"
            "   - Phone: +1 (320) 406-6287\n"
            "   - Email: support@yazitravels.com\n"
            "   - Head Office: 3417 3rd St N, Saint Cloud MN 56303, United States\n"
            "4. PROFILE IMAGE & BUSINESS ADDRESS UPDATES:\n"
            "   - When user asks 'how to update profile image or business details/address?':\n"
            "     Explain clearly:\n"
            "     1. Click your Avatar in the top-right header and select 'View Profile' (or go to /dashboard/profile).\n"
            "     2. To update Profile Image: Click the camera icon badge on your avatar picture at the top of the profile page.\n"
            "     3. To update Business Details & Address: Under 'Business Information', click the 'Edit' button to update Legal Business Name, Email, Phone, Country, State/Province, City, and Business Address.\n"
            "5. PASSWORD UPDATES:\n"
            "   - When user asks 'how to change password?': Explain to go to Settings (/dashboard/settings) -> enter Current Password, New Password, Confirm Password -> click 'Save Changes'.\n"
            "=== CRITICAL MANDATORY QUEUE LIST TOOL RULE ===\n"
            "Whenever the user asks about tickets in any queue list (such as 'how many tickets are in queue 8?', 'how many tickets in queue list?', 'queue 8 count', 'queue 5 count', 'how many tickets in queue list'):\n"
            "1. YOU MUST EXCLUSIVELY CALL THE `get_queue_list_status` TOOL (pass queue_id=8 if asked about queue 8, or queue_id=5 if asked about queue 5 / ticketing arrangements / queue list).\n"
            "2. DO NOT CALL `get_agent_dashboard_stats` FOR QUEUE SPECIFIC QUESTIONS.\n"
            "3. DO NOT ANSWER 0 OR 2 FROM OLD MEMORY.\n"
            "4. REPORT THE EXACT `total_tickets` AND `pnr_list` RETURNED BY `get_queue_list_status` DIRECTLY TO THE USER!\n"
            "   Example: If `get_queue_list_status(queue_id=8)` returns `total_tickets: 4` and `pnr_list: ['BX383Y', 'BX9YNR', 'BXHK6A', 'C5PU5K']`, your response MUST be: 'There are currently 4 tickets in Queue 8 (Ticketing Time Limits): C5PU5K, BX383Y, BX9YNR, BXHK6A.'\n\n"
            "=== TOOL CALLING INSTRUCTIONS ===\n"
            "- If a user asks for PNR status, ALWAYS call the `get_pnr_status` tool.\n"
            "- If a user asks to search for flights, call the `search_flights` tool with origin IATA, destination IATA, and departure date (YYYY-MM-DD).\n"
            "- If a user asks for general dashboard stats ('today sales', 'total bookings', 'confirmed bookings'), call the `get_agent_dashboard_stats` tool.\n"
            "- If a user asks about tickets or PNRs in Queue 8, Queue 5, Queue 0, Queue 2, Queue 12, Queue 23, or Queue List ('how many tickets in queue 8?'), ALWAYS call the `get_queue_list_status` tool with that `queue_id`!\n\n"
            "CRITICAL MANDATORY INSTRUCTION FOR FLIGHT SEARCH:\n"
            "Whenever search_flights returns flight options, you MUST ALWAYS output the returned JSON flight options array inside a markdown code block tagged with `flight_options` so the UI can render the flight cards!\n"
            "Example:\n"
            "Here are the available flight options:\n"
            "```flight_options\n"
            "[...json array here...]\n"
            "```\n\n"
            "=== RESPONSE STYLE ===\n"
            "Be clear, professional, concise, direct, and helpful. Always give direct answers with real live data numbers without unprompted navigation steps!"
        ) + live_queue_context
        
        # We don't want to include the very last user message in the history parameter of chats.create
        history_for_chat = history[:-1] if history else []
        last_user_msg = history[-1]["message"] if history else user_message

        for msg in history_for_chat:
            role = "user" if msg["role"] == "user" else "model"
            contents.append(
                types.Content(role=role, parts=[types.Part.from_text(text=msg["message"])])
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
                        tools=[get_pnr_status, search_flights, get_agent_dashboard_stats, get_queue_list_status]
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
        
        # Save bot response to DB (Permanent storage)
        db_bot_msg = ChatHistory(session_id=session_id, role="model", message=bot_text)
        db.add(db_bot_msg)
        db.commit()
        
        # Save bot response to Redis
        if redis_client:
            bot_msg_dict = {"role": "model", "message": bot_text, "created_at": datetime.utcnow().isoformat()}
            redis_client.rpush(f"chat_session:{session_id}", json.dumps(bot_msg_dict))

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
    db = SessionLocal()
    try:
        history = []
        if redis_client:
            raw_history = redis_client.lrange(f"chat_session:{session_id}", 0, -1)
            if raw_history:
                history = [json.loads(msg) for msg in raw_history]
                
        if not history:
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
        
    db = SessionLocal()
    try:
        db.execute(text(f"DELETE FROM chat_histories WHERE session_id='{session_id}'"))
        db.commit()
        return {"success": True, "message": "Deleted from history"}
    except Exception as e:
        db.rollback()
        return {"success": False, "error": str(e)}
    finally:
        db.close()

@app.get("/api/chat/sessions")
async def get_all_sessions():
    sessions = []
    seen_ids = set()
    
    if redis_client:
        keys = redis_client.keys("chat_session:*")
        for key in keys:
            session_id = key.replace("chat_session:", "")
            raw = redis_client.lindex(key, 0)
            if raw:
                msg = json.loads(raw)
                sessions.append({"id": session_id, "preview": msg.get("message", "")[:40] + "...", "timestamp": msg.get("created_at")})
                seen_ids.add(session_id)
                
    db = SessionLocal()
    try:
        result = db.execute(text("SELECT session_id, MIN(created_at) as timestamp FROM chat_histories GROUP BY session_id ORDER BY timestamp DESC LIMIT 50"))
        for row in result:
            s_id = row[0]
            ts = row[1]
            if s_id not in seen_ids:
                msg_row = db.execute(text(f"SELECT message FROM chat_histories WHERE session_id='{s_id}' ORDER BY id ASC LIMIT 1")).fetchone()
                preview = msg_row[0][:40] + "..." if msg_row else "Previous Chat"
                sessions.append({"id": s_id, "preview": preview, "timestamp": str(ts)})
                seen_ids.add(s_id)
                
        sessions.sort(key=lambda x: str(x.get("timestamp", "")), reverse=True)
        return {"success": True, "sessions": sessions}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        db.close()
            

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8001)
