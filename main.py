import os
import random
import string
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()

import psycopg2
import psycopg2.pool
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

pool = psycopg2.pool.SimpleConnectionPool(1, 10, dsn=os.environ["DATABASE_URL"])


def get_conn():
    return pool.getconn()


def release_conn(conn):
    pool.putconn(conn)


def init_db():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS contacts (
                    uid        TEXT PRIMARY KEY,
                    slug       TEXT NOT NULL UNIQUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS visits (
                    id         SERIAL PRIMARY KEY,
                    uid        TEXT NOT NULL REFERENCES contacts(uid),
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
    slug: str


class ContactResponse(BaseModel):
    uid: str
    slug: str
    tracking_url: str


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/contacts", response_model=ContactResponse)
def upsert_contact(body: ContactRequest):
    slug = body.slug.strip().lower()
    if not slug:
        raise HTTPException(status_code=400, detail="slug is required")

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Return existing UID if slug already has one
            cur.execute("SELECT uid FROM contacts WHERE slug = %s", (slug,))
            row = cur.fetchone()
            if row:
                uid = row[0]
            else:
                # Generate a collision-free UID
                for _ in range(10):
                    uid = generate_uid()
                    cur.execute("SELECT 1 FROM contacts WHERE uid = %s", (uid,))
                    if not cur.fetchone():
                        break
                else:
                    raise HTTPException(status_code=500, detail="Could not generate unique UID")

                cur.execute(
                    "INSERT INTO contacts (uid, slug) VALUES (%s, %s)",
                    (uid, slug),
                )
                conn.commit()
    finally:
        release_conn(conn)

    return ContactResponse(
        uid=uid,
        slug=slug,
        tracking_url=f"https://vaughneugenio.com/r/{uid}",
    )


@app.get("/r/{uid}")
def resolve_uid(uid: str):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT slug FROM contacts WHERE uid = %s", (uid,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Link not found")
            slug = row[0]

            cur.execute("INSERT INTO visits (uid) VALUES (%s)", (uid,))
            conn.commit()
    finally:
        release_conn(conn)

    return {"slug": slug, "linkedin_url": f"https://www.linkedin.com/in/{slug}"}


@app.get("/contacts/{uid}/visits")
def get_visits(uid: str):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT slug FROM contacts WHERE uid = %s", (uid,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Contact not found")

            cur.execute(
                "SELECT visited_at FROM visits WHERE uid = %s ORDER BY visited_at DESC",
                (uid,),
            )
            visits = [r[0].isoformat() for r in cur.fetchall()]
    finally:
        release_conn(conn)

    return {"uid": uid, "slug": row[0], "visit_count": len(visits), "visits": visits}
