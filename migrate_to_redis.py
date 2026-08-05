import os
import json
import redis
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

MYSQL_URL = os.getenv("MYSQL_URL")
if not MYSQL_URL:
    raise ValueError("MYSQL_URL not set")

engine = create_engine(MYSQL_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class ChatHistory(Base):
    __tablename__ = "chat_histories"
    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String(255), index=True, nullable=False)
    role = Column(String(50), nullable=False)
    message = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

redis_client = redis.from_url("redis://localhost:6379", decode_responses=True)

def migrate():
    db = SessionLocal()
    records = db.query(ChatHistory).order_by(ChatHistory.id.asc()).all()

    sessions = {}
    for r in records:
        if r.session_id not in sessions:
            sessions[r.session_id] = []
        
        msg_dict = {
            "role": r.role,
            "message": r.message,
            "created_at": r.created_at.isoformat() if r.created_at else datetime.utcnow().isoformat()
        }
        sessions[r.session_id].append(msg_dict)

    for session_id, msgs in sessions.items():
        key = f"chat_session:{session_id}"
        # Prevent duplication if already migrated
        redis_client.delete(key)
        for msg in msgs:
            redis_client.rpush(key, json.dumps(msg))
        redis_client.expire(key, 86400)
        print(f"Migrated {len(msgs)} messages for session {session_id}")

    print("Migration from MySQL to Redis complete!")
    db.close()

if __name__ == "__main__":
    migrate()
