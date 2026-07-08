"""
Day 4: Database setup.

Defines the Receipt table and gives us a way to open a database session.
Using SQLite for now (single file, zero setup) -- swapping to Postgres
later just means changing DATABASE_URL, nothing else in this file changes.
"""

from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, JSON
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = "sqlite:///./snapledger.db"

# check_same_thread=False is needed for SQLite specifically when used with FastAPI
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


class Receipt(Base):
    __tablename__ = "receipts"

    id = Column(Integer, primary_key=True, index=True)
    vendor = Column(String, nullable=True)
    date = Column(String, nullable=True)  # stored as string (YYYY-MM-DD) for simplicity
    total_amount = Column(Float, nullable=True)
    currency = Column(String, nullable=True)
    category = Column(String, nullable=True)
    confidence = Column(String, nullable=True)
    line_items = Column(JSON, nullable=True)  # stores the list of {description, amount} as JSON
    created_at = Column(DateTime, default=datetime.utcnow)


def init_db():
    """Create all tables if they don't exist yet. Call this once on app startup."""
    Base.metadata.create_all(bind=engine)


def get_db():
    """
    FastAPI dependency: gives each request its own database session,
    and makes sure it's closed afterward even if an error occurs.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()