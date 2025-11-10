# specialist_blueprint.py
# -*- coding: utf-8 -*-
# -----------------------------------------------------------------------------
# Specialist-AI Agent (BLUEPRINT)
#
# Cel:
#  - gotowy szkielet agenta SPADE zgodny z Twoim MAS,
#  - rejestracja i heartbeat do DF,
#  - obsługa REQUEST.ASK_EXPERT -> INFORM.RESULT,
#  - proste HOOKI (BaseLogic), żeby dopisać własną logikę bez grzebania
#    w szkielecie. Klasa z logiką ładowana jest z ENV: SPEC_LOGIC,
#    np. "logic_example:MyLogic".
#
# Zasada działania:
#  - jeśli hook ask_expert() zwróci tekst, to on idzie do Koordynatora;
#  - jeśli hook zwróci None lub pusty tekst, wchodzi warstwa AI
#    (OpenAI przez AIConnector albo lokalny model przez komendę).
#
# Uruchomienie (po staremu):
#  1) przygotuj .env (patrz fragment pod kodem),
#  2) python specialist_blueprint.py
# -----------------------------------------------------------------------------

import os
import json
import time
import uuid
import asyncio
import shlex
import subprocess
import importlib
from typing import Any, Dict, List, Optional, Tuple

# --- dotenv (opcjonalnie) ----------------------------------------------------
try:
    from dotenv import load_dotenv, find_dotenv
    _dotenv_path = find_dotenv(filename=".env", usecwd=True)
    if _dotenv_path:
        load_dotenv(_dotenv_path)
    else:
        print("[SPEC] Uwaga: nie znaleziono .env (kontynuuję).")
except Exception as e:
    print(f"[SPEC] Uwaga: problem z dotenv: {e} (kontynuuję).")

# --- SPADE -------------------------------------------------------------------
from spade.agent import Agent
from spade.message import Message
from spade.behaviour import CyclicBehaviour, OneShotBehaviour, PeriodicBehaviour

# --- AIConnector (OpenAI) ----------------------------------------------------
from utils.aiconnector import AIConnector  # Twój sprawdzony konektor

# ====== ENV helpers ==========================================================
def _env(*names: str, default: Optional[str] = None) -> Optional[str]:
    for n in names:
        v = os.getenv(n)
        if v not in (None, ""):
            return v
    return default

# Tożsamość XMPP
SPEC_JID        = _env("SPECIALIST_JID", "AGENT_JID", "XMPP_JID")
SPEC_PASS       = _env("SPECIALIST_PASS", "AGENT_PASS", "XMPP_PASS")
VERIFY_SECURITY = (_env("SPEC_VERIFY_SECURITY", default="1") or "1") != "0"
AUTO_REGISTER   = (_env("SPEC_AUTO_REGISTER", default="0") or "0") == "1"

# Registry (DF)
DF_JID          = _env("DF_JID", "REGISTRY_JID",
                       default="registry@xmpp.pawelhaladyj.pl")

# Profil widoczny w DF i dla Koordynatora
SPEC_NAME        = _env("SPEC_NAME", default="Specialist")
SPEC_VERSION     = _env("SPEC_VERSION", default="1.0.0")
SPEC_STATUS      = _env("SPEC_STATUS", default="ready")
SPEC_DESCRIPTION = _env(
    "SPEC_DESCRIPTION",
    default=("Specjalista AI. Odpowiada z użyciem hooków albo AI."),
)
SPEC_CAPABILITIES = [
    s.strip() for s in
    (_env("SPEC_CAPABILITIES", default="ASK_EXPERT") or "").split(",")
    if s.strip()
]
SPEC_SKILLS = [
    s.strip() for s in
    (_env("SPEC_SKILLS", default="general") or "").split(",")
    if s.strip()
]
SPEC_HEARTBEAT_SEC = int(_env("SPEC_HEARTBEAT_SEC", default="30") or "30")

# AI backend (gdy hook nic nie zwróci)
AI_BACKEND    = (_env("AI_BACKEND", default="openai") or "openai").lower()
SYSTEM_PROMPT = _env(
    "SPEC_SYSTEM_PROMPT",
    default=("Jesteś rzeczowym ekspertem. Odpowiadaj zwięźle, "
             "biorąc pod uwagę pytanie i skrót historii."),
)
LOCAL_AI_CMD       = _env("LOCAL_AI_CMD", default=None)
LOCAL_AI_TIMEOUT_S = float(_env("LOCAL_AI_TIMEOUT_S", default="30") or "30")
LOCAL_AI_MODEL     = _env("LOCAL_AI_MODEL", default=None)

# Ładowanie logiki (moduł:Klasa), np. "logic_example:MyLogic"
SPEC_LOGIC = _env("SPEC_LOGIC", default=None)

# ====== utilsy ===============================================================
def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def bare(j: Any) -> str:
    return str(j).split("/")[0] if j else ""

def make_acl(
    performative: str, sender: str, receiver: str, content: Dict[str, Any],
    conversation_id: Optional[str] = None, reply_with: Optional[str] = None,
    in_reply_to: Optional[str] = None, protocol: Optional[str] = None
) -> str:
    prot = protocol or ("fipa-query" if performative == "QUERY-REF"
                        else "fipa-request")
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
    }, ensure_ascii=False)

def parse_acl(body: str) -> Dict[str, Any]:
    return json.loads(body or "{}")

# ====== HOOKI: interfejs i loader ============================================
class BaseLogic:
    """
    Prosty interfejs na „własną logikę”.
    Wystarczy zaimplementować tylko to, czego potrzebujesz.
    Zwracaj:
      - ask_expert -> str lub None (None oznacza: użyj AI).
    """
    async def on_register(self, profile: Dict[str, Any]) -> None:
        return None

    async def on_heartbeat(self, jid: str, status: str, ts: str) -> None:
        return None

    async def ask_expert(self, question: str,
                         history: List[Dict[str, Any]]) -> Optional[str]:
        return None

def _load_logic_from_env() -> BaseLogic:
    """
    Ładuje klasę logiki z ENV SPEC_LOGIC w formacie "modul:Klasa".
    Gdy brak ENV lub błąd – zwraca pustą logikę (BaseLogic()).
    """
    if not SPEC_LOGIC:
        return BaseLogic()
    try:
        mod_name, cls_name = SPEC_LOGIC.split(":", 1)
        mod = importlib.import_module(mod_name)
        cls = getattr(mod, cls_name)
        inst = cls()  # bezargumentowy konstruktor
        if not isinstance(inst, BaseLogic):
            # Nie zmuszamy do dziedziczenia – wystarczy, że ma te metody.
            # Dla prostoty „udajemy” BaseLogic przez wrapper.
            return _wrap_loose_logic(inst)
        return inst
    except Exception as e:
        print(f"[SPEC] Uwaga: nie udało się załadować SPEC_LOGIC={SPEC_LOGIC} "
              f"({e}). Użyję pustej logiki.")
        return BaseLogic()

def _wrap_loose_logic(obj: Any) -> BaseLogic:
    """
    Pozwala użyć klasy, która nie dziedziczy po BaseLogic,
    ale ma zgodne nazwy metod. Brakujące metody „domyka”.
    """
    class _Loose(BaseLogic):
        async def on_register(self, profile, *a, **k):
            m = getattr(obj, "on_register", None)
            return await m(profile) if asyncio.iscoroutinefunction(m) \
                   else (m(profile) if m else None)

        async def on_heartbeat(self, jid, status, ts, *a, **k):
            m = getattr(obj, "on_heartbeat", None)
            return await m(jid, status, ts) if asyncio.iscoroutinefunction(m) \
                   else (m(jid, status, ts) if m else None)

        async def ask_expert(self, q, h, *a, **k):
            m = getattr(obj, "ask_expert", None)
            if not m:
                return None
            return await m(q, h) if asyncio.iscoroutinefunction(m) else m(q, h)
    return _Loose()

# ====== Warstwa AI (fallback) ================================================
class _AIBase:
    async def aanswer(self, system_prompt: str, question: str,
                      history: List[Dict[str, Any]]) -> str:
        raise NotImplementedError

class _OpenAI(_AIBase):
    def __init__(self) -> None:
        self.ai = AIConnector()

    async def aanswer(self, system_prompt: str, question: str,
                      history: List[Dict[str, Any]]) -> str:
        messages = [
            {"role": "system", "content": system_prompt or ""},
            {"role": "user", "content": json.dumps(
                {"question": question, "history": history}, ensure_ascii=False)},
        ]
        res = await self.ai.achat_from_history(
            messages, caller="Specialist",
            extra={"response_format": {"type": "text"}}
        )
        if res.get("error"):
            return f"[AI/OpenAI] Błąd: {res['error']}"
        return (res.get("text") or "").strip()

class _LocalCmd(_AIBase):
    def __init__(self, cmd: Optional[str], timeout_s: float,
                 model_name: Optional[str]) -> None:
        self.cmd = cmd
        self.timeout_s = timeout_s
        self.model_name = model_name

    def _run_sync(self, payload: Dict[str, Any]) -> str:
        if not self.cmd:
            return "[AI/Local] Brak LOCAL_AI_CMD w konfiguracji."
        try:
            proc = subprocess.run(
                shlex.split(self.cmd),
                input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout_s,
            )
            out = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
            try:
                obj = json.loads(out)
                if isinstance(obj, dict) and "text" in obj:
                    return str(obj["text"])
            except Exception:
                pass
            return out
        except subprocess.TimeoutExpired:
            return "[AI/Local] Przekroczono czas lokalnego modelu."
        except Exception as e:
            return f"[AI/Local] Wyjątek: {e}"

    async def aanswer(self, system_prompt: str, question: str,
                      history: List[Dict[str, Any]]) -> str:
        payload = {
            "system_prompt": system_prompt,
            "question": question,
            "history": history,
            "model": self.model_name,
            "ts": now_iso(),
        }
        return await asyncio.to_thread(self._run_sync, payload)

def _build_ai() -> _AIBase:
    if AI_BACKEND == "local":
        return _LocalCmd(LOCAL_AI_CMD, LOCAL_AI_TIMEOUT_S, LOCAL_AI_MODEL)
    return _OpenAI()

# ====== Zachowanie: odbiór i obsługa zleceń ==================================
class SpecialistCycle(CyclicBehaviour):
    async def run(self):
        msg = await self.receive(timeout=1)
        if not msg:
            return
        try:
            acl = parse_acl(msg.body)
        except Exception:
            return

        pf   = str(acl.get("performative") or "").upper()
        cont = acl.get("content") or {}
        typ  = str((cont.get("type") or "")).upper()
        conv = acl.get("conversation_id")

        if pf == "REQUEST" and typ == "ASK_EXPERT":
            # 1) AGREE
            agree = Message(to=bare(msg.sender))
            agree.body = make_acl(
                "AGREE", "Specialist", "Coordinator",
                content={"type": "ASK_EXPERT", "note": "Przyjęto."},
                conversation_id=conv,
            )
            await self.send(agree)
            print(f"[SPEC] {now_iso()} ← REQUEST.ASK_EXPERT conv={conv} — AGREE")

            # 2) Dane do odpowiedzi
            question = ((cont.get("args") or {}).get("question") or "").strip()
            history  = (cont.get("args") or {}).get("history") or []

            # 3) Najpierw własna logika (hook). Jeśli nic nie odda,
            #    używamy fallbacku AI (OpenAI / Local).
            try:
                txt = await self.agent.logic.ask_expert(question, history)
                if not txt:
                    txt = await self.agent.ai.aanswer(
                        self.agent.system_prompt, question, history
                    )
            except Exception as e:
                txt = f"[SPEC] Błąd podczas generowania odpowiedzi: {e}"

            # 4) INFORM.RESULT
            reply = Message(to=bare(msg.sender))
            reply.body = make_acl(
                "INFORM", "Specialist", "Coordinator",
                content={
                    "type": "RESULT",
                    "result": {
                        "answer": txt,
                        "meta": {
                            "backend": AI_BACKEND
                            if not await _has_own_answer(self.agent.logic, question, history)
                            else "hook",
                            "model_hint": (LOCAL_AI_MODEL if AI_BACKEND == "local"
                                           else os.getenv("OPENAI_MODEL")),
                        },
                    },
                },
                conversation_id=conv,
            )
            await self.send(reply)
            print(f"[SPEC] {now_iso()} → INFORM.RESULT conv={conv}")

async def _has_own_answer(logic: BaseLogic, q: str,
                          h: List[Dict[str, Any]]) -> bool:
    """
    Delikatnie sprawdza, czy logika zwróciłaby coś własnego.
    Nie wołamy drugi raz ciężkiej logiki; tu tylko heurystyka:
    jeśli klasa nadpisuje ask_expert, uznajemy „hook”.
    """
    impl = logic.__class__.ask_expert is not BaseLogic.ask_expert
    return impl

# ====== DF REGISTER + HEARTBEAT ==============================================
class RegisterOnce(OneShotBehaviour):
    async def run(self):
        jid_bare = bare(str(self.agent.jid))
        profile = {
            "jid": jid_bare,
            "name": SPEC_NAME,
            "description": SPEC_DESCRIPTION,
            "capabilities": SPEC_CAPABILITIES,
            "skills": SPEC_SKILLS,
            "version": SPEC_VERSION,
            "status": SPEC_STATUS,
            "ttl_sec": SPEC_HEARTBEAT_SEC * 3,
            "ai_backend": AI_BACKEND,
            "models": ([LOCAL_AI_MODEL] if (AI_BACKEND == "local"
                       and LOCAL_AI_MODEL) else [os.getenv("OPENAI_MODEL")]),
        }
        # hook: pozwól logice zareagować na rejestrację
        try:
            await self.agent.logic.on_register(profile)
        except Exception as e:
            print(f"[SPEC] on_register hook błąd: {e}")

        body_reg = {
            "performative": "REQUEST",
            "sender": jid_bare,
            "receiver": "Registry",
            "ontology": "MAS.Core",
            "protocol": "fipa-request",
            "language": "application/json",
            "timestamp": now_iso(),
            "conversation_id": f"df-{uuid.uuid4().hex}",
            "content": {"type": "REGISTER", "profile": profile},
        }
        msg_reg = Message(to=self.agent.registry_jid)
        msg_reg.body = json.dumps(body_reg, ensure_ascii=False)
        await self.send(msg_reg)
        print(f"[SPEC] {now_iso()} ->DF REGISTER {self.agent.registry_jid} "
              f"name={SPEC_NAME} caps={SPEC_CAPABILITIES}")

        await self.agent.send_heartbeat()

class Heartbeat(PeriodicBehaviour):
    async def run(self):
        await self.agent.send_heartbeat()

# ====== Agent ================================================================
class SpecialistAgent(Agent):
    def __init__(self, jid: str, password: str, verify_security: bool = True):
        super().__init__(jid, password, verify_security=verify_security)
        self.registry_jid = DF_JID
        self.system_prompt = SYSTEM_PROMPT
        self.logic = _load_logic_from_env()
        self.ai = _build_ai()

    async def setup(self):
        print(f"[SPEC] Start jako {self.jid}. DF={self.registry_jid} "
              f"HB={SPEC_HEARTBEAT_SEC}s AI={AI_BACKEND} LOGIC={SPEC_LOGIC}")
        self.add_behaviour(SpecialistCycle())
        self.add_behaviour(RegisterOnce())
        self.add_behaviour(Heartbeat(period=SPEC_HEARTBEAT_SEC))

    async def send_heartbeat(self):
        jid_bare = bare(str(self.jid))
        hb = {
            "type": "HEARTBEAT",
            "jid": jid_bare,
            "status": SPEC_STATUS,
            "name": SPEC_NAME,
            "description": SPEC_DESCRIPTION,
            "capabilities": SPEC_CAPABILITIES,
            "skills": SPEC_SKILLS,
            "version": SPEC_VERSION,
        }
        # hook: pozwól logice odnotować heartbeat
        try:
            await self.logic.on_heartbeat(jid_bare, SPEC_STATUS, now_iso())
        except Exception as e:
            print(f"[SPEC] on_heartbeat hook błąd: {e}")

        body = {
            "performative": "INFORM",
            "sender": jid_bare,
            "receiver": "Registry",
            "ontology": "MAS.Core",
            "protocol": "fipa-request",
            "language": "application/json",
            "timestamp": now_iso(),
            "conversation_id": f"df-{uuid.uuid4().hex}",
            "content": hb,
        }
        msg = Message(to=self.registry_jid)
        msg.body = json.dumps(body, ensure_ascii=False)
        await self.send(msg)
        print(f"[SPEC] {now_iso()} ->DF HEARTBEAT tick")

# ====== MAIN =================================================================
async def amain():
    if not SPEC_JID or not SPEC_PASS:
        raise SystemExit("Brak SPECIALIST_JID/SPECIALIST_PASS "
                         "(lub XMPP_JID/XMPP_PASS).")
    ag = SpecialistAgent(SPEC_JID, SPEC_PASS, verify_security=VERIFY_SECURITY)
    await ag.start(auto_register=AUTO_REGISTER)
    print("[SPEC] Agent wystartował. CTRL+C aby zakończyć.")
    try:
        while ag.is_alive():
            await asyncio.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        await ag.stop()

if __name__ == "__main__":
    asyncio.run(amain())
