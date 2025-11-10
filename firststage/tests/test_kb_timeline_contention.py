# -*- coding: utf-8 -*-

import os, asyncio, json, time, uuid, pytest
from psycopg2 import connect
from psycopg2.extras import Json as PgJson

DSN = os.getenv("KB_DB_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="KB_DB_DSN nie ustawione — test integracyjny")

CREATE = """
CREATE TABLE IF NOT EXISTS kb_items (
    id bigserial PRIMARY KEY,
    key text NOT NULL,
    version integer NOT NULL,
    etag uuid NOT NULL,
    content_type text NOT NULL,
    value jsonb NOT NULL,
    tags text[] NOT NULL DEFAULT '{}',
    session_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    created_by text NOT NULL,
    deleted boolean NOT NULL DEFAULT false
);
CREATE UNIQUE INDEX IF NOT EXISTS kb_items_key_version_uq ON kb_items(key, version);
"""

def _conn():
    return connect(DSN)

def _next_version(cur, key):
    cur.execute("SELECT COALESCE(MAX(version),0)+1 FROM kb_items WHERE key=%s", (key,))
    return int(cur.fetchone()[0])

@pytest.fixture(autouse=True)
def ensure_schema():
    with _conn() as c:
        with c.cursor() as cur:
            cur.execute(CREATE)


async def store_once(key, value, if_match=None):
    with _conn() as c:
        with c.cursor() as cur:
            if if_match:
                if if_match.startswith("v"):
                    expected = int(if_match[1:])
                    cur.execute("SELECT COALESCE(MAX(version),0) FROM kb_items WHERE key=%s", (key,))
                    cur_v = int(cur.fetchone()[0])
                    if cur_v != expected:
                        raise RuntimeError("CONFLICT")
            v = _next_version(cur, key)
            cur.execute(
                """
                INSERT INTO kb_items (key,version,etag,content_type,value,tags,session_id,created_by)
                VALUES (%s,%s,%s::uuid,%s,%s,%s,%s,%s)
                """,
                (key, v, str(uuid.uuid4()), "application/json", PgJson(value), ['kind:timeline'], "sess-test", "coordinator@xmpp")
            )
        return True

@pytest.mark.asyncio
async def test_timeline_conflict_and_retry():
    key = f"session:sess-{int(time.time()*1000)}:chat:timeline:main"
    # seed v1
    await store_once(key, [ {"seed":1} ])

    # równoległe próby z if_match=v1 → jedna się uda, druga rzuci konflikt
    async def writer(name):
        try:
            await store_once(key, [{"who":name}], if_match="v1")
            return "ok"
        except RuntimeError:
            return "conflict"

    r1, r2 = await asyncio.gather(writer("A"), writer("B"))
    assert sorted([r1, r2]) == ["conflict", "ok"]

    # zwycięzca ma v2, przegrany robi GET (pomiń) i STORE(if_match=v2) → OK
    await store_once(key, [{"who":"B","retry":True}], if_match="v2")