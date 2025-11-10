# -*- coding: utf-8 -*-
from __future__ import annotations
from typing import Optional, Dict, Any

def bare(j: Optional[str]) -> str:
    return (j or "").split("/")[0]

def allow_if_correlated(corr, acl: Dict[str, Any], *, from_bare: str) -> bool:
    conv = acl.get("conversation_id") or ""
    pf   = (acl.get("performative") or "").upper()
    irt  = acl.get("in_reply_to")
    return corr.match_and_pop(conv, irt, from_bare=from_bare, performative=pf)
