"""One-time backfill: enrich existing contacts with Apollo data.

Usage:
    set APOLLO_API_KEY=your-key   (or add to backend/.env)
    python enrich.py

Only touches rows where apollo_raw IS NULL and linkedin_url is set,
so it is safe to re-run.
"""
import json
import os
import time
import urllib.request

from dotenv import load_dotenv
load_dotenv()

from psycopg2.extras import Json

from main import derive_apollo_fields, get_conn, release_conn

API_KEY = os.environ["APOLLO_API_KEY"]


def apollo_match(linkedin_url: str) -> dict | None:
    req = urllib.request.Request(
        "https://api.apollo.io/api/v1/people/match",
        data=json.dumps({"linkedin_url": linkedin_url}).encode(),
        headers={"Content-Type": "application/json", "x-api-key": API_KEY},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read()).get("person")


def main():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT uid, linkedin_url FROM contacts "
                "WHERE apollo_raw IS NULL AND linkedin_url IS NOT NULL"
            )
            rows = cur.fetchall()
        print(f"{len(rows)} contacts to enrich")

        for i, (uid, linkedin_url) in enumerate(rows, 1):
            try:
                person = apollo_match(linkedin_url)
            except Exception as e:
                print(f"[{i}/{len(rows)}] {uid} {linkedin_url} — Apollo error: {e}")
                continue

            if not person:
                print(f"[{i}/{len(rows)}] {uid} {linkedin_url} — no Apollo match")
                continue

            fields = derive_apollo_fields(person)
            fields["apollo_raw"] = Json(person)
            sets = ", ".join(f"{k} = %s" for k in fields)
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE contacts SET {sets} WHERE uid = %s",
                    (*fields.values(), uid),
                )
            conn.commit()
            print(f"[{i}/{len(rows)}] {uid} — enriched ({fields.get('title')} @ {fields.get('company_name')})")
            time.sleep(1)  # stay well under Apollo rate limits
    finally:
        release_conn(conn)


if __name__ == "__main__":
    main()
