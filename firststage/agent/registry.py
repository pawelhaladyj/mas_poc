# -*- coding: utf-8 -*-
# Minimalny Directory Facilitator (DF) – SPADE
# Obsługa: REGISTER, HEARTBEAT, DEREGISTER, QUERY-REF (need)
# In-memory; po staremu – ale QUERY-REF zwraca także pełne profile kandydatów.

import os
import json
import time
import copy
import asyncio
from typing import Dict, List, Any

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
    # ^^^ spaDE importy
from spade.behaviour import CyclicBehaviour
from spade.message import Message

# --- ENV ---
def _env(name: str, default: str | None = None) -> str | None:
    return os.getenv(name, default)

AGENT_JID  = _env("AGENT_JID") or _env("XMPP_JID")
AGENT_PASS = _env("AGENT_PASS") or _env("XMPP_PASS")

HEARTBEAT_SEC   = int(_env("DF_HEARTBEAT_SEC", "30"))
TTL_MULTIPLIER  = int(_env("DF_TTL_MULTIPLIER", "3"))
CLEANUP_PERIOD  = int(_env("DF_CLEANUP_PERIOD", "10"))

# --- ACL helpers ---
def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def make_acl(
    performative: str,
    sender: str,
    receiver: str,
    content: Dict[str, Any],
    conversation_id: str | None = None,
    reply_with: str | None = None,
    in_reply_to: str | None = None,
) -> str:
    return json.dumps({
        "performative": performative,
        "sender": sender,
        "receiver": receiver,
        "ontology": "MAS.Core",
        "protocol": "fipa-request" if performative != "QUERY-REF" else "fipa-query",
        "language": "application/json",
        "timestamp": now_iso(),
        "conversation_id": conversation_id,
        "reply_with": reply_with,
        "in_reply_to": in_reply_to,
        "content": content
    })

def parse_acl(body: str) -> Dict[str, Any]:
    return json.loads(body)

class RegistryAgent(Agent):
    # --- struktury i operacje katalogu ---
    def _upsert_profile(self, profile: Dict[str, Any]) -> None:
        """
        Zapis/aktualizacja profilu. Zachowuje wszystko, co przyszło w REGISTER,
        nie tylko podstawowe pola. capabilities scala w listę unikalną.
        """
        jid = profile["jid"]
        incoming_caps = profile.get("capabilities", [])
        # pobierz istniejący lub nowy
        record = self.catalog.get(jid, {"jid": jid})
        # domyślne (jeśli brak)
        record.setdefault("name", jid)
        record.setdefault("version", "0.0.0")
        record.setdefault("description", "")
        record.setdefault("capabilities", [])
        # merge: dowolne pola z REGISTER zostają (poza jid/type)
        for k, v in profile.items():
            if k in ("jid", "type"):
                continue
            if k == "capabilities":
                # unikalna lista
                cur = set(record.get("capabilities", []))
                for c in (incoming_caps or []):
                    cur.add(c)
                record["capabilities"] = list(cur)
            else:
                record[k] = v
        # last_seen/status zawsze odświeżamy
        record["last_seen"] = time.time()
        record["status"] = "online"
        self.catalog[jid] = record

        # odśwież mapę cap -> jids (przebudowa tylko dla zmienionego jid)
        for c, lst in list(self.cap2jids.items()):
            if jid in lst and c not in record.get("capabilities", []):
                lst.remove(jid)
            if not lst:
                self.cap2jids.pop(c, None)
        for c in record.get("capabilities", []):
            self.cap2jids.setdefault(c, [])
            if jid not in self.cap2jids[c]:
                self.cap2jids[c].append(jid)

    def _touch(self, jid: str, extra: Dict[str, Any] | None = None) -> None:
        """
        Aktualizacja znaczników czasu/stanu na bazie HEARTBEAT.
        Dokleja wszelkie przekazane pola (poza type/jid), np. runtime/quality/metrics.
        """
        p = self.catalog.setdefault(jid, {"jid": jid})
        p["last_seen"] = time.time()
        p["status"] = (extra or {}).get("status", "online")
        if extra:
            for k, v in extra.items():
                if k in ("jid", "type"):
                    continue
                p[k] = v  # pozwalamy na zagnieżdżone dict'y (runtime/quality/…)
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
        """
        Zwraca kopię profilu gotową do wysłania. Przekazujemy „to co wiemy”.
        """
        p = self.catalog.get(jid, {"jid": jid})
        return copy.deepcopy(p)

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

            try:
                acl = parse_acl(msg.body)
            except Exception:
                return

            pf = acl.get("performative")
            c  = acl.get("content", {})
            typ = (c.get("type") or "").upper()

            if pf == "REQUEST" and typ == "REGISTER":
                profile = c.get("profile", {})
                if "jid" not in profile:
                    nack = Message(to=str(msg.sender))
                    nack.body = make_acl(
                        "FAILURE", "Registry", "Specialist",
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

            elif pf == "INFORM" and typ == "HEARTBEAT":
                jid = c.get("jid")
                if jid:
                    # przekaż całe content (oprócz jid/type) do profilu
                    self.agent._touch(jid, extra=c)

            elif pf == "REQUEST" and typ == "DEREGISTER":
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

            elif pf == "QUERY-REF":
                need = c.get("need")
                cands = self.agent.cap2jids.get(need, [])
                now = time.time()
                ttl_alive = HEARTBEAT_SEC * 2

                alive: List[str] = []
                profiles: List[Dict[str, Any]] = []
                for jid in cands:
                    p = self.agent.catalog.get(jid)
                    if p and now - p.get("last_seen", 0) <= ttl_alive:
                        alive.append(jid)
                        profiles.append(self.agent._public_profile(jid))

                ans = Message(to=str(msg.sender))
                ans.body = make_acl(
                    "INFORM", "Registry", acl.get("sender", "Unknown"),
                    # ← przekazujemy wszystko co DF wie (per profil),
                    # jednocześnie zostawiając „candidates” dla wstecznej zgodności.
                    content={"candidates": alive, "profiles": profiles, "df_timestamp": now_iso()},
                    conversation_id=acl.get("conversation_id"),
                    in_reply_to=acl.get("reply_with"),
                )
                await self.send(ans)

            else:
                # ignorujemy inne rzeczy
                return

    async def setup(self):
        # pamięć DF
        self.catalog: Dict[str, Dict[str, Any]] = {}   # jid -> pełny profil (statyczne + runtime)
        self.cap2jids: Dict[str, List[str]] = {}       # cap -> list[jid]
        self._last_cleanup = 0.0
        # podpinamy zachowanie
        self.add_behaviour(self.DFBehaviour())

async def main():
    if not AGENT_JID or not AGENT_PASS:
        raise RuntimeError("Brak AGENT_JID/AGENT_PASS (lub XMPP_JID/XMPP_PASS) w środowisku/.env")
    ag = RegistryAgent(AGENT_JID, AGENT_PASS)
    await ag.start(auto_register=False)   # konta zakładamy ejabberdctl
    # blokuj do przerwania (Ctrl+C)
    while ag.is_alive():
        await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(main())
