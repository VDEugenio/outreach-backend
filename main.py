import os
import random
import string
from datetime import date, datetime
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

import psycopg2
import psycopg2.pool
from psycopg2.extras import Json
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
                ALTER TABLE visits
                    ADD COLUMN IF NOT EXISTS user_agent TEXT,
                    ADD COLUMN IF NOT EXISTS kind TEXT;
                ALTER TABLE contacts
                    ADD COLUMN IF NOT EXISTS title             TEXT,
                    ADD COLUMN IF NOT EXISTS seniority         TEXT,
                    ADD COLUMN IF NOT EXISTS departments       TEXT,
                    ADD COLUMN IF NOT EXISTS company_name      TEXT,
                    ADD COLUMN IF NOT EXISTS company_size      INTEGER,
                    ADD COLUMN IF NOT EXISTS company_industry  TEXT,
                    ADD COLUMN IF NOT EXISTS city              TEXT,
                    ADD COLUMN IF NOT EXISTS state             TEXT,
                    ADD COLUMN IF NOT EXISTS country           TEXT,
                    ADD COLUMN IF NOT EXISTS years_at_company  NUMERIC,
                    ADD COLUMN IF NOT EXISTS email_status      TEXT,
                    ADD COLUMN IF NOT EXISTS apollo_raw        JSONB,
                    ADD COLUMN IF NOT EXISTS premium           BOOLEAN,
                    ADD COLUMN IF NOT EXISTS follower_count    INTEGER,
                    ADD COLUMN IF NOT EXISTS connection_degree TEXT,
                    ADD COLUMN IF NOT EXISTS target_role       TEXT,
                    ADD COLUMN IF NOT EXISTS target_company    TEXT,
                    ADD COLUMN IF NOT EXISTS contacted_at      TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS channel           TEXT,
                    ADD COLUMN IF NOT EXISTS responded         BOOLEAN,
                    ADD COLUMN IF NOT EXISTS responded_at      TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS outcome           TEXT;
            """)
        conn.commit()
    finally:
        release_conn(conn)


init_db()

CHARS = string.ascii_lowercase + string.digits  # a-z0-9, 36^3 = 46,656 UIDs


def generate_uid():
    return "".join(random.choices(CHARS, k=3))


# ── Apollo field derivation ───────────────────────────────────────────────────

def derive_apollo_fields(person: dict) -> dict:
    """Promote the queryable fields out of a raw Apollo person object."""
    if not person:
        return {}
    org = person.get("organization") or {}

    years = None
    for job in person.get("employment_history") or []:
        if job.get("current") and job.get("start_date"):
            try:
                start = datetime.strptime(job["start_date"][:10], "%Y-%m-%d").date()
                years = round((date.today() - start).days / 365.25, 1)
            except ValueError:
                pass
            break

    deps = person.get("departments")
    return {
        "title": person.get("title"),
        "seniority": person.get("seniority"),
        "departments": ", ".join(deps) if deps else None,
        "company_name": person.get("organization_name") or org.get("name"),
        "company_size": org.get("estimated_num_employees"),
        "company_industry": org.get("industry"),
        "city": person.get("city"),
        "state": person.get("state"),
        "country": person.get("country"),
        "years_at_company": years,
        "email_status": person.get("email_status"),
    }


# ── Models ────────────────────────────────────────────────────────────────────

class ContactRequest(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    linkedin_url: Optional[str] = None
    apollo_raw: Optional[dict] = None
    page: Optional[dict] = None  # {premium, follower_count, connection_degree}
    target_role: Optional[str] = None
    target_company: Optional[str] = None


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

            # Apply enrichment fields (both insert and update paths)
            fields = {}
            if body.apollo_raw:
                fields.update(derive_apollo_fields(body.apollo_raw))
                fields["apollo_raw"] = Json(body.apollo_raw)
            if body.page:
                fields["premium"] = body.page.get("premium")
                fields["follower_count"] = body.page.get("follower_count")
                fields["connection_degree"] = body.page.get("connection_degree")
            if body.target_role:
                fields["target_role"] = body.target_role
            if body.target_company:
                fields["target_company"] = body.target_company
            if fields:
                sets = ", ".join(f"{k} = %s" for k in fields)
                cur.execute(
                    f"UPDATE contacts SET {sets} WHERE uid = %s",
                    (*fields.values(), uid),
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


class ContactedRequest(BaseModel):
    channel: Optional[str] = None  # 'copy' | 'email'


@app.post("/contacts/{uid}/contacted")
def mark_contacted(uid: str, body: ContactedRequest):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE contacts SET contacted_at = NOW(), channel = %s WHERE uid = %s RETURNING uid",
                (body.channel, uid),
            )
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Contact not found")
        conn.commit()
    finally:
        release_conn(conn)
    return {"ok": True}


BOT_UA_MARKERS = [
    "linkedinbot", "slackbot", "twitterbot", "facebookexternalhit", "whatsapp",
    "telegrambot", "discordbot", "skypeuripreview", "googlebot", "bingbot",
    "bot", "crawler", "spider", "apache-httpclient", "python-requests",
    "curl", "wget", "headlesschrome",
]


def is_bot(ua: str) -> bool:
    if not ua:
        return True
    ua_lower = ua.lower()
    return any(marker in ua_lower for marker in BOT_UA_MARKERS)


@app.get("/r/{uid}")
def resolve_uid(uid: str, request: Request):
    # The RAG-backend proxy forwards the real visitor via custom headers,
    # because Railway's edge strips X-Forwarded-For from proxied requests.
    ip = (
        request.headers.get("x-client-ip")
        or request.headers.get("x-forwarded-for", request.client.host).split(",")[0].strip()
    )
    ua = request.headers.get("x-client-ua") or request.headers.get("user-agent", "")

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

            # Log every fetch; bots are kept but marked so stats can exclude them
            kind = "bot" if is_bot(ua) else "human"
            cur.execute(
                "INSERT INTO visits (uid, ip, user_agent, kind) VALUES (%s, %s, %s, %s)",
                (uid, ip, ua, kind),
            )
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
                "SELECT visited_at, ip, user_agent FROM visits "
                "WHERE uid = %s AND kind = 'human' ORDER BY visited_at DESC",
                (uid,),
            )
            visits = [
                {"visited_at": r[0].isoformat(), "ip": r[1], "user_agent": r[2]}
                for r in cur.fetchall()
            ]
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
