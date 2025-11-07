# -*- coding: utf-8 -*-
# Minimalny Directory Facilitator (DF) – SPADE
# Obsługa: REGISTER, HEARTBEAT, DEREGISTER, QUERY-REF (need)
# In-memory; prosto i po staremu.

import os
import json
import time
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
        jid  = profile["jid"]
        caps = profile.get("capabilities", [])
        record = {
            "jid": jid,
            "name": profile.get("name", jid),
            "version": profile.get("version", "0.0.0"),
            "capabilities": list(caps),
            "description": profile.get("description", ""),
            "last_seen": time.time(),
            "status": "online",
        }
        self.catalog[jid] = record
        # odśwież mapowanie cap -> jids
        for c, lst in list(self.cap2jids.items()):
            if jid in lst:
                lst.remove(jid)
            if not lst:
                self.cap2jids.pop(c, None)
        for c in caps:
            self.cap2jids.setdefault(c, [])
            if jid not in self.cap2jids[c]:
                self.cap2jids[c].append(jid)

    def _touch(self, jid: str) -> None:
        p = self.catalog.get(jid)
        if p:
            p["last_seen"] = time.time()
            p["status"] = "online"

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
            delta = now - p["last_seen"]
            if delta > ttl:
                removed.append(jid)
                self._remove(jid, reason="timeout")
            elif delta > HEARTBEAT_SEC * 2:
                p["status"] = "offline"
        if removed:
            print(f"[DF] GC removed (timeout): {removed}")

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
                    self.agent._touch(jid)

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
                alive = []
                ttl_alive = HEARTBEAT_SEC * 2
                for jid in cands:
                    p = self.agent.catalog.get(jid)
                    if p and now - p["last_seen"] <= ttl_alive:
                        alive.append(jid)

                ans = Message(to=str(msg.sender))
                ans.body = make_acl(
                    "INFORM", "Registry", acl.get("sender", "Unknown"),
                    content={"candidates": alive},
                    conversation_id=acl.get("conversation_id"),
                    in_reply_to=acl.get("reply_with"),
                )
                await self.send(ans)

            else:
                # ignorujemy inne rzeczy
                return

    async def setup(self):
        # pamięć DF
        self.catalog: Dict[str, Dict[str, Any]] = {}
        self.cap2jids: Dict[str, List[str]] = {}
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
