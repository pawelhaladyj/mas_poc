# -*- coding: utf-8 -*-
# Minimalny Directory Facilitator (DF) – SPADE
# Obsługa: REGISTER, HEARTBEAT, DEREGISTER, QUERY-REF (LIST / DUMP / need / ALL)
# In-memory; po staremu – QUERY-REF potrafi zwrócić pełne profile.

import os
import json
import time
import copy
import asyncio
from typing import Dict, List, Any, Optional

# --- dotenv (opcjonalnie) ---
try:
    from dotenv import load_dotenv, find_dotenv  # pip install python-dotenv
    _dotenv_path = find_dotenv(filename=".env", usecwd=True)
    if _dotenv_path:
        load_dotenv(_dotenv_path)
    else:
        print("[DF] Uwaga: nie znaleziono .env (kontynuuję).")
except Exception as e:
    print(f"[DF] Uwaga: problem z dotenv: {e} (kontynuuję).")

from spade.agent import Agent
    # ^ SPADE
from spade.behaviour import CyclicBehaviour
from spade.message import Message

# --- ENV ---
def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    return v if v is not None and v != "" else default

AGENT_JID  = _env("AGENT_JID") or _env("XMPP_JID")
AGENT_PASS = _env("AGENT_PASS") or _env("XMPP_PASS")

HEARTBEAT_SEC   = int(_env("DF_HEARTBEAT_SEC", "30"))
TTL_MULTIPLIER  = int(_env("DF_TTL_MULTIPLIER", "3"))
CLEANUP_PERIOD  = int(_env("DF_CLEANUP_PERIOD", "10"))
DF_DEBUG        = (_env("DF_DEBUG", "0") == "1")  # proste logi diagnostyczne

# --- ACL helpers ---
def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def make_acl(
    performative: str,
    sender: str,
    receiver: str,
    content: Dict[str, Any],
    conversation_id: Optional[str] = None,
    reply_with: Optional[str] = None,
    in_reply_to: Optional[str] = None,
) -> str:
    prot = "fipa-query" if performative == "QUERY-REF" else "fipa-request"
    return json.dumps({
        "performative": performative,
        "sender": sender,
        "receiver": receiver,
        "ontology": "MAS.Core",
        "protocol": prot,
        "language": "application/json",
        "timestamp": now_iso(),
        "conversation_id": conversation_id,
        "reply_with": reply_with,
        "in_reply_to": in_reply_to,
        "content": content
    }, ensure_ascii=False)

def parse_acl(body: str) -> Dict[str, Any]:
    try:
        return json.loads(body or "{}")
    except Exception:
        return {}

# --- DF Agent ---
class RegistryAgent(Agent):
    # --- struktury i operacje katalogu ---
    def _upsert_profile(self, profile: Dict[str, Any]) -> None:
        """
        Zapis/aktualizacja profilu na REGISTER.
        Zachowuje wszystko, co przyszło w REGISTER; capabilities scala do unikalnej listy.
        """
        jid = profile["jid"]
        incoming_caps = profile.get("capabilities", []) or []
        record = self.catalog.get(jid, {"jid": jid})
        # domyślne (jeśli brak)
        record.setdefault("name", jid)
        record.setdefault("version", "0.0.0")
        record.setdefault("description", "")
        record.setdefault("capabilities", [])
        # merge pól (poza jid/type)
        for k, v in profile.items():
            if k in ("jid", "type"):
                continue
            if k == "capabilities":
                cur = set(record.get("capabilities", []))
                for c in incoming_caps:
                    cur.add(c)
                record["capabilities"] = sorted(cur)
            else:
                record[k] = v
        # last_seen/status zawsze odświeżamy
        record["last_seen"] = time.time()
        record["status"] = "online"
        self.catalog[jid] = record

        # cap -> jids (aktualizacja mapy; bez zmiany istniejących kluczy)
        # Najpierw usuń niepasujące wpisy
        for c, lst in list(self.cap2jids.items()):
            if jid in lst and c not in record.get("capabilities", []):
                lst.remove(jid)
            if not lst:
                self.cap2jids.pop(c, None)
        # Potem dodaj aktualne capabilities
        for c in record.get("capabilities", []):
            self.cap2jids.setdefault(c, [])
            if jid not in self.cap2jids[c]:
                self.cap2jids[c].append(jid)

    def _touch(self, jid: str, extra: Optional[Dict[str, Any]] = None) -> None:
        """
        Aktualizacja znaczników czasu/stanu na bazie HEARTBEAT.
        Dokleja wszystkie pola z extra (poza type/jid), np. runtime/quality/metrics.
        """
        p = self.catalog.setdefault(jid, {"jid": jid})
        p["last_seen"] = time.time()
        p["status"] = (extra or {}).get("status", "online")
        if extra:
            for k, v in extra.items():
                if k in ("jid", "type"):
                    continue
                p[k] = v
        self.catalog[jid] = p

    def _remove(self, jid: str, reason: str = "deregister") -> None:
        if jid in self.catalog:
            for c in list(self.cap2jids.keys()):
                if jid in self.cap2jids[c]:
                    self.cap2jids[c].remove(jid)
                    if not self.cap2jids[c]:
                        self.cap2jids.pop(c, None)
            self.catalog.pop(jid, None)

    def _gc(self) -> None:
        now = time.time()
        ttl = HEARTBEAT_SEC * TTL_MULTIPLIER
        removed = []
        for jid, p in list(self.catalog.items()):
            delta = now - p.get("last_seen", 0)
            if delta > ttl:
                removed.append(jid)
                self._remove(jid, reason="timeout")
            elif delta > HEARTBEAT_SEC * 2:
                p["status"] = "offline"
        if removed:
            print(f"[DF] GC removed (timeout): {removed}")

    def _public_profile(self, jid: str) -> Dict[str, Any]:
        """Zwraca kopię profilu gotową do wysłania."""
        return copy.deepcopy(self.catalog.get(jid, {"jid": jid}))

    class DFBehaviour(CyclicBehaviour):
        async def on_start(self):
            self.agent._last_cleanup = 0.0
            print(f"[DF] Registry started as {self.agent.jid}. HB={HEARTBEAT_SEC}s TTLx{TTL_MULTIPLIER} GC={CLEANUP_PERIOD}s")

        async def run(self):
            # sprzątanie okresowe
            if time.time() - self.agent._last_cleanup > CLEANUP_PERIOD:
                self.agent._gc()
                self.agent._last_cleanup = time.time()

            msg = await self.receive(timeout=1)
            if not msg:
                return

            acl = parse_acl(msg.body)
            if not acl:
                return

            pf = (acl.get("performative") or "").upper()
            c  = (acl.get("content") or {}) or {}
            typ = (c.get("type") or "").upper()

            # --- REGISTER ---
            if pf == "REQUEST" and typ == "REGISTER":
                profile = c.get("profile", {}) or {}
                if "jid" not in profile:
                    nack = Message(to=str(msg.sender))
                    nack.body = make_acl(
                        "FAILURE", "Registry", "Unknown",
                        content={"reason": "INVALID_PROFILE"},
                        conversation_id=acl.get("conversation_id"),
                        in_reply_to=acl.get("reply_with"),
                    )
                    await self.send(nack)
                    return
                self.agent._upsert_profile(profile)
                print(f"[DF] REGISTER: {profile['jid']} caps={profile.get('capabilities', [])}")
                ack = Message(to=str(msg.sender))
                ack.body = make_acl(
                    "AGREE", "Registry", acl.get("sender", "Unknown"),
                    content={"status": "registered"},
                    conversation_id=acl.get("conversation_id"),
                    in_reply_to=acl.get("reply_with"),
                )
                await self.send(ack)
                return

            # --- HEARTBEAT ---
            if pf == "INFORM" and typ == "HEARTBEAT":
                jid = c.get("jid")
                if jid:
                    self.agent._touch(jid, extra=c)
                    print(f"[DF] HEARTBEAT: {jid} status={c.get('status','')} at={now_iso()}")
                return

            # --- DEREGISTER ---
            if pf == "REQUEST" and typ == "DEREGISTER":
                jid = c.get("jid")
                if jid:
                    self.agent._remove(jid, reason="deregister")
                    print(f"[DF] DEREGISTER: {jid}")
                    ack = Message(to=str(msg.sender))
                    ack.body = make_acl(
                        "AGREE", "Registry", acl.get("sender", "Unknown"),
                        content={"status": "deregistered"},
                        conversation_id=acl.get("conversation_id"),
                        in_reply_to=acl.get("reply_with"),
                    )
                    await self.send(ack)
                return

            # --- QUERY-REF: LIST / DUMP / need|ALL ---
            if pf == "QUERY-REF":
                qtype = (c.get("type") or "").upper()
                need_raw = c.get("need")
                need = (str(need_raw).strip().upper() if isinstance(need_raw, (str, bytes)) else None)

                now_ts = time.time()
                ttl_alive = HEARTBEAT_SEC * 2

                def _alive_profiles() -> List[Dict[str, Any]]:
                    out: List[Dict[str, Any]] = []
                    for jid, p in self.agent.catalog.items():
                        if now_ts - p.get("last_seen", 0) <= ttl_alive:
                            out.append(self.agent._public_profile(jid))
                    return out

                # LIST – wszyscy "żywi"
                if qtype == "LIST":
                    profiles = _alive_profiles()
                    if DF_DEBUG:
                        print(f"[DF] QUERY LIST → {len(profiles)} live profiles")
                    ans = Message(to=str(msg.sender))
                    ans.body = make_acl(
                        "INFORM", "Registry", acl.get("sender", "Unknown"),
                        content={
                            "candidates": [p.get("jid") for p in profiles],
                            "profiles": profiles,
                            "df_timestamp": now_iso(),
                        },
                        conversation_id=acl.get("conversation_id"),
                        in_reply_to=acl.get("reply_with"),
                    )
                    await self.send(ans)
                    return

                # DUMP – pełny katalog (także offline)
                if qtype == "DUMP":
                    profiles = [self.agent._public_profile(j) for j in self.agent.catalog.keys()]
                    if DF_DEBUG:
                        print(f"[DF] QUERY DUMP → {len(profiles)} total profiles")
                    ans = Message(to=str(msg.sender))
                    ans.body = make_acl(
                        "INFORM", "Registry", acl.get("sender", "Unknown"),
                        content={
                            "candidates": list(self.agent.catalog.keys()),
                            "profiles": profiles,
                            "df_timestamp": now_iso(),
                        },
                        conversation_id=acl.get("conversation_id"),
                        in_reply_to=acl.get("reply_with"),
                    )
                    await self.send(ans)
                    return

                # need – klasyczny tryb: capability -> kandydaci żywi
                # Rozszerzone: need in ("", "ALL", "*") → wszyscy żywi (pełne profile)
                if need in ("", "ALL", "*", None):
                    profiles = _alive_profiles()
                    if DF_DEBUG:
                        print(f"[DF] QUERY need={need_raw!r} (ALL/*) → {len(profiles)} live profiles")
                    ans = Message(to=str(msg.sender))
                    ans.body = make_acl(
                        "INFORM", "Registry", acl.get("sender", "Unknown"),
                        content={
                            "candidates": [p.get("jid") for p in profiles],
                            "profiles": profiles,
                            "df_timestamp": now_iso(),
                        },
                        conversation_id=acl.get("conversation_id"),
                        in_reply_to=acl.get("reply_with"),
                    )
                    await self.send(ans)
                    return
                else:
                    # dopasowanie capability case-insensitive
                    jids: List[str] = []
                    for cap_key, lst in self.agent.cap2jids.items():
                        if (cap_key or "").upper() == need:
                            jids.extend(lst)

                    profiles: List[Dict[str, Any]] = []
                    alive: List[str] = []
                    for jid in jids:
                        p = self.agent.catalog.get(jid)
                        if p and now_ts - p.get("last_seen", 0) <= ttl_alive:
                            alive.append(jid)
                            profiles.append(self.agent._public_profile(jid))

                    if DF_DEBUG:
                        print(f"[DF] QUERY need={need_raw!r} → match={len(alive)} (live)")

                    ans = Message(to=str(msg.sender))
                    ans.body = make_acl(
                        "INFORM", "Registry", acl.get("sender", "Unknown"),
                        content={"candidates": alive, "profiles": profiles, "df_timestamp": now_iso()},
                        conversation_id=acl.get("conversation_id"),
                        in_reply_to=acl.get("reply_with"),
                    )
                    await self.send(ans)
                    return

            # Inne rzeczy ignorujemy
            return

    async def setup(self):
        # pamięć DF
        self.catalog: Dict[str, Dict[str, Any]] = {}   # jid -> pełny profil (statyczne + runtime)
        self.cap2jids: Dict[str, List[str]] = {}       # cap -> list[jid]
        self._last_cleanup = 0.0
        # podpinamy zachowanie
        self.add_behaviour(self.DFBehaviour())

# --- uruchomienie ---
async def main():
    if not AGENT_JID or not AGENT_PASS:
        raise RuntimeError("Brak AGENT_JID/AGENT_PASS (lub XMPP_JID/XMPP_PASS) w środowisku/.env")
    ag = RegistryAgent(AGENT_JID, AGENT_PASS)
    await ag.start(auto_register=False)   # konta zakładamy ejabberdctl
    while ag.is_alive():
        await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(main())
