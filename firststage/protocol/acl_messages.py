# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, Dict, Literal, Optional, Set

from pydantic import BaseModel, Field, field_validator, model_validator


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_reply_id(prefix: str = "msg") -> str:
    return f"{prefix}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"


VALID_PERFORMATIVES: Set[str] = {
    "ACCEPT-PROPOSAL",
    "AGREE",
    "CANCEL",
    "CFP",
    "CONFIRM",
    "DISCONFIRM",
    "FAILURE",
    "INFORM",
    "INFORM-IF",
    "INFORM-REF",
    "NOT-UNDERSTOOD",
    "PROPOSE",
    "QUERY-IF",
    "QUERY-REF",
    "REFUSE",
    "REJECT-PROPOSAL",
    "REQUEST",
    "REQUEST-WHEN",
    "REQUEST-WHENEVER",
    "SUBSCRIBE",
}

Performative = Literal[
    "ACCEPT-PROPOSAL",
    "AGREE",
    "CANCEL",
    "CFP",
    "CONFIRM",
    "DISCONFIRM",
    "FAILURE",
    "INFORM",
    "INFORM-IF",
    "INFORM-REF",
    "NOT-UNDERSTOOD",
    "PROPOSE",
    "QUERY-IF",
    "QUERY-REF",
    "REFUSE",
    "REJECT-PROPOSAL",
    "REQUEST",
    "REQUEST-WHEN",
    "REQUEST-WHENEVER",
    "SUBSCRIBE",
]

_space_or_underscore = re.compile(r"[ _]+")


def normalize_performative(pf: str | None) -> str:
    if not pf:
        return ""
    s = _space_or_underscore.sub("-", str(pf).strip())
    s = (
        s.upper()
        .replace("ACCEPTPROPOSAL", "ACCEPT-PROPOSAL")
        .replace("REJECTPROPOSAL", "REJECT-PROPOSAL")
        .replace("INFORMIF", "INFORM-IF")
        .replace("INFORMREF", "INFORM-REF")
        .replace("QUERYIF", "QUERY-IF")
        .replace("QUERYREF", "QUERY-REF")
        .replace("REQUESTWHEN", "REQUEST-WHEN")
        .replace("REQUESTWHENEVER", "REQUEST-WHENEVER")
    )
    s = re.sub(r"-{2,}", "-", s)
    return s


def default_protocol_for(pf: str) -> str:
    if pf.startswith("QUERY-"):
        return "fipa-query"
    if pf == "SUBSCRIBE":
        return "fipa-subscribe"
    if pf in {"CFP", "PROPOSE", "ACCEPT-PROPOSAL", "REJECT-PROPOSAL"}:
        return "fipa-contract-net"
    return "fipa-request"


class AclMessage(BaseModel):
    performative: Performative
    sender: str
    receiver: str
    ontology: str = "MAS.Core"
    protocol: str = Field(default="fipa-request")
    language: str = Field(default="application/json")
    timestamp: str = Field(default_factory=now_iso)
    conversation_id: Optional[str] = None
    reply_with: Optional[str] = None
    in_reply_to: Optional[str] = None
    content: Dict[str, Any]

    # --- Pydantic v2 validators ---

    @field_validator("performative", mode="before")
    @classmethod
    def _norm_pf(cls, v: Any) -> str:
        pf = normalize_performative(v)
        if pf not in VALID_PERFORMATIVES:
            raise ValueError(f"Unknown performative: {v!r}")
        return pf

    @model_validator(mode="after")
    def _ensure_protocol(self) -> "AclMessage":
        # Jeśli użytkownik nie podał protocol → ustawiamy domyślny wg performatywu
        if not (self.protocol or "").strip():
            self.protocol = default_protocol_for(self.performative)
        return self

    # --- Serialization helpers ---

    def dumps(self) -> str:
        # Pydantic v2: używamy model_dump()
        return json.dumps(self.model_dump(), ensure_ascii=False)

    @staticmethod
    def loads(body: str) -> "AclMessage":
        return AclMessage(**json.loads(body or "{}"))


def make_acl(
    performative: str,
    sender: str,
    receiver: str,
    *,
    content: Dict[str, Any],
    conversation_id: Optional[str] = None,
    reply_with: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    protocol: Optional[str] = None,
    ontology: Optional[str] = None,
    language: Optional[str] = None,
) -> str:
    pf_norm = normalize_performative(performative)
    msg = AclMessage(
        performative=pf_norm,
        sender=sender,
        receiver=receiver,
        ontology=ontology or "MAS.Core",
        protocol=protocol or default_protocol_for(pf_norm),
        language=language or "application/json",
        content=content,
        conversation_id=conversation_id,
        reply_with=reply_with,
        in_reply_to=in_reply_to,
    )
    return msg.dumps()
