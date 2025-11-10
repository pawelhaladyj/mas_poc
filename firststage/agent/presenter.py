# -*- coding: utf-8 -*-
# Presenter – wejście/wyjście człowieka (USER_MSG -> PRESENTER_REPLY)
# Po staremu: SPADE, JSON-ACL, solidne logi, brak wyścigów.
# ZMIANY (2025-11-10):
# - Stałe conversation_id per sesja (self.session_id) + lock na sesję
# - KORELACJA (punkt 8): CorrBook + allow_if_correlated
# - Rejestr oczekiwań na INFORM.PRESENTER_REPLY od Koordynatora (in_reply_to = reply_with)

import os
import json
import time
import asyncio
from typing import Dict, Any, Optional

from spade.agent import Agent
from spade.message import Message
from spade.behaviour import CyclicBehaviour, OneShotBehaviour

# --- dotenv (wymagany plik .env) ---
from dotenv import load_dotenv, find_dotenv  # pip install python-dotenv
_DOTENV_PATH = find_dotenv(filename=".env", usecwd=True)
if not _DOTENV_PATH:
    raise RuntimeError("[PRES] Brak pliku .env w drzewie projektu.")
load_dotenv(_DOTENV_PATH)

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

# Tryb pracy
QUESTION_ONESHOT = _env("PRESENTER_QUESTION", "QUESTION", default=None)
CONV_ID_ONESHOT  = _env("PRESENTER_CONV_ID", "CONV_ID", default=None)

# Możliwość narzucenia ID sesji z ENV (np. frontend)
SESSION_ID_ENV   = _env("PRESENTER_SESSION_ID", "PRESENTER_CONV_ID", "CONV_ID", default=None)

# ====== PROTOKOŁ (wzorcówka) ======
from firststage.protocol.acl_messages import (
    AclMessage, make_acl, new_reply_id, now_iso
)
from firststage.protocol.correlation import CorrBook
from firststage.protocol.guards import allow_if_correlated, bare as bare_jid

def parse_acl(body: str) -> Dict[str, Any]:
    try:
        return AclMessage.loads(body).model_dump()
    except Exception:
        return json.loads(body or "{}")

# ====== AGENT ======
class PresenterAgent(Agent):
    """
    REQ:  REQUEST.USER_MSG -> to Coordinator
    RSP:  INFORM.PRESENTER_REPLY <- from Coordinator
    Jedna SESJA agenta = JEDNO stałe conversation_id (self.session_id).
    Korelacja: odpowiedzi muszą mieć in_reply_to == reply_with z naszej wysyłki
               i pochodzić od bare(COORD_JID).
    """

    def __init__(self, jid: str, password: str, *args, **kwargs):
        super().__init__(jid, password, *args, **kwargs)
        self.conv_queues: Dict[str, asyncio.Queue] = {}
        self.sem = asyncio.Semaphore(MAX_CONCURRENCY)
        self.coordinator_jid = COORD_JID
        self.session_lock = asyncio.Lock()
        self.corr = CorrBook(ttl_sec=REQ_TIMEOUT_S + 2.0)  # księga korelacji
        # self.session_id ustawimy w setup()

    # ---------- Behawiory ----------
    class Dispatcher(CyclicBehaviour):
        """Globalny odbiornik: filtruje i kieruje ramki wg conversation_id z korelacją (punkt 8)."""
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

            # Strażnik korelacji: przyjmujemy tylko to, na co czekamy (od Koordynatora, po in_reply_to)
            from_bare = bare_jid(str(msg.sender))
            if not allow_if_correlated(self.agent.corr, acl, from_bare=from_bare):
                print(f"[PRES] {now_iso()} DROP pf={pf} from={from_bare} conv={conv} irt={acl.get('in_reply_to')}")
                return

            q = self.agent.conv_queues.get(conv)
            if q:
                await q.put(msg)
            else:
                # Spóźnione ramki po domknięciu rozmowy – pomiń po cichu
                pass

    class ServeConversation(OneShotBehaviour):
        """Jedna interakcja w obrębie tej samej sesji (self.agent.session_id)."""
        def __init__(self, question: str, conv_id: str):
            super().__init__()
            self.question = question
            self.conv_id = conv_id

        async def send_user_msg(self) -> str:
            # reply_with do korelacji
            reply_id = new_reply_id("msg")
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
                reply_with=reply_id,
                protocol="fipa-request",
            )

            # Zarejestruj oczekiwanie: INFORM (preferowane) + dopuszczalnie REFUSE/FAILURE/NOT-UNDERSTOOD
            self.agent.corr.register(
                self.conv_id, reply_id,
                allow_from=[bare_jid(self.agent.coordinator_jid)],
                allow_pf=["INFORM", "REFUSE", "FAILURE", "NOT-UNDERSTOOD"],
                note="PRES REQUEST.USER_MSG → oczekuję INFORM.PRESENTER_REPLY"
            )

            print(f"[PRES] {now_iso()} → COORD {self.agent.coordinator_jid} REQUEST.USER_MSG conv={self.conv_id} q={self.question!r}")
            await self.send(msg)
            return reply_id

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

                # Inne dopuszczalne PF – pokaż ślad dla operatora
                if pf in ("REFUSE", "FAILURE", "NOT-UNDERSTOOD"):
                    print(f"[PRES] {now_iso()} [CONV {self.conv_id}] pf={pf} typ={typ} payload={json.dumps(cont, ensure_ascii=False)}")
                    # czekamy dalej do timeoutu, bo niektóre implementacje wyślą jeszcze INFORM

                else:
                    print(f"[PRES] {now_iso()} [CONV {self.conv_id}] niespodziewane pf={pf} typ={typ}")

            print(f"[PRES] {now_iso()} [CONV {self.conv_id}] timeout po {REQ_TIMEOUT_S}s – brak PRESENTER_REPLY. "
                  f"Sugestia: sprawdź czy Koordynator działa i ma poprawny JID ({self.agent.coordinator_jid}).")
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
        conv_id = agent.session_id  # stałe ID sesji
        agent.add_behaviour(PresenterAgent.ServeConversation(question, conv_id))
        await asyncio.sleep(0.05)

async def oneshot(agent: PresenterAgent, question: str, conv_id: Optional[str]):
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
