# tools/dfctl_env.py
# Inspektor DF oparty WYŁĄCZNIE na .env
# Tryby:
#   DFCTL_CMD=list              → QUERY-REF type=LIST (wymaga wsparcia po stronie DF)
#   DFCTL_CMD=need              → QUERY-REF need=<DFCTL_CAPABILITY>
#
# Wymagane w .env:
#   INSPECTOR_JID, INSPECTOR_PASS, DF_JID
#   DFCTL_CMD = list | need
#   DFCTL_CAPABILITY (wymagane tylko dla DFCTL_CMD=need)
#
# Opcjonalne:
#   DFCTL_TIMEOUT (sekundy, domyślnie 5)

import os, json, time, asyncio, sys

# --- dotenv (opcjonalnie; nadal tylko .env) ---
try:
    from dotenv import load_dotenv, find_dotenv  # pip install python-dotenv
    _dotenv_path = find_dotenv(filename=".env", usecwd=True)
    if _dotenv_path:
        load_dotenv(_dotenv_path)
    else:
        print("[DFCTL] Uwaga: nie znaleziono .env (kontynuuję, ale użyję tylko env procesu).")
except Exception as e:
    print(f"[DFCTL] Uwaga: problem z dotenv: {e} (kontynuuję).")

from spade.agent import Agent
from spade.behaviour import OneShotBehaviour
from spade.message import Message

def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

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

class InspectorAgent(Agent):
    def __init__(self, jid: str, password: str, df_jid: str, payload: dict, timeout_sec: float = 5.0):
        super().__init__(jid, password)
        self.df_jid = df_jid
        self.payload = payload
        self.timeout_sec = timeout_sec

    class Ask(OneShotBehaviour):
        async def run(self):
            m = Message(to=self.agent.df_jid)
            m.body = json.dumps(self.agent.payload)
            await self.send(m)

            rep = await self.receive(timeout=self.agent.timeout_sec)
            if not rep:
                print("[DFCTL] ERROR: Brak odpowiedzi DF (timeout).", file=sys.stderr)
                await self.agent.stop()
                return

            try:
                body = json.loads(rep.body)
                content = body.get("content", body)
                print(json.dumps(content, indent=2, ensure_ascii=False))
            except Exception:
                print(rep.body)
            await self.agent.stop()

    async def setup(self):
        self.add_behaviour(self.Ask())

async def main():
    JID  = os.getenv("INSPECTOR_JID") or os.getenv("AGENT_JID")
    PASS = os.getenv("INSPECTOR_PASS") or os.getenv("AGENT_PASS")
    DF   = os.getenv("DF_JID")

    CMD  = (os.getenv("DFCTL_CMD") or "").strip().lower()
    CAP  = os.getenv("DFCTL_CAPABILITY")
    TOUT = float(os.getenv("DFCTL_TIMEOUT", "5"))

    missing = []
    if not JID:  missing.append("INSPECTOR_JID (lub AGENT_JID)")
    if not PASS: missing.append("INSPECTOR_PASS (lub AGENT_PASS)")
    if not DF:   missing.append("DF_JID")
    if not CMD:  missing.append("DFCTL_CMD (list|need)")
    if missing:
        raise RuntimeError("[DFCTL] Brakujące zmienne w .env: " + ", ".join(missing))

    if CMD == "list":
        payload = make_payload({"type": "LIST"})
    elif CMD == "need":
        if not CAP:
            raise RuntimeError("[DFCTL] Dla DFCTL_CMD=need wymagana jest DFCTL_CAPABILITY.")
        payload = make_payload({"need": CAP})
    else:
        raise RuntimeError("[DFCTL] DFCTL_CMD musi mieć wartość 'list' lub 'need'.")

    agent = InspectorAgent(JID, PASS, DF, payload, TOUT)
    await agent.start(auto_register=False)
    while agent.is_alive():
        await asyncio.sleep(0.1)

if __name__ == "__main__":
    asyncio.run(main())
