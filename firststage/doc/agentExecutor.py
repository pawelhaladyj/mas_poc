# logic_example.py
# -*- coding: utf-8 -*-
# Minimalna „własna logika” do wpięcia przez SPEC_LOGIC=logic_example:MyLogic
# Wystarczy nadpisać to, czego potrzebujesz. Resztą zajmie się blueprint.

from typing import Any, Dict, List, Optional
from specialist_blueprint import BaseLogic  # tylko interfejs (lekkie)

class MyLogic(BaseLogic):
    async def on_register(self, profile: Dict[str, Any]) -> None:
        # Tu możesz np. dopisać profil do swojej bazy albo logów.
        print(f"[HOOK] REGISTER {profile.get('jid')} caps={profile.get('capabilities')}")

    async def on_heartbeat(self, jid: str, status: str, ts: str) -> None:
        # Tu np. metryka albo proste logowanie obecności.
        print(f"[HOOK] HEARTBEAT {jid} status={status} ts={ts}")

    async def ask_expert(self, question: str,
                         history: List[Dict[str, Any]]) -> Optional[str]:
        # Przykład „łagodnego” hooka:
        #  - jeśli pytanie zaczyna się od "echo:", odpowiadamy sami;
        #  - w innym razie zwracamy None → blueprint użyje AI fallback.
        q = (question or "").strip()
        if q.lower().startswith("echo:"):
            return f"[hook] {q[5:].strip()}"
        return None
