# -*- coding: utf-8 -*-
"""
Specialist (ASK_EXPERT) – agent usługowy (worker) dla MAS.
Rola: odbiera REQUEST.ASK_EXPERT i odsyła INFORM.RESULT.
Rejestruje się w DF (Directory Facilitator) i wysyła heartbeat.

Opis długi agenta może być dostarczony:
1) przez .env (SPEC_DESC / SPEC_DESC_PATH / SPEC_DESC_B64),
2) przez stałą SPEC_DESC_CODE poniżej (gdy developer nie ma dostępu do .env).

Priorytet ładowania opisu:
SPEC_DESC_PATH (plik) > SPEC_DESC_B64 (base64) > SPEC_DESC (tekst z \n) > SPEC_DESC_CODE (poniżej) > SPEC_DESC_SHORT
"""

import os
import json
import time
import base64
import asyncio
from typing import Dict, Any

# --- dotenv (opcjonalnie) ---
try:
    from dotenv import load_dotenv, find_dotenv  # pip install python-dotenv
    _dotenv_path = find_dotenv(filename=".env", usecwd=True)
    if _dotenv_path:
        load_dotenv(_dotenv_path)
    else:
        print("[SPEC] Uwaga: nie znaleziono .env (kontynuuję).")
except Exception as e:
    print(f"[SPEC] Uwaga: problem z dotenv: {e} (kontynuuję).")

from spade.agent import Agent
from spade.behaviour import CyclicBehaviour, PeriodicBehaviour, OneShotBehaviour
from spade.message import Message

# ====== OPIS W KODZIE (fallback) ======
SPEC_DESC_CODE = """\
Agent specjalistyczny odpowiedzialny za capability ASK_EXPERT.
Wejście: REQUEST { type: "ASK_EXPERT", args: { question: <str> } }
Wyjście: AGREE, następnie INFORM { type: "RESULT", result: { answer: <str>, meta: { capability: "ASK_EXPERT" } } }.
Rejestracja i heartbeat do DF automatycznie.
"""

# ====== helpers ======
def _first_env(*names: str, default: str | None = None) -> str | None:
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return default

def _env_text(name: str) -> str | None:
    v = os.getenv(name)
    if not v:
        return None
    if "\\u" in v or "\\n" in v or "\\t" in v:
        try:
            import codecs
            v = codecs.decode(v, "unicode_escape")
        except Exception:
            v = v.replace("\\n", "\n").replace("\\t", "\t")
    return v

def _load_description(fallback_code_text: str) -> str:
    path = _first_env("SPEC_DESC_PATH", "SPECIALIST_DESC_PATH")
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            print(f"[SPEC] Uwaga: SPEC_DESC_PATH='{path}' nieczytelny: {e}")

    b64 = _first_env("SPEC_DESC_B64", "SPECIALIST_DESC_B64")
    if b64:
        try:
            return base64.b64decode(b64).decode("utf-8")
        except Exception as e:
            print(f"[SPEC] Uwaga: SPEC_DESC_B64 błędne: {e}")

    s = _env_text("SPEC_DESC") or _env_text("SPECIALIST_DESC")
    if s:
        return s

    if fallback_code_text and fallback_code_text.strip():
        return fallback_code_text

    return _first_env("SPEC_DESC_SHORT", default="POC specialist (ASK_EXPERT)") or "POC specialist (ASK_EXPERT)"

def _bare(j: str | None) -> str:
    return (j or "").split("/")[0]

# ====== ENV (identyfikacja i parametry) ======
AGENT_JID    = _first_env("SPECIALIST_JID", "AGENT_JID", "XMPP_JID")  # np. specialist@xmpp.pawelhaladyj.pl
AGENT_PASS   = _first_env("SPECIALIST_PASS", "AGENT_PASS", "XMPP_PASS")
REGISTRY_JID = _first_env("REGISTRY_JID", "DF_JID", default="registry@xmpp.pawelhaladyj.pl")

HEARTBEAT_S  = int(_first_env("SPEC_HEARTBEAT_SEC", "HEARTBEAT_SEC", default="30") or "30")
NAME         = _first_env("SPEC_NAME", default="Specialist") or "Specialist"
VERSION      = _first_env("SPEC_VERSION", default="1.0.0") or "1.0.0"
DESC         = _load_description(SPEC_DESC_CODE)
CAP          = "ASK_EXPERT"

# ====== ACL helpers ======
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
    protocol: str | None = None
) -> str:
    prot = protocol or ("fipa-query" if performative == "QUERY-REF" else "fipa-request")
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
    })

def parse_acl(body: str) -> Dict[str, Any]:
    return json.loads(body or "{}")

# ====== AGENT ======
class SpecialistAgent(Agent):

    class RegisterBehaviour(OneShotBehaviour):
        async def run(self):
            conv_id = f"sess-{int(time.time())}"
            reply_id = f"msg-{int(time.time()*1000)}"
            jid_bare = _bare(str(self.agent.jid))

            # REGISTER
            reg = Message(to=self.agent.registry_jid)
            reg.body = make_acl(
                "REQUEST", jid_bare, "Registry",
                content={
                    "type": "REGISTER",
                    "profile": {
                        "jid": jid_bare,
                        "name": NAME,
                        "version": VERSION,
                        "capabilities": [CAP],
                        "description": DESC,
                    }
                },
                conversation_id=conv_id,
                reply_with=reply_id,
            )
            await self.send(reg)

            # NATYCHMIASTOWY HEARTBEAT (żeby od razu być „alive” w DF)
            hb = Message(to=self.agent.registry_jid)
            hb.body = make_acl(
                "INFORM", jid_bare, "Registry",
                content={"type": "HEARTBEAT", "jid": jid_bare, "status": "ready"}
            )
            await self.send(hb)

            print(f"[SPEC] REGISTER+HB sent to {self.agent.registry_jid} as {jid_bare}")
            print(f"[SPEC] HB={HEARTBEAT_S}s, cap={CAP}, name={NAME}, version={VERSION}")
            print(f"[SPEC] Description (first 120 chars): {DESC[:120].replace(os.linesep, ' ')}{'...' if len(DESC) > 120 else ''}")

    class ServeBehaviour(CyclicBehaviour):
        async def run(self):
            msg = await self.receive(timeout=1)
            if not msg:
                return
            try:
                acl = parse_acl(msg.body)
            except Exception:
                return

            pf  = (acl.get("performative") or "").upper()
            c   = (acl.get("content") or {}) or {}
            typ = (c.get("type") or "").upper()

            if pf == "REQUEST" and typ == "ASK_EXPERT":
                conv   = acl.get("conversation_id")
                req_id = acl.get("reply_with")

                # 1) AGREE
                agree = Message(to=str(msg.sender))
                agree.body = make_acl(
                    "AGREE", NAME, "Coordinator",
                    content={"status": "accepted"},
                    conversation_id=conv,
                    in_reply_to=req_id,
                )
                await self.send(agree)

                # 2) „Praca” – POC
                args = c.get("args", {}) or {}
                question = args.get("question", "")
                result_text = f"[{NAME} v{VERSION}] Odpowiedź na pytanie: {question or '(puste)'}"

                # 3) INFORM.RESULT
                res = Message(to=str(msg.sender))
                res.body = make_acl(
                    "INFORM", NAME, "Coordinator",
                    content={"type": "RESULT",
                             "result": {"answer": result_text, "meta": {"capability": CAP}}},
                    conversation_id=conv,
                    in_reply_to=req_id,
                )
                await self.send(res)

            # Inne komunikaty ignorujemy w tej wersji POC

    class HeartbeatBehaviour(PeriodicBehaviour):
        async def run(self):
            jid_bare = _bare(str(self.agent.jid))
            hb = Message(to=self.agent.registry_jid)
            hb.body = make_acl(
                "INFORM", jid_bare, "Registry",
                content={"type": "HEARTBEAT", "jid": jid_bare, "status": "ready"}
            )
            await self.send(hb)

    async def setup(self):
        self.registry_jid = REGISTRY_JID
        self.add_behaviour(self.RegisterBehaviour())
        self.add_behaviour(self.ServeBehaviour())
        self.add_behaviour(self.HeartbeatBehaviour(period=HEARTBEAT_S))
        print("[SPEC] Specialist started.")

# ====== MAIN ======
async def main():
    if not AGENT_JID or not AGENT_PASS:
        raise RuntimeError(
            "Brak SPECIALIST_JID/SPECIALIST_PASS (lub AGENT_JID/AGENT_PASS, XMPP_JID/XMPP_PASS) w środowisku."
        )
    ag = SpecialistAgent(AGENT_JID, AGENT_PASS)
    await ag.start(auto_register=False)  # konta zakładamy ejabberdctl
    try:
        while ag.is_alive():
            await asyncio.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        pass

if __name__ == "__main__":
    asyncio.run(main())
