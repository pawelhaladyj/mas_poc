# -*- coding: utf-8 -*-
# Presenter – wejście/wyjście człowieka (USER_MSG -> PRESENTER_REPLY)
# Po staremu: SPADE, JSON-ACL, solidne logi, brak wyścigów.
# ZMIANY: stałe conversation_id per sesja Presentera (self.session_id) + lock na sesję.

import os
import json
import time
import asyncio
from typing import Dict, Any, Optional

# --- dotenv (opcjonalnie) ---
try:
    from dotenv import load_dotenv, find_dotenv  # pip install python-dotenv
    _dotenv_path = find_dotenv(filename=".env", usecwd=True)
    if _dotenv_path:
        load_dotenv(_dotenv_path)
    else:
        print("[PRES] Uwaga: nie znaleziono .env (kontynuuję).")
except Exception as e:
    print(f"[PRES] Uwaga: problem z dotenv: {e} (kontynuuję).")

from spade.agent import Agent
from spade.message import Message
from spade.behaviour import CyclicBehaviour, OneShotBehaviour

# Wariant twardszy: wymagamy .env (zostawiam jak miałeś)
from dotenv import load_dotenv, find_dotenv
_dotenv_path = find_dotenv(filename=".env", usecwd=True)
if not _dotenv_path:
    raise RuntimeError("[PRES] Brak pliku .env w drzewie projektu.")
load_dotenv(_dotenv_path)

# ====== ENV ======
def _env(*names: str, default: Optional[str] = None) -> Optional[str]:
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return default

AGENT_JID       = _env("PRESENTER_JID", "AGENT_JID", "XMPP_JID")          # np. presenter@xmpp.pawelhaladyj.pl
AGENT_PASS      = _env("PRESENTER_PASS", "AGENT_PASS", "XMPP_PASS")
COORD_JID       = _env("COORDINATOR_JID", "COORD_JID", default="coordinator@xmpp.pawelhaladyj.pl")
REQ_TIMEOUT_S   = int(_env("PRESENTER_TIMEOUT", default="15") or "15")
CONV_GRACE_SEC  = float(_env("PRESENTER_CONV_GRACE_SEC", default="0.5") or "0.5")
MAX_CONCURRENCY = int(_env("PRESENTER_MAX_CONCURRENCY", default="5") or "5")

# Tryb pracy (jak było)
QUESTION_ONESHOT = _env("PRESENTER_QUESTION", "QUESTION", default=None)
CONV_ID_ONESHOT  = _env("PRESENTER_CONV_ID", "CONV_ID", default=None)

# NOWE: możliwość narzucenia ID sesji z ENV (np. dla Streamlit)
SESSION_ID_ENV   = _env("PRESENTER_SESSION_ID", "PRESENTER_CONV_ID", "CONV_ID", default=None)

# ====== ACL helpers ======
def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def make_acl(
    performative: str, sender: str, receiver: str, content: Dict[str, Any],
    conversation_id: Optional[str] = None, reply_with: Optional[str] = None,
    in_reply_to: Optional[str] = None, protocol: Optional[str] = None
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
        "content": content,
    })

def parse_acl(body: str) -> Dict[str, Any]:
    return json.loads(body)

# ====== AGENT ======
class PresenterAgent(Agent):
    """
    REQ:  REQUEST.USER_MSG -> to Coordinator
    RSP:  INFORM.PRESENTER_REPLY <- from Coordinator
    Jedna SESJA agenta = JEDNO stałe conversation_id (self.session_id).
    """

    def __init__(self, jid: str, password: str, *args, **kwargs):
        super().__init__(jid, password, *args, **kwargs)
        self.conv_queues: Dict[str, asyncio.Queue] = {}
        self.sem = asyncio.Semaphore(MAX_CONCURRENCY)
        self.coordinator_jid = COORD_JID
        # NOWE: lock na sesję, by nie mieszać ramek przy wspólnym conversation_id
        self.session_lock = asyncio.Lock()
        # self.session_id ustawimy w setup(), kiedy środowisko i zegar są gotowe

    # ---------- Behawiory ----------
    class Dispatcher(CyclicBehaviour):
        """Globalny odbiornik: kieruje każdą ramkę do kolejki wg conversation_id."""
        async def run(self):
            msg = await self.receive(timeout=1)
            if not msg:
                return

            try:
                acl = parse_acl(msg.body)
            except Exception as e:
                print(f"[PRES] {now_iso()} Odrzucono nie-JSON od {msg.sender}: {e}")
                return

            pf   = acl.get("performative")
            cont = acl.get("content") or {}
            typ  = (cont.get("type") or "").upper()
            conv = acl.get("conversation_id")

            if not conv:
                print(f"[PRES] {now_iso()} Ignoruję ramkę bez conversation_id pf={pf} typ={typ} od {msg.sender}")
                return

            q = self.agent.conv_queues.get(conv)
            if q:
                await q.put(msg)
            else:
                # Spóźnione ramki po domknięciu rozmowy – ciche pominięcie
                pass

    class ServeConversation(OneShotBehaviour):
        """Jedna interakcja w obrębie tej samej sesji (self.agent.session_id)."""
        def __init__(self, question: str, conv_id: str):
            super().__init__()
            self.question = question
            self.conv_id = conv_id

        async def send_user_msg(self):
            msg = Message(to=self.agent.coordinator_jid)
            msg.set_metadata("conv", self.conv_id)
            msg.set_metadata("performative", "REQUEST")
            msg.body = make_acl(
                "REQUEST", "Presenter", "Coordinator",
                content={
                    "type": "USER_MSG",
                    "args": {"question": self.question},
                    "meta": {"presenter_jid": str(self.agent.jid)}
                },
                conversation_id=self.conv_id,
                reply_with=f"msg-{int(time.time()*1000)}",
                protocol="fipa-request",
            )
            print(f"[PRES] {now_iso()} → COORD {self.agent.coordinator_jid} REQUEST.USER_MSG conv={self.conv_id} q={self.question!r}")
            await self.send(msg)

        async def wait_for_reply(self) -> Optional[str]:
            q: asyncio.Queue = self.agent.conv_queues[self.conv_id]
            deadline = time.time() + REQ_TIMEOUT_S
            while time.time() < deadline:
                remain = max(0, deadline - time.time())
                try:
                    m: Message = await asyncio.wait_for(q.get(), timeout=min(remain, 1.0))
                except asyncio.TimeoutError:
                    continue

                try:
                    acl = parse_acl(m.body)
                except Exception as e:
                    print(f"[PRES] {now_iso()} Odrzucono nie-JSON ({e}) od {m.sender}")
                    continue

                if acl.get("conversation_id") != self.conv_id:
                    continue

                pf = acl.get("performative")
                cont = acl.get("content") or {}
                typ = (cont.get("type") or "").upper()

                if pf == "INFORM" and typ == "PRESENTER_REPLY":
                    text = cont.get("text") or ""
                    print(f"[PRES] {now_iso()} ← COORD INFORM.PRESENTER_REPLY conv={self.conv_id} text={text!r}")
                    return text

                print(f"[PRES] {now_iso()} [CONV {self.conv_id}] niespodziewane pf={pf} typ={typ}")

            print(f"[PRES] {now_iso()} [CONV {self.conv_id}] timeout po {REQ_TIMEOUT_S}s – brak PRESENTER_REPLY")
            return None

        async def run(self):
            # JEDEN dialog na raz w ramach JEDNEJ sesji (jedno conversation_id)
            async with self.agent.session_lock:
                if self.conv_id not in self.agent.conv_queues:
                    self.agent.conv_queues[self.conv_id] = asyncio.Queue()
                print(f"[PRES] {now_iso()} [CONV {self.conv_id}] start")
                try:
                    await self.send_user_msg()
                    text = await self.wait_for_reply()
                    if text is not None:
                        print(f"[PRES] Odpowiedź: {text}")
                    else:
                        print("[PRES] Brak odpowiedzi od Koordynatora w zadanym czasie.")
                finally:
                    if CONV_GRACE_SEC > 0:
                        await asyncio.sleep(CONV_GRACE_SEC)
                    try:
                        q = self.agent.conv_queues.pop(self.conv_id, None)
                        if q:
                            while not q.empty():
                                _ = q.get_nowait()
                    except Exception:
                        pass
                    print(f"[PRES] {now_iso()} [CONV {self.conv_id}] koniec")

    async def setup(self):
        # Ustal stałe ID sesji: z ENV lub jednorazowo z zegara
        self.session_id = SESSION_ID_ENV or f"sess-pres-{int(time.time()*1000)}"
        print(f"[PRES] Start jako {self.jid}. COORD={self.coordinator_jid} TIMEOUT={REQ_TIMEOUT_S}s "
              f"MODE={'ONESHOT' if QUESTION_ONESHOT else 'REPL'} SESSION_ID={self.session_id}")
        self.add_behaviour(self.Dispatcher())

# ====== uruchomienie ======
async def repl_loop(agent: PresenterAgent):
    print("[PRES] Tryb REPL. Zakończ: /quit")
    while agent.is_alive():
        try:
            question = await asyncio.to_thread(input, "Pytanie> ")
        except (EOFError, KeyboardInterrupt):
            break
        question = (question or "").strip()
        if not question:
            continue
        if question in ("/q", "/quit", "/exit"):
            break
        # STAŁE ID SESJI zamiast nowego ID co pytanie
        conv_id = agent.session_id
        agent.add_behaviour(PresenterAgent.ServeConversation(question, conv_id))
        await asyncio.sleep(0.05)

async def oneshot(agent: PresenterAgent, question: str, conv_id: Optional[str]):
    # Jeśli nie podasz CONV_ID_ONESHOT, użyj ID sesji agenta
    conv = conv_id or agent.session_id
    agent.add_behaviour(PresenterAgent.ServeConversation(question, conv))
    while agent.is_alive():
        await asyncio.sleep(0.5)

async def main():
    if not AGENT_JID or not AGENT_PASS:
        raise RuntimeError("Brak PRESENTER_JID/PRESENTER_PASS (lub XMPP_JID/XMPP_PASS) w środowisku.")
    ag = PresenterAgent(AGENT_JID, AGENT_PASS)
    await ag.start(auto_register=False)
    try:
        if QUESTION_ONESHOT:
            await oneshot(ag, QUESTION_ONESHOT, CONV_ID_ONESHOT)
        else:
            await repl_loop(ag)
    except (KeyboardInterrupt, SystemExit):
        pass

if __name__ == "__main__":
    asyncio.run(main())
