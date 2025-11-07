# -*- coding: utf-8 -*-
# Coordinator – USER_MSG -> DF lookup -> REQUEST.ASK_EXPERT -> RESULT -> PRESENTER_REPLY
# Wersja z dyspozytorem i kolejkami per-rozmowa (brak wyścigu o receive), poprawione logi.

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
        print("[COORD] Uwaga: nie znaleziono .env (kontynuuję).")
except Exception as e:
    print(f"[COORD] Uwaga: problem z dotenv: {e} (kontynuuję).")

from spade.agent import Agent
from spade.message import Message
from spade.behaviour import CyclicBehaviour, OneShotBehaviour

# ====== ENV ======
def _env(*names: str, default: Optional[str] = None) -> Optional[str]:
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return default

AGENT_JID        = _env("COORDINATOR_JID", "AGENT_JID", "XMPP_JID")         # np. coordinator@xmpp.pawelhaladyj.pl
AGENT_PASS       = _env("COORDINATOR_PASS", "AGENT_PASS", "XMPP_PASS")
REGISTRY_JID     = _env("REGISTRY_JID", "DF_JID", default="registry@xmpp.pawelhaladyj.pl")
NEED_CAP         = _env("NEED_CAP", default="ASK_EXPERT") or "ASK_EXPERT"
REQ_TIMEOUT_S    = int(_env("COORD_REQ_TIMEOUT", default="10") or "10")      # czekanie na odpowiedź (s)
MAX_RETRIES      = int(_env("COORD_MAX_RETRIES", default="2") or "2")        # próby wysłania do specjalisty
MAX_CONCURRENCY  = int(_env("COORD_MAX_CONCURRENCY", default="5") or "5")    # maks. rozmów równolegle
CONV_GRACE_SEC   = float(_env("COORD_CONV_GRACE_SEC", default="0.5") or "0.5")  # krótka karencja przed sprzątaniem

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
class CoordinatorAgent(Agent):
    def __init__(self, jid: str, password: str, *args, **kwargs):
        super().__init__(jid, password, *args, **kwargs)
        # Kolejki per-rozmowa: conv_id -> asyncio.Queue[Message]
        self.conv_queues: Dict[str, asyncio.Queue] = {}
        # Kontrola współbieżności rozmów
        self.sem = asyncio.Semaphore(MAX_CONCURRENCY)
        self.registry_jid = REGISTRY_JID

    # ---------- Behawiory ----------
    class Dispatcher(CyclicBehaviour):
        """
        Globalny odbiornik: odbiera WSZYSTKIE wiadomości do agenta,
        rozpoznaje conversation_id i wrzuca je do kolejki właściwej rozmowy.
        Gdy dostanie REQUEST.USER_MSG – zakłada nową rozmowę i uruchamia jej obsługę.
        """
        async def run(self):
            msg = await self.receive(timeout=1)
            if not msg:
                return

            # Spróbuj sparsować ACL (JSON).
            try:
                acl = parse_acl(msg.body)
            except Exception as e:
                print(f"[COORD] {now_iso()} Odrzucono nie-JSON od {msg.sender}: {e}")
                return

            pf   = acl.get("performative")
            cont = acl.get("content") or {}
            typ  = (cont.get("type") or "").upper()
            conv = acl.get("conversation_id")

            # Echo finalnej odpowiedzi – ignorujemy, żeby nie śmiecić logów
            if pf == "INFORM" and typ == "PRESENTER_REPLY":
                # Opcjonalnie: print(f"[DISP] {now_iso()} Echo PRESENTER_REPLY – ignoruję")
                return

            # USER_MSG → start rozmowy
            if pf == "REQUEST" and typ == "USER_MSG":
                if not conv:
                    conv = f"sess-{int(time.time()*1000)}"
                    print(f"[COORD] {now_iso()} Brak conversation_id – nadano {conv}")

                if conv not in self.agent.conv_queues:
                    self.agent.conv_queues[conv] = asyncio.Queue()
                    presenter_jid = (cont.get("meta") or {}).get("presenter_jid") or str(msg.sender)
                    question = (cont.get("args") or {}).get("question") or ""
                    print(f"[COORD] {now_iso()} ← USER_MSG od {presenter_jid} conv={conv} q={question!r}")
                    self.agent.add_behaviour(
                        CoordinatorAgent.ServeConversation(presenter_jid, question, conv)
                    )
                # NIE wkładamy USER_MSG do kolejki rozmowy — unikamy „niespodziewane pf=REQUEST”
                return

            # Inne wiadomości – kieruj do kolejki po conversation_id
            if not conv:
                print(f"[COORD] {now_iso()} Ignoruję ramkę bez conversation_id pf={pf} typ={typ} od {msg.sender}")
                return

            q = self.agent.conv_queues.get(conv)
            if q:
                await q.put(msg)
                print(f"[COORD] {now_iso()} Dyspozytor: dostarczono pf={pf} typ={typ} do conv={conv}")
            else:
                # Po zakończeniu rozmowy może jeszcze przyjść spóźniona ramka – nie traktujemy tego jako błąd
                # Opcjonalnie: print(f"[DISP] {now_iso()} Spóźniona ramka conv={conv} pf={pf} typ={typ} – ignoruję")
                pass

    class ServeConversation(OneShotBehaviour):
        """Obsługa jednej rozmowy end-to-end w ramach własnej kolejki."""
        def __init__(self, presenter_jid: str, question: str, conv_id: str):
            super().__init__()
            self.presenter_jid = presenter_jid
            self.question = question
            self.conv_id = conv_id

        # --- Pomocnicze: wysyłka/odbiór *z poziomu behawioru* ---
        async def df_lookup(self) -> list[str]:
            """Zapytaj DF o kandydatów ASK_EXPERT. Zwraca listę JID-ów."""
            reply_id = f"dfq-{int(time.time()*1000)}"
            msg = Message(to=self.agent.registry_jid)
            msg.set_metadata("conv", self.conv_id)
            msg.set_metadata("performative", "QUERY-REF")
            msg.body = make_acl(
                "QUERY-REF", "Coordinator", "Registry",
                content={"need": NEED_CAP},
                conversation_id=self.conv_id,
                reply_with=reply_id,
                protocol="fipa-query",
            )
            print(f"[COORD] {now_iso()} → DF {self.agent.registry_jid} QUERY-REF need={NEED_CAP} conv={self.conv_id}")
            await self.send(msg)

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
                    print(f"[COORD] {now_iso()} [DF] odrzucono nie-JSON ({e}) od {m.sender}")
                    continue

                if acl.get("conversation_id") != self.conv_id:
                    print(f"[COORD] {now_iso()} [DF] inna rozmowa (conv={acl.get('conversation_id')}) – ignoruję")
                    continue

                pf = acl.get("performative")
                if pf == "INFORM":
                    cont = acl.get("content") or {}
                    candidates = cont.get("candidates") or []
                    print(f"[COORD] {now_iso()} ← DF INFORM candidates={candidates} conv={self.conv_id}")
                    return candidates

                print(f"[COORD] {now_iso()} [DF] niespodziewane pf={pf} conv={self.conv_id}")

            print(f"[COORD] {now_iso()} [DF] timeout po {REQ_TIMEOUT_S}s conv={self.conv_id}")
            return []

        async def ask_specialist(self, specialist_jid: str) -> Optional[str]:
            """Wyślij REQUEST.ASK_EXPERT, czekaj na INFORM.RESULT. Zwraca answer albo None."""
            req_id = f"ask-{int(time.time()*1000)}"
            msg = Message(to=specialist_jid)
            msg.set_metadata("conv", self.conv_id)
            msg.set_metadata("performative", "REQUEST")
            msg.body = make_acl(
                "REQUEST", "Coordinator", "Specialist",
                content={"type": "ASK_EXPERT", "args": {"question": self.question}},
                conversation_id=self.conv_id,
                reply_with=req_id,
            )
            print(f"[COORD] {now_iso()} → SPEC {specialist_jid} REQUEST.ASK_EXPERT conv={self.conv_id} q={self.question!r}")
            await self.send(msg)

            q: asyncio.Queue = self.agent.conv_queues[self.conv_id]
            deadline = time.time() + REQ_TIMEOUT_S
            got_agree = False

            while time.time() < deadline:
                remain = max(0, deadline - time.time())
                try:
                    m: Message = await asyncio.wait_for(q.get(), timeout=min(remain, 1.0))
                except asyncio.TimeoutError:
                    continue

                try:
                    acl = parse_acl(m.body)
                except Exception as e:
                    print(f"[COORD] {now_iso()} [SPEC] odrzucono nie-JSON ({e}) od {m.sender}")
                    continue

                if acl.get("conversation_id") != self.conv_id:
                    print(f"[COORD] {now_iso()} [SPEC] inna rozmowa – ignoruję")
                    continue

                pf = acl.get("performative")
                cont = acl.get("content") or {}
                typ = (cont.get("type") or "").upper()

                if pf == "AGREE":
                    if not got_agree:
                        print(f"[COORD] {now_iso()} ← SPEC AGREE conv={self.conv_id}")
                        got_agree = True
                    continue  # czekamy dalej na wynik

                if pf == "INFORM" and typ == "RESULT":
                    ans = (cont.get("result") or {}).get("answer")
                    print(f"[COORD] {now_iso()} ← SPEC INFORM.RESULT conv={self.conv_id} answer={ans!r}")
                    return ans

                print(f"[COORD] {now_iso()} [SPEC] niespodziewane pf={pf} typ={typ} conv={self.conv_id}")

            print(f"[COORD] {now_iso()} [SPEC] timeout po {REQ_TIMEOUT_S}s conv={self.conv_id}")
            return None

        async def reply_to_presenter(self, text: str) -> None:
            msg = Message(to=self.presenter_jid)
            msg.set_metadata("conv", self.conv_id)
            msg.set_metadata("performative", "INFORM")
            msg.body = make_acl(
                "INFORM", "Coordinator", "Presenter",
                content={"type": "PRESENTER_REPLY", "text": text},
                conversation_id=self.conv_id,
            )
            print(f"[COORD] {now_iso()} → PRESENTER {self.presenter_jid} INFORM.PRESENTER_REPLY conv={self.conv_id} text={text!r}")
            await self.send(msg)

        async def run(self):
            async with self.agent.sem:
                print(f"[COORD] {now_iso()} [CONV {self.conv_id}] start")
                try:
                    # 1) DF lookup
                    candidates = await self.df_lookup()
                    if not candidates:
                        await self.reply_to_presenter("Brak dostępnych specjalistów (ASK_EXPERT).")
                        return

                    print(f"[COORD] {now_iso()} [CONV {self.conv_id}] Kandydaci DF: {candidates}")

                    # 2) prosty retry po kandydatach
                    attempts = 0
                    answer: Optional[str] = None
                    for jid in candidates:
                        attempts += 1
                        print(f"[COORD] {now_iso()} [CONV {self.conv_id}] Próba {attempts}/{MAX_RETRIES} → {jid}")
                        answer = await self.ask_specialist(jid)
                        if answer:
                            break
                        if attempts >= MAX_RETRIES:
                            print(f"[COORD] {now_iso()} [CONV {self.conv_id}] Limit prób {MAX_RETRIES} osiągnięty")
                            break

                    # 3) odpowiedź do Presentera
                    if answer:
                        await self.reply_to_presenter(answer)
                    else:
                        await self.reply_to_presenter("Specjalista nie odpowiedział w czasie. Spróbuj ponownie.")
                finally:
                    # Krótka karencja – na wypadek spóźnionych ramek
                    if CONV_GRACE_SEC > 0:
                        await asyncio.sleep(CONV_GRACE_SEC)
                    # Sprzątanie kolejki rozmowy
                    try:
                        q = self.agent.conv_queues.pop(self.conv_id, None)
                        if q:
                            while not q.empty():
                                _ = q.get_nowait()
                    except Exception:
                        pass
                    print(f"[COORD] {now_iso()} [CONV {self.conv_id}] koniec")

    async def setup(self):
        print(f"[COORD] Start jako {self.jid}. DF={self.registry_jid} "
              f"NEED={NEED_CAP} TIMEOUT={REQ_TIMEOUT_S}s RETRIES={MAX_RETRIES} "
              f"CONCURRENCY={MAX_CONCURRENCY}")
        self.add_behaviour(self.Dispatcher())

# ====== MAIN ======
async def main():
    if not AGENT_JID or not AGENT_PASS:
        raise RuntimeError("Brak COORDINATOR_JID/COORDINATOR_PASS (lub XMPP_JID/XMPP_PASS) w środowisku.")
    ag = CoordinatorAgent(AGENT_JID, AGENT_PASS)
    await ag.start(auto_register=False)
    try:
        while ag.is_alive():
            await asyncio.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        pass

if __name__ == "__main__":
    asyncio.run(main())
