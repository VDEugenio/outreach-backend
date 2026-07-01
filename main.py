import os
import random
import string
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

import psycopg2
import psycopg2.pool
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

DSN = os.environ["DATABASE_URL"]
pool = psycopg2.pool.SimpleConnectionPool(1, 10, dsn=DSN)


def get_conn():
    conn = pool.getconn()
    try:
        conn.cursor().execute("SELECT 1")
    except Exception:
        # Connection is dead — replace it
        pool.putconn(conn, close=True)
        conn = psycopg2.connect(DSN)
        pool._pool.append(conn)
        conn = pool.getconn()
    return conn


def release_conn(conn):
    pool.putconn(conn)


def init_db():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS contacts (
                    uid          TEXT PRIMARY KEY,
                    first_name   TEXT,
                    last_name    TEXT,
                    linkedin_url TEXT,
                    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS visits (
                    id         SERIAL PRIMARY KEY,
                    uid        TEXT NOT NULL REFERENCES contacts(uid),
                    ip         TEXT,
                    visited_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
        conn.commit()
    finally:
        release_conn(conn)


init_db()

CHARS = string.ascii_lowercase + string.digits  # a-z0-9, 36^3 = 46,656 UIDs


def generate_uid():
    return "".join(random.choices(CHARS, k=3))


# ── Models ────────────────────────────────────────────────────────────────────

class ContactRequest(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    linkedin_url: Optional[str] = None


class ContactResponse(BaseModel):
    uid: str
    first_name: Optional[str]
    last_name: Optional[str]
    linkedin_url: Optional[str]
    tracking_url: str


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/contacts", response_model=ContactResponse)
def upsert_contact(body: ContactRequest):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Return existing UID if this linkedin_url already has one
            if body.linkedin_url:
                cur.execute("SELECT uid FROM contacts WHERE linkedin_url = %s", (body.linkedin_url,))
                row = cur.fetchone()
            else:
                row = None

            if row:
                uid = row[0]
                # Update name fields in case they changed
                cur.execute(
                    "UPDATE contacts SET first_name = %s, last_name = %s WHERE uid = %s",
                    (body.first_name, body.last_name, uid),
                )
                conn.commit()
            else:
                for _ in range(10):
                    uid = generate_uid()
                    cur.execute("SELECT 1 FROM contacts WHERE uid = %s", (uid,))
                    if not cur.fetchone():
                        break
                else:
                    raise HTTPException(status_code=500, detail="Could not generate unique UID")

                cur.execute(
                    "INSERT INTO contacts (uid, first_name, last_name, linkedin_url) VALUES (%s, %s, %s, %s)",
                    (uid, body.first_name, body.last_name, body.linkedin_url),
                )
                conn.commit()
    finally:
        release_conn(conn)

    return ContactResponse(
        uid=uid,
        first_name=body.first_name,
        last_name=body.last_name,
        linkedin_url=body.linkedin_url,
        tracking_url=f"https://vaughneugenio.com/r/{uid}",
    )


@app.get("/r/{uid}")
def resolve_uid(uid: str, request: Request):
    ip = request.headers.get("x-forwarded-for", request.client.host).split(",")[0].strip()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT first_name, last_name, linkedin_url FROM contacts WHERE uid = %s",
                (uid,)
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Link not found")

            cur.execute("INSERT INTO visits (uid, ip) VALUES (%s, %s)", (uid, ip))
            conn.commit()
    finally:
        release_conn(conn)

    return {
        "uid": uid,
        "first_name": row[0],
        "last_name": row[1],
        "linkedin_url": row[2],
    }


@app.get("/contacts/{uid}/visits")
def get_visits(uid: str):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT first_name, last_name, linkedin_url FROM contacts WHERE uid = %s",
                (uid,)
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Contact not found")

            cur.execute(
                "SELECT visited_at, ip FROM visits WHERE uid = %s ORDER BY visited_at DESC",
                (uid,),
            )
            visits = [{"visited_at": r[0].isoformat(), "ip": r[1]} for r in cur.fetchall()]
    finally:
        release_conn(conn)

    return {
        "uid": uid,
        "first_name": row[0],
        "last_name": row[1],
        "linkedin_url": row[2],
        "visit_count": len(visits),
        "visits": visits,
    }
