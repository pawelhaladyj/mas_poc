# tools/dfctl_env.py
# Inspektor DF tylko na .env (LIST/NEED + agregacja „wszyscy kandydaci”)
import os, json, time, asyncio, sys
from typing import Any, Dict, List, Optional, Tuple

# --- dotenv (opcjonalnie) ---
try:
    from dotenv import load_dotenv, find_dotenv  # pip install python-dotenv
    _dotenv_path = find_dotenv(filename=".env", usecwd=True)
    if _dotenv_path:
        load_dotenv(_dotenv_path)
    else:
        print("[DFCTL] Uwaga: nie znaleziono .env (kontynuuję).")
except Exception as e:
    print(f"[DFCTL] Uwaga: problem z dotenv: {e} (kontynuuję).")

from spade.agent import Agent
from spade.behaviour import OneShotBehaviour
from spade.message import Message

def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def now_ts() -> float:
    return time.time()

def make_payload(content: dict, performative="QUERY-REF") -> dict:
    ts = int(time.time())
    return {
        "performative": performative,
        "sender": "InspectorCLI",
        "receiver": "Registry",
        "ontology": "MAS.Core",
        "protocol": "fipa-query",
        "language": "application/json",
        "timestamp": now_iso(),
        "conversation_id": f"sess-dfctl-{ts}",
        "reply_with": f"msg-dfctl-{ts*1000}",
        "content": content,
    }

def parse_df_reply(raw_body: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    try:
        body = json.loads(raw_body)
        content = body.get("content", body)
        return body, content
    except Exception:
        return {"raw": raw_body}, {"raw": raw_body}

def enrich_profiles(profiles: List[Dict[str, Any]], alive_ttl_sec: Optional[float]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    now = now_ts()
    for p in profiles or []:
        q = dict(p)
        last_seen = q.get("last_seen")
        if isinstance(last_seen, (int, float)):
            q["seen_at_iso"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(last_seen))
            q["age_sec"] = round(max(0.0, now - float(last_seen)), 3)
            q["alive"] = (alive_ttl_sec is None) or ((now - float(last_seen)) <= float(alive_ttl_sec))
        else:
            q["seen_at_iso"] = None
            q["age_sec"] = None
            q["alive"] = (q.get("status") != "offline")
        out.append(q)
    out.sort(key=lambda r: (str(r.get("jid","")), str(r.get("name",""))))
    return out

def build_cap_index(profiles: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    cap_index: Dict[str, List[str]] = {}
    for p in profiles:
        for cap in (p.get("capabilities") or []):
            cap_index.setdefault(cap, []).append(p.get("jid"))
    for k in list(cap_index.keys()):
        cap_index[k] = sorted(set(cap_index[k]))
    return dict(sorted(cap_index.items(), key=lambda kv: kv[0]))

class InspectorAgent(Agent):
    def __init__(self, jid: str, password: str, df_jid: str, mode: str, cap: Optional[str],
                 timeout_sec: float = 5.0, include_raw: bool = False, alive_ttl_sec: Optional[float] = None,
                 try_dump: bool = False):
        super().__init__(jid, password)
        self.df_jid = df_jid
        self.mode = mode
        self.cap = cap
        self.timeout_sec = timeout_sec
        self.include_raw = include_raw
        self.alive_ttl_sec = alive_ttl_sec
        self.try_dump = try_dump

    class Ask(OneShotBehaviour):
        async def _send_and_recv(self, payload: Dict[str, Any]):
            m = Message(to=self.agent.df_jid)
            m.body = json.dumps(payload)
            await self.send(m)
            rep = await self.receive(timeout=self.agent.timeout_sec)
            if not rep:
                return None, {"error": "timeout", "sent": payload}, {"error": "timeout", "sent": payload}
            full, content = parse_df_reply(rep.body)
            return rep, full, content

        async def run(self):
            result: Dict[str, Any] = {
                "received_at": now_iso(),
                "df_jid": self.agent.df_jid,
                "inspector_jid": str(self.agent.jid),
                "mode": self.agent.mode,
            }

            # 1) LIST (żywi)
            rep_list, full_list, content_list = await self._send_and_recv(make_payload({"type": "LIST"}))
            profiles_list = (content_list or {}).get("profiles", [])
            enriched_all = enrich_profiles(profiles_list, self.agent.alive_ttl_sec)
            cap_index = build_cap_index(enriched_all)
            candidates_all = sorted([p.get("jid") for p in enriched_all if p.get("jid")])

            result["list"] = {
                "df_timestamp": (content_list or {}).get("df_timestamp"),
                "count_profiles": len(enriched_all),
                "profiles": enriched_all,
                "candidates_all_jids": candidates_all,
                "cap_index": cap_index,
                "capabilities_all": list(cap_index.keys()),
            }
            if self.agent.include_raw:
                result["list"]["raw_reply"] = full_list

            # 2) NEED (jeśli proszono)
            if self.agent.mode == "need":
                need = (self.agent.cap or "").strip()
                rep_need, full_need, content_need = await self._send_and_recv(make_payload({"need": need}))
                cands_jids = (content_need or {}).get("candidates", [])
                cand_profiles = [p for p in enriched_all if p.get("jid") in set(cands_jids)]
                others = [p for p in enriched_all if p.get("jid") not in set(cands_jids)]
                result["need"] = {
                    "capability": need,
                    "count_candidates": len(cands_jids or []),
                    "candidates_jids": cands_jids,
                    "candidates_profiles": cand_profiles,
                    "others_profiles": others,
                    "df_timestamp": (content_need or {}).get("df_timestamp"),
                }
                if self.agent.include_raw:
                    result["need"]["raw_reply"] = full_need

            # 3) Opcjonalnie: pełny DUMP (wymaga wsparcia w DF)
            if self.agent.try_dump:
                _, full_dump, content_dump = await self._send_and_recv(make_payload({"type": "DUMP"}))
                result["dump"] = content_dump
                if self.agent.include_raw:
                    result["dump_raw"] = full_dump

            # 4) Podsumowanie
            result["summary"] = {
                "total_alive": len(result["list"]["profiles"]),
                "alive_candidates": len(result.get("need", {}).get("candidates_profiles", [])) if self.agent.mode == "need" else None,
            }

            print(json.dumps(result, indent=2, ensure_ascii=False))
            await self.agent.stop()

    async def setup(self):
        self.add_behaviour(self.Ask())

async def main():
    JID  = os.getenv("INSPECTOR_JID") or os.getenv("AGENT_JID")
    PASS = os.getenv("INSPECTOR_PASS") or os.getenv("AGENT_PASS")
    DF   = os.getenv("DF_JID")
    CMD  = (os.getenv("DFCTL_CMD") or "").strip().lower()  # list | need
    CAP  = os.getenv("DFCTL_CAPABILITY")
    TOUT = float(os.getenv("DFCTL_TIMEOUT", "5"))
    INCLUDE_RAW = (os.getenv("DFCTL_INCLUDE_RAW", "0").strip() == "1")
    ALIVE_TTL = os.getenv("DFCTL_ALIVE_TTL_SEC")
    ALIVE_TTL_SEC = float(ALIVE_TTL) if (ALIVE_TTL and ALIVE_TTL.strip()) else None
    TRY_DUMP = (os.getenv("DFCTL_TRY_DUMP", "0").strip() == "1")

    missing = []
    if not JID:  missing.append("INSPECTOR_JID (lub AGENT_JID)")
    if not PASS: missing.append("INSPECTOR_PASS (lub AGENT_PASS)")
    if not DF:   missing.append("DF_JID")
    if CMD not in ("list", "need"): missing.append("DFCTL_CMD (list|need)")
    if CMD == "need" and not CAP:   missing.append("DFCTL_CAPABILITY (dla need)")
    if missing:
        raise RuntimeError("[DFCTL] Braki w .env: " + ", ".join(missing))

    agent = InspectorAgent(JID, PASS, DF, CMD, CAP, TOUT, INCLUDE_RAW, ALIVE_TTL_SEC, TRY_DUMP)
    await agent.start(auto_register=False)
    while agent.is_alive():
        await asyncio.sleep(0.1)

if __name__ == "__main__":
    asyncio.run(main())
