# -*- coding: utf-8 -*-
"""
ai_connector.py – konektor do OpenAI „po staremu”, z:
- kontrolą wielkości kontekstu,
- obsługą 429 (retry 5× z 2 s przerwy),
- metodami synchronicznymi i asynchronicznymi (równoległe użycie).

Wymaga: pip install openai
Opcjonalnie: pip install tiktoken  (lepsze szacowanie tokenów)
"""

import os
import json
import time
import math
import asyncio
from typing import Dict, Any, List, Optional, Literal, Tuple

# --- dotenv (opcjonalnie) ---
try:
    from dotenv import load_dotenv, find_dotenv  # pip install python-dotenv
    _dotenv_path = find_dotenv(filename=".env", usecwd=True)
    if _dotenv_path:
        load_dotenv(_dotenv_path)
    else:
        print("[AI] Uwaga: nie znaleziono .env (kontynuuję).")
except Exception as e:
    print(f"[AI] Uwaga: problem z dotenv: {e} (kontynuuję).")

# --- OpenAI client i wyjątki ---
try:
    from openai import OpenAI
    from openai import OpenAIError
    # W nowych wersjach:
    try:
        from openai import APIStatusError, RateLimitError
    except Exception:  # starsze/alternatywne wersje
        APIStatusError = OpenAIError  # type: ignore
        RateLimitError = OpenAIError  # type: ignore
except Exception as e:
    raise RuntimeError("Brak biblioteki 'openai'. Zainstaluj: pip install openai") from e

# --- tiktoken (opcjonalnie, lepszy estymator tokenów) ---
try:
    import tiktoken  # type: ignore
except Exception:
    tiktoken = None  # fallback na heurystykę

# ====== ENV ======
def _env(*names: str, default: Optional[str] = None) -> Optional[str]:
    for n in names:
        v = os.getenv(n)
        if v is not None and v != "":
            return v
    return default

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def _to_int(x: Optional[str]) -> Optional[int]:
    try:
        return int(x) if x not in (None, "", "0") else None
    except Exception:
        return None

def _to_float(x: Optional[str]) -> Optional[float]:
    try:
        return float(x) if x not in (None, "") else None
    except Exception:
        return None

OPENAI_API_KEY       = _env("OPENAI_API_KEY", "AI_API_KEY")
OPENAI_BASE_URL      = _env("OPENAI_BASE_URL", "AI_BASE_URL")
OPENAI_MODEL         = _env("OPENAI_MODEL", "AI_MODEL", default="gpt-4o-mini")
OPENAI_TEMPERATURE   = _to_float(_env("OPENAI_TEMPERATURE", default="0.2")) or 0.2
OPENAI_MAX_TOKENS    = _to_int(_env("OPENAI_MAX_TOKENS"))  # None → bez limitu
OPENAI_SEED          = _to_int(_env("OPENAI_SEED"))        # None → bez seed
OPENAI_CTX_LIMIT     = _to_int(_env("OPENAI_CTX_LIMIT"))   # opcjonalne, nadpisze mapę
OPENAI_RESERVE_TOKENS= _to_int(_env("OPENAI_RESERVE_TOKENS", default="1024")) or 1024

RETRY_MAX_CYCLES     = _to_int(_env("OPENAI_RETRY_MAX", default="5")) or 5
RETRY_SLEEP_SEC      = _to_float(_env("OPENAI_RETRY_SLEEP", default="2")) or 2.0

Role = Literal["system", "user", "assistant"]

# Mapowanie przybliżonych limitów kontekstu (można nadpisać przez OPENAI_CTX_LIMIT)
# W razie nieznanego modelu przyjmujemy 128k.
_CTX_LIMITS: Dict[str, int] = {
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4.1": 128_000,
    "gpt-4.1-mini": 128_000,
    "o3": 200_000,
    "o3-mini": 200_000,
}

def _ctx_limit_for(model: str) -> int:
    if OPENAI_CTX_LIMIT:
        return OPENAI_CTX_LIMIT
    for key, val in _CTX_LIMITS.items():
        if model.startswith(key):
            return val
    return 128_000

def _encoding_for_model(model: str):
    """Dobór encodera tiktoken; fallback na cl100k_base/o200k_base."""
    if not tiktoken:
        return None
    # Prosty wybór – dla 4o/4.1 i pochodnych spróbuj o200k_base
    try:
        if any(model.startswith(p) for p in ("gpt-4o", "gpt-4.1", "o3")):
            return tiktoken.get_encoding("o200k_base")
    except Exception:
        pass
    try:
        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None

def _estimate_tokens(messages: List[Dict[str, Any]], model: str) -> int:
    """
    Szacowanie liczby tokenów. Jeśli dostępny tiktoken – użyj go,
    inaczej heurystyka: ~ 1 token / 4 znaki + narzut na role/nagłówki.
    """
    # Złącz treści – to nie jest dokładne, ale stabilne w praktyce
    contents = []
    for m in messages:
        c = m.get("content")
        contents.append(c if isinstance(c, str) else json.dumps(c, ensure_ascii=False))
    text = "\n".join(contents)

    enc = _encoding_for_model(model)
    if enc:
        try:
            return len(enc.encode(text)) + 4 * len(messages)  # mały narzut za role
        except Exception:
            pass

    # Heurystyka 4-znaki-na-token + narzut za role
    approx = math.ceil(len(text) / 4)
    return approx + 6 * len(messages)

# ====== Konektor ======
class AIConnector:
    """
    Konektor synchroniczny z kontrolą kontekstu i retry 429.
    API:
      - chat_one(message, caller=None, **opts)
      - chat_from_history(messages, caller=None, **opts)
      - achat_one(...), achat_from_history(...) – warianty async (wątki)
    Zwracany wynik: dict z polami: id, model, created, finish_reason, usage, text, raw, (opcjonalnie) error.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        default_model: Optional[str] = None,
        *,
        default_temperature: float = OPENAI_TEMPERATURE,
        default_max_tokens: Optional[int] = OPENAI_MAX_TOKENS,
        default_seed: Optional[int] = OPENAI_SEED,
    ) -> None:
        self.api_key = api_key or OPENAI_API_KEY
        if not self.api_key:
            raise RuntimeError("Brak OPENAI_API_KEY (w .env lub środowisku).")

        self.base_url = base_url or OPENAI_BASE_URL
        self.default_model = default_model or OPENAI_MODEL
        self.default_temperature = default_temperature
        self.default_max_tokens = default_max_tokens
        self.default_seed = default_seed

        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        print(f"[AI] {_now_iso()} Konektor gotowy. model={self.default_model}")

    # ---------- Publiczne (sync) ----------
    def chat_one(
        self,
        message: Dict[str, Any],
        *,
        caller: Optional[str] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        seed: Optional[int] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        self._validate_message(message)
        return self._chat(
            [message],
            caller=caller,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            seed=seed,
            extra=extra,
        )

    def chat_from_history(
        self,
        messages: List[Dict[str, Any]],
        *,
        caller: Optional[str] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        seed: Optional[int] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        self._validate_messages(messages)
        return self._chat(
            messages,
            caller=caller,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            seed=seed,
            extra=extra,
        )

    # ---------- Publiczne (async – równoległe użycie) ----------
    async def achat_one(self, message: Dict[str, Any], **kw) -> Dict[str, Any]:
        """Asynchroniczny wariant chat_one – wykonywany w wątku, nie blokuje event loop."""
        return await asyncio.to_thread(self.chat_one, message, **kw)

    async def achat_from_history(self, messages: List[Dict[str, Any]], **kw) -> Dict[str, Any]:
        """Asynchroniczny wariant chat_from_history – wykonywany w wątku, nie blokuje event loop."""
        return await asyncio.to_thread(self.chat_from_history, messages, **kw)

    # ---------- Prywatne ----------
    def _chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        caller: Optional[str],
        model: Optional[str],
        temperature: Optional[float],
        max_tokens: Optional[int],
        seed: Optional[int],
        extra: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        mdl = model or self.default_model
        payload: Dict[str, Any] = {
            "model": mdl,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.default_temperature,
        }
        if (max_tokens is not None) or (self.default_max_tokens is not None):
            payload["max_tokens"] = max_tokens if max_tokens is not None else self.default_max_tokens
        if (seed is not None) or (self.default_seed is not None):
            payload["seed"] = seed if seed is not None else self.default_seed
        if extra:
            payload.update(extra)

        # --- kontrola kontekstu ---
        ctx_limit = _ctx_limit_for(mdl)
        est_tokens = _estimate_tokens(messages, mdl)
        limit_for_input = ctx_limit - (payload.get("max_tokens") or OPENAI_RESERVE_TOKENS or 0)

        if est_tokens > limit_for_input:
            ts = _now_iso()
            agent = caller or "unknown"
            print(f"[AI] {ts} KONTEKST ZA DUŻY: est={est_tokens} > limit_in={limit_for_input} (model={mdl}) agent={agent}")
            return {
                "id": None,
                "model": mdl,
                "created": int(time.time()),
                "finish_reason": "context_too_large",
                "usage": None,
                "text": (
                    f"Zapytanie przekracza obsługiwany kontekst modelu ({ctx_limit} tok.). "
                    f"Agent: {agent}. Czas: {ts}. (Szacowane wejście: {est_tokens} tok.)"
                ),
                "error": {
                    "type": "context_too_large",
                    "agent": agent,
                    "at": ts,
                    "limit_tokens": ctx_limit,
                    "estimated_tokens": est_tokens,
                    "model": mdl,
                },
                "raw": None,
            }

        # --- wywołanie OpenAI z retry na 429 ---
        print(f"[AI] {_now_iso()} → chat.completions.create model={mdl} msgs={len(messages)} est_in={est_tokens}")
        attempt = 0
        last_err: Optional[Exception] = None
        while attempt < RETRY_MAX_CYCLES:
            attempt += 1
            try:
                resp = self.client.chat.completions.create(**payload)
                # OK
                choice = resp.choices[0]
                text = ""
                try:
                    text = getattr(choice.message, "content", None) or ""
                except Exception:
                    text = ""

                result: Dict[str, Any] = {
                    "id": getattr(resp, "id", None),
                    "model": getattr(resp, "model", None),
                    "created": getattr(resp, "created", None),
                    "finish_reason": getattr(choice, "finish_reason", None),
                    "usage": (getattr(resp, "usage", None).model_dump()  # type: ignore[attr-defined]
                              if getattr(resp, "usage", None) else None),
                    "text": text,
                    "raw": (resp.model_dump() if hasattr(resp, "model_dump") else json.loads(resp.json())),
                }
                print(f"[AI] {_now_iso()} ← OK finish_reason={result['finish_reason']}")
                return result

            except RateLimitError as e:
                # Klasyczny 429
                last_err = e
                print(f"[AI] {_now_iso()} 429 RateLimit (próba {attempt}/{RETRY_MAX_CYCLES}) – śpię {RETRY_SLEEP_SEC}s")
                time.sleep(RETRY_SLEEP_SEC)
                continue
            except APIStatusError as e:  # typ HTTP z kodem statusu
                last_err = e
                status = getattr(e, "status_code", None)
                if status == 429:
                    print(f"[AI] {_now_iso()} 429 APIStatus (próba {attempt}/{RETRY_MAX_CYCLES}) – śpię {RETRY_SLEEP_SEC}s")
                    time.sleep(RETRY_SLEEP_SEC)
                    continue
                print(f"[AI] {_now_iso()} Błąd HTTP {status}: {e}")
                raise
            except OpenAIError as e:
                # Inne błędy biblioteki – nie retry'ujemy w nieskończoność
                last_err = e
                print(f"[AI] {_now_iso()} Błąd OpenAI: {e}")
                raise
            except Exception as e:
                last_err = e
                print(f"[AI] {_now_iso()} Nieoczekiwany błąd: {e}")
                raise

        # Po wyczerpaniu prób 429
        ts = _now_iso()
        agent = caller or "unknown"
        print(f"[AI] {ts} Nieudane po {RETRY_MAX_CYCLES} próbach (429). Agent={agent}")
        return {
            "id": None,
            "model": mdl,
            "created": int(time.time()),
            "finish_reason": "rate_limited",
            "usage": None,
            "text": (
                f"Usługa chwilowo przeciążona (429). Próbowano {RETRY_MAX_CYCLES}× z przerwami "
                f"{RETRY_SLEEP_SEC}s. Agent: {agent}. Czas: {ts}."
            ),
            "error": {
                "type": "rate_limited",
                "agent": agent,
                "at": ts,
                "retries": RETRY_MAX_CYCLES,
                "sleep_seconds": RETRY_SLEEP_SEC,
                "last_error": str(last_err) if last_err else None,
            },
            "raw": None,
        }

    # ---------------- walidacja ---------------
    @staticmethod
    def _validate_message(msg: Dict[str, Any]) -> None:
        role = msg.get("role")
        content = msg.get("content")
        if role not in ("system", "user", "assistant"):
            raise ValueError("Wiadomość musi mieć role ∈ {system,user,assistant}.")
        if not isinstance(content, str) or not content:
            raise ValueError("Pole 'content' musi być niepustym stringiem.")

    def _validate_messages(self, msgs: List[Dict[str, Any]]) -> None:
        if not msgs:
            raise ValueError("Lista 'messages' nie może być pusta.")
        for m in msgs:
            self._validate_message(m)
