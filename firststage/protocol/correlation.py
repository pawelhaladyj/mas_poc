# -*- coding: utf-8 -*-
from __future__ import annotations

import time
from typing import Dict, List, Optional, Set
from dataclasses import dataclass

# PF-y traktowane jako „ack” (nie powinny konsumować oczekiwania przy scenariuszu wieloetapowym)
ACK_PFS: Set[str] = {"AGREE"}

@dataclass
class Expectation:
    """Pojedyncze oczekiwanie korelacyjne dla (conversation_id, reply_with)."""
    allow_from: Set[str]            # dozwolone bare-JID (puste = dowolny)
    allow_pf: Set[str]              # dozwolone performatywy UPPER (puste = dowolny)
    expires_at: float               # znacznik wygaśnięcia
    note: str                       # opcjonalny opis/debug
    # NOWE: jeśli ustawione – wpis konsumuje się WYŁĄCZNIE na PF-ach z tego zbioru
    consume_on: Optional[Set[str]] = None


class CorrBook:
    """
    Rejestr oczekiwań korelacyjnych:
      (conv_id) -> (reply_with) -> Expectation

    Zasady:
    - match_and_pop(conv, None, ...) zwraca True (luźny tryb dla ramek inicjalnych).
    - Jeżeli wpis istnieje i pasuje (from + performative + TTL), zwracamy True.
      Konsumpcja (pop) zależy od polityki consume_on / heurystyki ACK.
    - Jeżeli nie pasuje albo wygasł → False (wygasły także usuwamy).
    - Gdy kubełek konwersacji się opróżni, czyścimy go z mapy.
    """

    def __init__(self, ttl_sec: float = 30.0):
        self.ttl = float(ttl_sec)
        self._by_conv: Dict[str, Dict[str, Expectation]] = {}

    # API
    # ---

    def register(
        self,
        conv_id: str,
        reply_with: str,
        *,
        allow_from: Optional[List[str]] = None,
        allow_pf: Optional[List[str]] = None,
        ttl_sec: Optional[float] = None,
        note: str = "",
    ) -> None:
        """
        Zarejestruj oczekiwanie na ramkę zwrotną identyfikowaną przez (conv_id, reply_with).

        :param conv_id: identyfikator konwersacji
        :param reply_with: oczekiwany identyfikator odpowiedzi (in_reply_to)
        :param allow_from: lista dopuszczalnych bare-JID nadawcy (pusta → dowolny)
        :param allow_pf: lista dopuszczalnych performatywów (case-insensitive; pusta → dowolny)
        :param ttl_sec: czas życia wpisu
        :param note: opcjonalny opis do debugowania
        """
        ttl = self.ttl if ttl_sec is None else float(ttl_sec)
        pf_set: Set[str] = {pf.upper() for pf in (allow_pf or [])} if allow_pf else set()

        # Domyślna, wstecznie kompatybilna polityka:
        # jeśli oczekujemy zarówno AGREE jak i INFORM → konsumuj wyłącznie na INFORM.
        consume_on: Optional[Set[str]] = None
        if {"AGREE", "INFORM"}.issubset(pf_set):
            consume_on = {"INFORM"}

        bucket = self._by_conv.setdefault(conv_id, {})
        bucket[reply_with] = Expectation(
            allow_from=set(allow_from or []),
            allow_pf=pf_set,
            expires_at=time.time() + ttl,
            note=note,
            consume_on=consume_on,
        )

    def match_and_pop(
        self,
        conv_id: str,
        in_reply_to: Optional[str],
        *,
        from_bare: Optional[str],
        performative: Optional[str],
    ) -> bool:
        """
        Sprawdź dopasowanie odpowiedzi i w razie potrzeby usuń wpis.

        :param conv_id: identyfikator konwersacji
        :param in_reply_to: wartość z nagłówka odpowiedzi; None traktujemy jako ramkę inicjalną
        :param from_bare: bare-JID nadawcy odpowiedzi (np. "agent@domain")
        :param performative: np. "INFORM", "AGREE" (case-insensitive)
        :return: True jeśli dopasowano (niezależnie od tego, czy wpis skonsumowano), False w przeciwnym razie
        """
        # Luźny tryb dla ramek inicjalnych (brak korelacji wymaganej)
        if not in_reply_to:
            return True

        bucket = self._by_conv.get(conv_id)
        if not bucket:
            return False

        exp = bucket.get(in_reply_to)
        if not exp:
            return False

        # TTL
        now = time.time()
        if now > exp.expires_at:
            bucket.pop(in_reply_to, None)
            self._cleanup_conv(conv_id)
            return False

        # Nadawca (jeśli ograniczono)
        if exp.allow_from:
            if not from_bare or from_bare not in exp.allow_from:
                return False

        # Performative (jeśli ograniczono)
        pf = (performative or "").upper()
        if exp.allow_pf and pf not in exp.allow_pf:
            return False

        # --- Polityka konsumpcji wpisu ---
        should_consume = True

        if exp.consume_on is not None:
            # Konsumujemy WYŁĄCZNIE na PF-ach końcowych
            should_consume = pf in exp.consume_on
        else:
            # Heurystyka: przy wielofazowych oczekiwaniach nie konsumuj na ACK-ach (np. AGREE)
            multi_phase = len(exp.allow_pf) > 1
            if multi_phase and pf in ACK_PFS:
                should_consume = False

        if should_consume:
            bucket.pop(in_reply_to, None)
            self._cleanup_conv(conv_id)

        return True

    def sweep(self) -> None:
        """Usuń wszystkie wpisy, które wygasły (TTL)."""
        now = time.time()
        for conv_id, bucket in list(self._by_conv.items()):
            for rid, exp in list(bucket.items()):
                if now > exp.expires_at:
                    bucket.pop(rid, None)
            if not bucket:
                self._by_conv.pop(conv_id, None)

    # Wewnętrzne
    # ----------

    def _cleanup_conv(self, conv_id: str) -> None:
        bucket = self._by_conv.get(conv_id)
        if bucket is not None and not bucket:
            self._by_conv.pop(conv_id, None)


# ====== helpers (guards.py korzysta z tych funkcji) ======
def bare(j: Optional[str]) -> str:
    return (j or "").split("/")[0]

def allow_if_correlated(corr: CorrBook, acl: Dict[str, any], *, from_bare: str) -> bool:
    conv = acl.get("conversation_id") or ""
    pf   = (acl.get("performative") or "").upper()
    irt  = acl.get("in_reply_to")
    return corr.match_and_pop(conv, irt, from_bare=from_bare, performative=pf)
