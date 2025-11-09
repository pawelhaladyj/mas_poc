# -*- coding: utf-8 -*-
# Coordinator – USER_MSG -> DF lookup -> AI select (z historią z KB) ->
# REQUEST.ASK_EXPERT (+history) -> RESULT -> PRESENTER_REPLY
# Dodatki: logging do KB (frame + timeline), historia do AI i do specjalisty.

import os
import json
import time
import asyncio
from typing import Dict, Any, Optional, List, Tuple

# --- dotenv (opcjonalnie) ---
try:
    from dotenv import load_dotenv, find_dotenv
    _dotenv_path = find_dotenv(filename=".env", usecwd=True)
    if _dotenv_path:
        load_dotenv(_dotenv_path)
    else:
        print("[COORD] Uwaga: nie znaleziono .env (kontynuuję).")
except Exception as e:
    print(f"[COORD] Uwaga: problem z dotenv: {e} (kontynuuję).")

# --- SPADE ---
from spade.agent import Agent
from spade.message import Message
from spade.behaviour import CyclicBehaviour, OneShotBehaviour

# --- AI Connector ---
from utils.aiconnector import AIConnector

# --- System prompt (z historią) ---
SELECTOR_SYSTEM_PROMPT: Optional[str] = None
try:
    from systemprompt import SELECTOR_SYSTEM_PROMPT as _SP  # type: ignore
    SELECTOR_SYSTEM_PROMPT = _SP
except Exception:
    SELECTOR_SYSTEM_PROMPT = (
        "Jesteś selektorem agentów.\n\n"
        "Wejście zawiera kontekst żądania, kandydatów z DF oraz 'history' – skrót ostatnich wypowiedzi wątku.\n"
        "Uwzględnij 'required_capability' i dopasowanie merytoryczne. Zwróć JSON:\n"
        "{ \"selected_jid\": \"...\", \"reason\": \"...\", \"confidence\": 0..1 }"
    )

# ====== ENV ======
def _env(*names: str, default: Optional[str] = None) -> Optional[str]:
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return default

AGENT_JID        = _env("COORDINATOR_JID", "AGENT_JID", "XMPP_JID")
AGENT_PASS       = _env("COORDINATOR_PASS", "AGENT_PASS", "XMPP_PASS")
REGISTRY_JID     = _env("REGISTRY_JID", "DF_JID", default="registry@xmpp.pawelhaladyj.pl")

NEED_CAP         = _env("NEED_CAP", default="ASK_EXPERT") or "ASK_EXPERT"
REQ_TIMEOUT_S    = int(_env("COORD_REQ_TIMEOUT", default="10") or "10")
MAX_RETRIES      = int(_env("COORD_MAX_RETRIES", default="2") or "2")
MAX_CONCURRENCY  = int(_env("COORD_MAX_CONCURRENCY", default="5") or "5")
CONV_GRACE_SEC   = float(_env("COORD_CONV_GRACE_SEC", default="0.5") or "0.5")
DF_MODE          = (_env("COORD_DF_MODE", default="NEED") or "NEED").upper()  # NEED | ALL
DEBUG_AI         = (_env("COORD_DEBUG_AI", default="1") or "1") == "1"

# --- KB integracja ---
KB_JID           = _env("KB_JID", default="kb@xmpp.pawelhaladyj.pl")
HISTORY_LEN      = int(_env("COORD_HISTORY_LEN", default="10") or "10")
KB_TIMEOUT_S     = int(_env("COORD_KB_TIMEOUT", default="5") or "5")

# ====== helpers ======
def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def now_ms() -> int:
    return int(time.time() * 1000)

def bare(j: Any) -> str:
    try:
        return str(j).split("/")[0]
    except Exception:
        return str(j or "")

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

def _history_text_from_acl(acl: Dict[str, Any]) -> str:
    c = (acl.get("content") or {})
    typ = str(c.get("type") or "").upper()
    if typ == "USER_MSG":
        return str((c.get("args") or {}).get("question") or "")
    if typ == "PRESENTER_REPLY":
        return str(c.get("text") or "")
    if typ == "RESULT":
        r = (c.get("result") or {})
        return str(r.get("answer") or c.get("answer") or "")
    return ""

# ====== AGENT ======
class CoordinatorAgent(Agent):
    def __init__(self, jid: str, password: str, *args, **kwargs):
        super().__init__(jid, password, *args, **kwargs)
        self.conv_queues: Dict[str, asyncio.Queue] = {}
        self.sem = asyncio.Semaphore(MAX_CONCURRENCY)
        self.registry_jid = REGISTRY_JID
        self.kb_jid = KB_JID
        self.history_len = HISTORY_LEN
        self.kb_timeout = KB_TIMEOUT_S
        self.ai = AIConnector()

    class Dispatcher(CyclicBehaviour):
        async def run(self):
            msg = await self.receive(timeout=1)
            if not msg:
                return

            try:
                acl = parse_acl(msg.body)
            except Exception as e:
                print(f"[COORD] {now_iso()} Odrzucono nie-JSON od {msg.sender}: {e}")
                return

            pf   = acl.get("performative")
            cont = acl.get("content") or {}
            typ  = (cont.get("type") or "").upper()
            conv = acl.get("conversation_id")

            # Przekieruj odpowiedzi KB do właściwych kolejek (oddzielne conv_id)
            if conv and any(tag in conv for tag in ("-kbget-", "-kbput-", "-kbframe")):
                q = self.agent.conv_queues.get(conv)
                if q:
                    await q.put(msg)
                return

            if pf == "REQUEST" and typ == "USER_MSG":
                if not conv:
                    conv = f"sess-{now_ms()}"
                    print(f"[COORD] {now_iso()} Brak conversation_id – nadano {conv}")

                if conv not in self.agent.conv_queues:
                    self.agent.conv_queues[conv] = asyncio.Queue()

                    presenter_jid = (cont.get("meta") or {}).get("presenter_jid") or bare(msg.sender)
                    question = (cont.get("args") or {}).get("question") or ""

                    self.agent.add_behaviour(
                        CoordinatorAgent.ServeConversation(
                            presenter_jid=presenter_jid,
                            question=question,
                            conv_id=conv,
                            orig_acl=acl,
                        )
                    )
                    print(f"[COORD] {now_iso()} ← USER_MSG od {presenter_jid} conv={conv} q={question!r}")
                return

            if not conv:
                print(f"[COORD] {now_iso()} Ignoruję ramkę bez conversation_id pf={pf} typ={typ} od {msg.sender}")
                return
            q = self.agent.conv_queues.get(conv)
            if q:
                await q.put(msg)
                print(f"[COORD] {now_iso()} Dyspozytor: dostarczono pf={pf} typ={typ} do conv={conv}")

    class ServeConversation(OneShotBehaviour):
        """Obsługa jednej rozmowy z KB-loggingiem i timeline."""
        def __init__(self, presenter_jid: str, question: str, conv_id: str, orig_acl: Dict[str, Any]):
            super().__init__()
            self.presenter_jid = presenter_jid
            self.question = question
            self.conv_id = conv_id
            self.orig_acl = orig_acl

        # ---------- KB helpery (TU, w behawiorze!) ----------
        def _kb_body(self, kb_conv: str, typ: str, payload: Dict[str, Any]) -> Dict[str, Any]:
            body = {
                "performative": "REQUEST",
                "sender": "Coordinator",
                "receiver": "KB",
                "ontology": "MAS.KB",
                "protocol": "fipa-request",
                "language": "application/json",
                "timestamp": now_iso(),
                "conversation_id": kb_conv,
                "type": typ,
            }
            body.update(payload)
            return body

        async def _kb_store_frame_ff(self, frame: Dict[str, Any]) -> None:
            kb_conv = f"{self.conv_id}-kbframe-{now_ms()}"
            key = f"session:{self.conv_id}:chat:frame:{now_ms()}"
            body = self._kb_body(kb_conv, "STORE", {
                "key": key,
                "content_type": "application/json",
                "value": frame,
                "tags": [f"conv:{self.conv_id}", f"type:{frame.get('type','').lower()}", f"from:{frame.get('agent','').lower()}"],
            })
            msg = Message(to=self.agent.kb_jid); msg.body = json.dumps(body, ensure_ascii=False)
            await self.send(msg)

        async def _kb_get_timeline(self) -> Tuple[List[Dict[str, Any]], Optional[int]]:
            kb_conv = f"{self.conv_id}-kbget-{now_ms()}"
            self.agent.conv_queues[kb_conv] = asyncio.Queue()
            try:
                body = self._kb_body(kb_conv, "GET", {
                    "key": f"session:{self.conv_id}:chat:timeline:main"
                })
                msg = Message(to=self.agent.kb_jid); msg.body = json.dumps(body, ensure_ascii=False)
                await self.send(msg)

                q = self.agent.conv_queues[kb_conv]
                deadline = time.time() + self.agent.kb_timeout
                while time.time() < deadline:
                    try:
                        m = await asyncio.wait_for(q.get(), timeout=min(1.0, deadline - time.time()))
                    except asyncio.TimeoutError:
                        continue
                    try:
                        acl = json.loads(m.body or "{}")
                    except Exception:
                        continue
                    if (acl.get("ontology") == "MAS.KB"
                            and acl.get("performative") == "INFORM"
                            and (acl.get("type") == "VALUE" or (acl.get("content") or {}).get("type") == "VALUE")):
                        key_ok = (acl.get("key")
                                  or (acl.get("content") or {}).get("key")) == f"session:{self.conv_id}:chat:timeline:main"
                        if not key_ok:
                            continue
                        content = acl.get("value") or (acl.get("content") or {}).get("value") or []
                        version = (acl.get("version")
                                   or (acl.get("content") or {}).get("version"))
                        try:
                            return list(content), int(version) if version is not None else None
                        except Exception:
                            return [], None
                    if (acl.get("ontology") == "MAS.KB"
                            and acl.get("performative") == "FAILURE"
                            and str(acl.get("type", "")).upper() == "FAILURE.NOT_FOUND"):
                        return [], None
                return [], None
            finally:
                try:
                    self.agent.conv_queues.pop(kb_conv, None)
                except Exception:
                    pass

        async def _kb_put_timeline(self, entries: List[Dict[str, Any]], if_match_version: Optional[int]) -> None:
            kb_conv = f"{self.conv_id}-kbput-{now_ms()}"
            if_match = f"v{int(if_match_version)}" if isinstance(if_match_version, int) else None
            body = self._kb_body(kb_conv, "STORE", {
                "key": f"session:{self.conv_id}:chat:timeline:main",
                "content_type": "application/json",
                "value": entries,
                "tags": [f"conv:{self.conv_id}", "kind:timeline"],
                "if_match": if_match,
            })
            msg = Message(to=self.agent.kb_jid); msg.body = json.dumps(body, ensure_ascii=False)
            await self.send(msg)

        async def _kb_log_acl_and_update_timeline(self, acl: Dict[str, Any]) -> List[Dict[str, Any]]:
            item = {
                "ts": now_iso(),
                "agent": str(acl.get("sender") or "").strip() or "Unknown",
                "pf": str(acl.get("performative") or ""),
                "type": str((acl.get("content") or {}).get("type") or ""),
                "text": _history_text_from_acl(acl),
            }
            await self._kb_store_frame_ff(item)
            curr, ver = await self._kb_get_timeline()
            curr = list(curr or [])
            curr.append(item)
            if self.agent.history_len > 0 and len(curr) > self.agent.history_len:
                curr = curr[-self.agent.history_len:]
            await self._kb_put_timeline(curr, ver)
            return curr

        # ---------- DF ----------
        async def df_lookup(self) -> List[Any]:
            async def _query(need_value: str) -> List[Any]:
                reply_id = f"dfq-{now_ms()}"
                msg = Message(to=self.agent.registry_jid)
                msg.set_metadata("conv", self.conv_id)
                msg.set_metadata("performative", "QUERY-REF")
                msg.body = make_acl(
                    "QUERY-REF", "Coordinator", "Registry",
                    content={"need": need_value},
                    conversation_id=self.conv_id,
                    reply_with=reply_id,
                    protocol="fipa-query",
                )
                print(f"[COORD] {now_iso()} → DF {self.agent.registry_jid} QUERY-REF need={need_value} conv={self.conv_id}")
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
                    except Exception:
                        continue
                    if acl.get("conversation_id") != self.conv_id:
                        continue
                    if acl.get("performative") == "INFORM":
                        await self._kb_log_acl_and_update_timeline(acl)
                        cont = acl.get("content") or {}
                        profiles = cont.get("profiles") or []
                        candidates = profiles or (cont.get("candidates") or [])
                        src = "profiles" if profiles else "candidates"
                        print(f"[COORD] {now_iso()} ← DF INFORM {src} count={len(candidates)} conv={self.conv_id}")
                        return candidates
                print(f"[COORD] {now_iso()} [DF] timeout po {REQ_TIMEOUT_S}s conv={self.conv_id}")
                return []

            if DF_MODE == "ALL":
                got = await _query("ALL")
                if not got:
                    print(f"[COORD] {now_iso()} [DF] ALL→pusto, fallback do NEED={NEED_CAP}")
                    got = await _query(NEED_CAP)
                return got
            else:
                return await _query(NEED_CAP)

        def _normalize_candidates(self, raw_list: List[Any]) -> List[Dict[str, Any]]:
            norm: List[Dict[str, Any]] = []
            for item in raw_list:
                if isinstance(item, str):
                    norm.append({
                        "jid": item, "name": item, "description": "",
                        "capabilities": [NEED_CAP], "skills": [], "status": "online",
                    })
                elif isinstance(item, dict):
                    norm.append({
                        "jid": item.get("jid", ""),
                        "name": item.get("name", item.get("jid", "")),
                        "description": item.get("description", ""),
                        "capabilities": item.get("capabilities", []),
                        "skills": item.get("skills", []),
                        "status": item.get("status", "online"),
                    })
            return [c for c in norm if c.get("jid")]

        def _build_fipa_request_for_prompt(self) -> Dict[str, Any]:
            content = self.orig_acl.get("content") or {}
            args = (content.get("args") or {})
            domain_tags = args.get("domain_tags") or []
            if not isinstance(domain_tags, list):
                domain_tags = [domain_tags]
            return {
                "performative": self.orig_acl.get("performative"),
                "ontology": self.orig_acl.get("ontology"),
                "sender": self.orig_acl.get("sender"),
                "content": {
                    "type": content.get("type"),
                    "args": {"question": args.get("question"), "domain_tags": domain_tags}
                }
            }

        async def _ai_select_candidate(self, candidates: List[Dict[str, Any]], history: List[Dict[str, Any]]) -> Optional[str]:
            if not candidates:
                return None
            selector_input = {
                "conversation_id": self.conv_id,
                "required_capability": NEED_CAP,
                "df_timestamp": now_iso(),
                "fipa_request": self._build_fipa_request_for_prompt(),
                "candidates": candidates,
                "history": history,
            }
            if DEBUG_AI:
                try:
                    preview = {
                        "selector_input": selector_input,
                        "system_prompt_preview": (SELECTOR_SYSTEM_PROMPT or "")[:240] +
                            ("..." if (SELECTOR_SYSTEM_PROMPT and len(SELECTOR_SYSTEM_PROMPT) > 240) else "")
                    }
                    print(f"[COORD] {now_iso()} [AI][DEBUG] payload → selector:\n"
                          f"{json.dumps(preview, ensure_ascii=False, indent=2)}")
                except Exception as e:
                    print(f"[COORD] {now_iso()} [AI][DEBUG] Błąd podczas logowania payloadu: {e}")

            messages = [
                {"role": "system", "content": SELECTOR_SYSTEM_PROMPT or ""},
                {"role": "user", "content": json.dumps(selector_input, ensure_ascii=False)},
            ]
            res = await self.agent.ai.achat_from_history(
                messages,
                caller="Coordinator",
                extra={"response_format": {"type": "json_object"}}
            )
            if res.get("error"):
                print(f"[COORD] {now_iso()} [AI] ERROR {res['error']}")
                return None
            txt = (res.get("text") or "").strip()
            if DEBUG_AI:
                print(f"[COORD] {now_iso()} [AI][DEBUG] raw response: {txt!r}")
            try:
                data = json.loads(txt) if txt else {}
            except Exception as e:
                print(f"[COORD] {now_iso()} [AI] Niepoprawny JSON z selektora: {e} / {txt!r}")
                return None
            selected = data.get("selected_jid")
            if not selected:
                print(f"[COORD] {now_iso()} [AI] Brak selected_jid w odpowiedzi.")
                return None
            c_jids = {c["jid"] for c in candidates}
            if selected not in c_jids:
                print(f"[COORD] {now_iso()} [AI] selected_jid={selected} nie jest na liście kandydatów.")
                return None
            print(f"[COORD] {now_iso()} [AI] Wybrano: {selected} (powód={data.get('reason')}, conf={data.get('confidence')})")
            return selected

        async def ask_specialist(self, specialist_jid: str, history: List[Dict[str, Any]]) -> Optional[str]:
            req_id = f"ask-{now_ms()}"
            msg = Message(to=specialist_jid)
            msg.set_metadata("conv", self.conv_id)
            msg.set_metadata("performative", "REQUEST")
            msg.body = make_acl(
                "REQUEST", "Coordinator", "Specialist",
                content={"type": "ASK_EXPERT", "args": {"question": self.question, "history": history}},
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
                except Exception:
                    continue
                if acl.get("conversation_id") != self.conv_id:
                    continue
                pf = acl.get("performative")
                cont = acl.get("content") or {}
                typ = (cont.get("type") or "").upper()

                if pf in ("AGREE", "INFORM"):
                    await self._kb_log_acl_and_update_timeline(acl)

                if pf == "AGREE":
                    if not got_agree:
                        print(f"[COORD] {now_iso()} ← SPEC AGREE conv={self.conv_id}")
                        got_agree = True
                    continue

                if pf == "INFORM" and typ == "RESULT":
                    ans = (cont.get("result") or {}).get("answer")
                    print(f"[COORD] {now_iso()} ← SPEC INFORM.RESULT conv={self.conv_id} answer={ans!r}")
                    return ans
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
                    # (0) Zaloguj USER_MSG do KB i uaktualnij timeline
                    history_after_user = await self._kb_log_acl_and_update_timeline(self.orig_acl)

                    # (1) DF lookup (+log DF INFORM do KB)
                    raw_candidates = await self.df_lookup()
                    if not raw_candidates:
                        await self.reply_to_presenter("Brak dostępnych specjalistów (ASK_EXPERT).")
                        return

                    candidates = self._normalize_candidates(raw_candidates)
                    if not candidates:
                        await self.reply_to_presenter("Brak poprawnych profili kandydatów.")
                        return

                    print(f"[COORD] {now_iso()} [CONV {self.conv_id}] Kandydaci (norm): {[c['jid'] for c in candidates]}")

                    # (2) Pobierz timeline i przekaż do selektora
                    tl, _ver = await self._kb_get_timeline()
                    history_for_ai = tl if tl else history_after_user
                    selected_jid = await self._ai_select_candidate(candidates, history_for_ai)

                    if not selected_jid:
                        avail = [c for c in candidates if str(c.get("status","")).lower() in ("online","available","ready")]
                        with_cap = [c for c in avail if NEED_CAP in (c.get("capabilities") or [])]
                        prefer = with_cap if with_cap else (avail if avail else candidates)
                        selected_jid = sorted([c["jid"] for c in prefer])[0]
                        print(f"[COORD] {now_iso()} [FALLBACK] Wybrano deterministycznie: {selected_jid}")

                    # (3) Timeline do specjalisty
                    tl2, _ver2 = await self._kb_get_timeline()
                    history_for_specialist = tl2 if tl2 else history_after_user

                    answer: Optional[str] = None
                    attempts = 0
                    ordered_try = [selected_jid] + [c["jid"] for c in candidates if c["jid"] != selected_jid]
                    for jid in ordered_try:
                        attempts += 1
                        print(f"[COORD] {now_iso()} [CONV {self.conv_id}] Próba {attempts}/{MAX_RETRIES} → {jid}")
                        answer = await self.ask_specialist(jid, history_for_specialist)
                        if answer:
                            break
                        if attempts >= MAX_RETRIES:
                            print(f"[COORD] {now_iso()} [CONV {self.conv_id}] Limit prób {MAX_RETRIES} osiągnięty")
                            break

                    # (4) Odpowiedź do Presentera
                    if answer:
                        await self.reply_to_presenter(answer)
                    else:
                        await self.reply_to_presenter("Specjalista nie odpowiedział w czasie. Spróbuj ponownie.")
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
                    print(f"[COORD] {now_iso()} [CONV {self.conv_id}] koniec")

    async def setup(self):
        print(f"[COORD] Start jako {self.jid}. DF={self.registry_jid} "
              f"NEED={NEED_CAP} TIMEOUT={REQ_TIMEOUT_S}s RETRIES={MAX_RETRIES} "
              f"CONCURRENCY={MAX_CONCURRENCY} DF_MODE={DF_MODE} KB={self.kb_jid} "
              f"HIST={self.history_len}@{self.kb_timeout}s")
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
