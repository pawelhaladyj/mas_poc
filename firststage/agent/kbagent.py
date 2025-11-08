# -*- coding: utf-8 -*-
# KB-Agent — prosty agent wiedzy (append-only) dla MAS
# Po staremu: SPADE, PostgreSQL (psycopg2), JSON-ACL, wyłączna brama przez Koordynatora.

import os
import re
import json
import uuid
import time
import asyncio
from typing import Any, Dict, Optional, Tuple

# --- dotenv (opcjonalnie) ---
try:
    from dotenv import load_dotenv, find_dotenv  # pip install python-dotenv
    _dotenv_path = find_dotenv(filename=".env", usecwd=True)
    if _dotenv_path:
        load_dotenv(_dotenv_path)
    else:
        print("[KB] Uwaga: nie znaleziono .env (kontynuuję).")
except Exception as e:
    print(f"[KB] Uwaga: problem z dotenv: {e} (kontynuuję).")

from spade.agent import Agent
from spade.message import Message
from spade.behaviour import CyclicBehaviour, OneShotBehaviour, PeriodicBehaviour

import psycopg2
from psycopg2.pool import SimpleConnectionPool
from psycopg2.extras import Json as PgJson

# ====== KONFIG z ENV ======
def _getenv(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    return v if v is not None and v != "" else default

KB_JID  = _getenv("KB_JID", "")
KB_PASSWORD = _getenv("KB_PASSWORD", "")
KB_ALLOWED_COORDINATOR_JID = (_getenv("KB_ALLOWED_COORDINATOR_JID", "coordinator@xmpp.pawelhaladyj.pl") or "").split("/")[0].lower()
KB_DB_DSN = _getenv("KB_DB_DSN", "postgresql://mas:mas@localhost:5432/mas_kb")
KB_LOG_LEVEL = (_getenv("KB_LOG_LEVEL", "INFO") or "INFO").upper()

# XMPP bezpieczeństwo / rejestracja
KB_VERIFY_SECURITY = _getenv("KB_VERIFY_SECURITY", "1")  # "1" (domyślnie): ścisły TLS; "0": poluzuj
KB_AUTO_REGISTER   = _getenv("KB_AUTO_REGISTER", "0")    # "1": spróbuj in-band register (jeśli serwer pozwala)

# DF / rejestracja w registry
DF_JID = _getenv("DF_JID", "registry@xmpp.pawelhaladyj.pl")
KB_NAME = _getenv("KB_NAME", "KB-Agent")

_DEFAULT_KB_DESCRIPTION = (
    "Append-only Knowledge Base for MAS. Przechowuje i udostępnia fakty/artefakty konwersacji "
    "(JSON) w celu przekazywania stanu między agentami. Klucz: 5 segmentów [a-z0-9._-] "
    "np. 'session:{id}:domain:entity:key'. Operacje: STORE (z if_match: 'vN' lub ETag) oraz GET "
    "(najnowsza/konkretna wersja lub 'as_of' timestamp). Więź: wyłącznie przez Koordynatora "
    "(whitelist JID). Zastosowania: po ekstrakcji slotów (Extractor) zapisz FACTy; po wynikach "
    "specjalisty zapisz OUTPUT; przed dispatch kolejnego specjalisty odczytaj wymagane dane; "
    "używaj tagów do filtrowania (np. ['facts','offers','nlu']). Spójność: brak mutacji wstecz, "
    "konflikty rozstrzygane if_match. Przewidywany TTL wpisu w DF utrzymywany heartbeatem."
)
KB_DESCRIPTION = _getenv("KB_DESCRIPTION", _DEFAULT_KB_DESCRIPTION)

_cap_raw = _getenv("KB_CAPABILITIES", "KB.STORE,KB.GET") or ""
KB_CAPABILITIES = [s.strip() for s in _cap_raw.split(",") if s.strip()]
KB_HEARTBEAT_SEC = int(_getenv("KB_HEARTBEAT_SEC", "30"))

# ====== stałe/regex ======
KEY_RE = re.compile(r"^[a-z0-9._-]+:[a-z0-9._-]+:[a-z0-9._-]+:[a-z0-9._-]+:[a-z0-9._-]+$")

def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def bare(jid: Optional[str]) -> Optional[str]:
    if not jid:
        return None
    return str(jid).split("/")[0]

def _mask_dsn(dsn: str) -> str:
    # maskuj hasło w DSN-ie
    try:
        # postgresql://user:pass@host:port/db -> zamień pass na ***
        prefix, rest = dsn.split("://", 1)
        creds, tail = rest.split("@", 1)
        if ":" in creds:
            user, _pwd = creds.split(":", 1)
            creds = f"{user}:***"
        return f"{prefix}://{creds}@{tail}"
    except Exception:
        return dsn

def _acl_df_base(**extra) -> Dict[str, Any]:
    base = {
        "ontology": "MAS.DF",
        "protocol": "fipa-request",
        "language": "application/json",
        "timestamp": now_iso(),
        "conversation_id": f"df-{uuid.uuid4().hex}",
        "sender": "KB",
        "receiver": "DF",
    }
    base.update(extra)
    return base

# ====== DB WARSTWA ======
class KBStorage:
    def __init__(self, dsn: str):
        # Możesz dodać connect_timeout w DSN, jeśli chcesz (np. ...?connect_timeout=5)
        self.pool = SimpleConnectionPool(1, 8, dsn)
        self._ensure_schema()
        # szybki healthcheck
        conn = self.pool.getconn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1;")
        finally:
            self.pool.putconn(conn)

    def _ensure_schema(self) -> None:
        sql = """
        CREATE TABLE IF NOT EXISTS kb_items (
          id           bigserial PRIMARY KEY,
          key          text        NOT NULL,
          version      integer     NOT NULL,
          etag         uuid        NOT NULL,
          content_type text        NOT NULL,
          value        jsonb       NOT NULL,
          tags         text[]      NOT NULL DEFAULT '{}',
          session_id   text,
          created_at   timestamptz NOT NULL DEFAULT now(),
          created_by   text        NOT NULL,
          deleted      boolean     NOT NULL DEFAULT false
        );
        CREATE UNIQUE INDEX IF NOT EXISTS kb_items_key_version_uq ON kb_items(key, version);
        CREATE INDEX IF NOT EXISTS kb_items_key_desc_idx ON kb_items(key, version DESC);
        CREATE INDEX IF NOT EXISTS kb_items_session_idx ON kb_items(session_id);
        CREATE INDEX IF NOT EXISTS kb_items_tags_gin ON kb_items USING GIN (tags);
        """
        conn = self.pool.getconn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(sql)
        finally:
            self.pool.putconn(conn)

    def _next_version(self, conn, key: str) -> int:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(version),0)+1 FROM kb_items WHERE key=%s;", (key,))
            (v,) = cur.fetchone()
            return int(v)

    def store(
        self,
        key: str,
        content_type: str,
        value: Any,
        tags: Optional[list],
        session_id: Optional[str],
        created_by: str,
        if_match: Optional[str],
    ) -> Tuple[int, str, str]:
        """Append-only. Zwraca (version, etag, stored_at). Rzuca ConflictError przy if_match."""
        conn = self.pool.getconn()
        try:
            with conn:
                with conn.cursor() as cur:
                    # if_match: "v3" albo ETag
                    if if_match:
                        if if_match.startswith("v") and if_match[1:].isdigit():
                            expected_v = int(if_match[1:])
                            cur.execute("SELECT COALESCE(MAX(version),0) FROM kb_items WHERE key=%s;", (key,))
                            (cur_v,) = cur.fetchone()
                            cur_v = int(cur_v)
                            if cur_v != expected_v:
                                raise ConflictError(f"Version mismatch: current v{cur_v}, expected v{expected_v}")
                        else:
                            cur.execute(
                                "SELECT 1 FROM kb_items WHERE key=%s AND etag::text=%s ORDER BY version DESC LIMIT 1;",
                                (key, if_match),
                            )
                            if cur.fetchone() is None:
                                raise ConflictError("ETag mismatch")

                    version = self._next_version(conn, key)
                    etag = str(uuid.uuid4())
                    tags_arr = tags or []
                    cur.execute(
                        """
                        INSERT INTO kb_items (key, version, etag, content_type, value, tags, session_id, created_by)
                        VALUES (%s, %s, %s::uuid, %s, %s, %s, %s, %s)
                        RETURNING created_at;
                        """,
                        (key, version, etag, content_type, PgJson(value), tags_arr, session_id, created_by),
                    )
                    (stored_at,) = cur.fetchone()
                    return version, etag, stored_at.isoformat()
        finally:
            self.pool.putconn(conn)

    def get(
        self,
        key: str,
        prefer: Optional[str] = None,
        version: Optional[int] = None,
        as_of: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], int, str, str]:
        """Zwraca (value_json, version, etag, stored_at). Rzuca NotFoundError."""
        conn = self.pool.getconn()
        try:
            with conn.cursor() as cur:
                if version is not None:
                    cur.execute(
                        "SELECT content_type, value, version, etag::text, created_at FROM kb_items WHERE key=%s AND version=%s AND deleted=false;",
                        (key, version),
                    )
                elif as_of is not None:
                    cur.execute(
                        """
                        SELECT content_type, value, version, etag::text, created_at
                        FROM kb_items
                        WHERE key=%s AND created_at <= %s AND deleted=false
                        ORDER BY version DESC LIMIT 1;
                        """,
                        (key, as_of),
                    )
                else:
                    cur.execute(
                        """
                        SELECT content_type, value, version, etag::text, created_at
                        FROM kb_items
                        WHERE key=%s AND deleted=false
                        ORDER BY version DESC LIMIT 1;
                        """,
                        (key,),
                    )
                row = cur.fetchone()
                if not row:
                    raise NotFoundError("No value for key")
                content_type, value, ver, etag, created_at = row
                return value, int(ver), etag, created_at.isoformat()
        finally:
            self.pool.putconn(conn)

class NotFoundError(Exception):
    pass

class ConflictError(Exception):
    pass

# ====== AGENT ======
class KBCycle(CyclicBehaviour):
    async def run(self):
        msg = await self.receive(timeout=1)
        if not msg:
            return

        try:
            payload = json.loads(msg.body or "{}")
        except Exception:
            await self._reply_failure(msg, None, code="FAILURE.INVALID_JSON", reason="Body is not valid JSON")
            return

        conv = payload.get("conversation_id") or payload.get("conversationId")
        mtype = payload.get("type")
        sender_bare = (bare(getattr(msg, "sender", None)) or "").lower()

        # Whitelist — tylko Koordynator
        if sender_bare != self.agent.allowed_bare:
            await self._reply_refuse(msg, conv, code="REFUSE.UNAUTHORIZED", reason=f"Only {self.agent.allowed_bare}")
            return

        if mtype == "STORE":
            await self._handle_store(msg, payload, conv)
        elif mtype == "GET":
            await self._handle_get(msg, payload, conv)
        else:
            await self._reply_refuse(msg, conv, code="REFUSE.UNSUPPORTED_TYPE", reason=str(mtype))

    async def _handle_store(self, msg: Message, p: Dict[str, Any], conv: Optional[str]):
        key = p.get("key", "")
        content_type = p.get("content_type", "application/json")
        value = p.get("value")
        tags = p.get("tags") or []
        if_match = p.get("if_match")

        if not KEY_RE.match(key):
            await self._reply_failure(msg, conv, code="FAILURE.INVALID_KEY",
                                      reason="Key must have 5 segments and allowed chars [a-z0-9._-]")
            return

        session_id = None
        parts = key.split(":", 4)
        if len(parts) >= 2 and parts[0] == "session":
            session_id = parts[1]

        try:
            version, etag, stored_at = await asyncio.to_thread(
                self.agent.storage.store,
                key, content_type, value, tags, session_id,
                created_by=self.agent.allowed_bare,
                if_match=if_match,
            )
            await self._reply_inform(msg, conv, {
                "type": "STORED",
                "key": key,
                "version": version,
                "etag": etag,
                "stored_at": stored_at,
            })
            if self.agent.log_info:
                print(f"[KB] {now_iso()} STORED key={key} v={version} etag={etag} conv={conv}")
        except ConflictError as e:
            await self._reply_failure(msg, conv, code="FAILURE.CONFLICT", reason=str(e))
        except Exception as e:
            await self._reply_failure(msg, conv, code="FAILURE.EXCEPTION", reason=str(e))

    async def _handle_get(self, msg: Message, p: Dict[str, Any], conv: Optional[str]):
        key = p.get("key", "")
        prefer = p.get("prefer")
        version = p.get("version")
        as_of = p.get("as_of")

        if not KEY_RE.match(key):
            await self._reply_failure(msg, conv, code="FAILURE.INVALID_KEY",
                                      reason="Key must have 5 segments and allowed chars [a-z0-9._-]")
            return

        try:
            vnum = None
            if isinstance(version, int):
                vnum = version
            elif isinstance(version, str) and version.isdigit():
                vnum = int(version)

            value, ver, etag, stored_at = await asyncio.to_thread(
                self.agent.storage.get, key, prefer, vnum, as_of
            )
            await self._reply_inform(msg, conv, {
                "type": "VALUE",
                "key": key,
                "version": ver,
                "etag": etag,
                "content_type": "application/json",
                "value": value,
                "stored_at": stored_at,
            })
            if self.agent.log_info:
                print(f"[KB] {now_iso()} VALUE key={key} v={ver} conv={conv}")
        except NotFoundError as e:
            await self._reply_failure(msg, conv, code="FAILURE.NOT_FOUND", reason=str(e))
        except Exception as e:
            await self._reply_failure(msg, conv, code="FAILURE.EXCEPTION", reason=str(e))

    async def _reply_inform(self, msg: Message, conv: Optional[str], content: Dict[str, Any]):
        body = {
            "performative": "INFORM",
            "sender": "KB",
            "receiver": "Coordinator",
            "conversation_id": conv,
            "ontology": "MAS.KB",
            "protocol": "fipa-request",
            "language": "application/json",
            "timestamp": now_iso(),
        }
        body.update(content)
        reply = Message(to=bare(getattr(msg, "sender", None)))
        reply.body = json.dumps(body, ensure_ascii=False)
        await self.send(reply)

    async def _reply_refuse(self, msg: Message, conv: Optional[str], code: str, reason: str):
        body = {
            "performative": "REFUSE",
            "sender": "KB",
            "receiver": "Coordinator",
            "conversation_id": conv,
            "ontology": "MAS.KB",
            "protocol": "fipa-request",
            "language": "application/json",
            "timestamp": now_iso(),
            "type": code,
            "reason": reason,
        }
        reply = Message(to=bare(getattr(msg, "sender", None)))
        reply.body = json.dumps(body, ensure_ascii=False)
        await self.send(reply)
        if self.agent.log_info:
            print(f"[KB] {now_iso()} {code} conv={conv} from={bare(getattr(msg, 'sender', None))} reason={reason}")

    async def _reply_failure(self, msg: Message, conv: Optional[str], code: str, reason: str):
        body = {
            "performative": "FAILURE",
            "sender": "KB",
            "receiver": "Coordinator",
            "conversation_id": conv,
            "ontology": "MAS.KB",
            "protocol": "fipa-request",
            "language": "application/json",
            "timestamp": now_iso(),
            "type": code,
            "reason": reason,
        }
        reply = Message(to=bare(getattr(msg, "sender", None)))
        reply.body = json.dumps(body, ensure_ascii=False)
        await self.send(reply)
        if self.agent.log_info:
            print(f"[KB] {now_iso()} {code} conv={conv} from={bare(getattr(msg, 'sender', None))} reason={reason}")

# ====== DF REGISTER + HEARTBEAT ======
class KBRegisterOnce(OneShotBehaviour):
    async def run(self):
        body = _acl_df_base(
            performative="REQUEST",
            type="REGISTER",
            agent={
                "jid": str(self.agent.jid),
                "name": KB_NAME,
                "description": KB_DESCRIPTION,
                "capabilities": KB_CAPABILITIES,
                "status": "ready",
                "ttl_sec": KB_HEARTBEAT_SEC * 3,
            },
        )
        msg = Message(to=DF_JID)
        msg.body = json.dumps(body, ensure_ascii=False)
        await self.send(msg)
        if getattr(self.agent, "log_info", True):
            print(f"[KB] {now_iso()} REGISTER sent to {DF_JID} name={KB_NAME} caps={KB_CAPABILITIES}")

class KBHeartbeat(PeriodicBehaviour):
    async def run(self):
        body = _acl_df_base(
            performative="INFORM",
            type="HEARTBEAT",
            agent={"jid": str(self.agent.jid), "status": "ready"}
        )
        msg = Message(to=DF_JID)
        msg.body = json.dumps(body, ensure_ascii=False)
        await self.send(msg)

class KBAgent(Agent):
    def __init__(self, jid: str, password: str, storage: KBStorage, allowed_bare: str, verify_security: bool):
        super().__init__(jid, password, verify_security=verify_security)
        self.storage = storage
        self.allowed_bare = (allowed_bare or "").lower()
        self.log_info = (KB_LOG_LEVEL == "INFO")

    async def setup(self):
        print(f"[KB] Start jako {self.jid}. Allowed={self.allowed_bare} DB={_mask_dsn(KB_DB_DSN)}")
        self.add_behaviour(KBCycle())
        # Rejestracja w DF + Heartbeat
        self.add_behaviour(KBRegisterOnce())
        self.add_behaviour(KBHeartbeat(period=KB_HEARTBEAT_SEC))

# ====== MAIN ======
async def amain():
    if not KB_JID or not KB_PASSWORD:
        raise SystemExit("Brak KB_JID/KB_PASSWORD w środowisku")

    # DB storage
    storage = KBStorage(KB_DB_DSN)

    # verify_security: "1" → True (domyślnie); "0" → False (poluzuj TLS, żeby ruszyć na self-signed)
    verify_flag = (str(KB_VERIFY_SECURITY).strip() != "0")

    agent = KBAgent(
        KB_JID, KB_PASSWORD, storage, KB_ALLOWED_COORDINATOR_JID, verify_security=verify_flag
    )

    auto_reg = (str(KB_AUTO_REGISTER).strip() == "1")
    await agent.start(auto_register=auto_reg)
    print("[KB] Agent wystartowany. CTRL+C aby zakończyć.")

    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        print("[KB] Kończę...")
    finally:
        await agent.stop()

if __name__ == "__main__":
    asyncio.run(amain())
