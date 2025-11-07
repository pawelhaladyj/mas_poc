MAS – Kontrakt FIPA-ACL (POC)

Cel: Ujednolicenie formatu wiadomości w systemie agentowym (XMPP/ejabberd). Prosto, stabilnie, „po staremu”.
Zasada nadrzędna: cały ruch operacyjny przez Coordinatora; wybór wykonawcy na podstawie Registry (DF); dostęp do KB wyłącznie przez Coordinatora.

1. Koperta (nagłówki – obowiązkowe)

Każda wiadomość to JSON o stałym szkielecie:

{
  "performative": "REQUEST|INFORM|AGREE|REFUSE|FAILURE|QUERY-REF",
  "sender": "LogicalName",          // np. "Presenter"
  "receiver": "LogicalName",        // np. "Coordinator"
  "conversation_id": "sess-abc123", // = XMPP thread = session_id
  "reply_with": "msg-uuid",         // id tej wiadomości (nadawca ustala)
  "in_reply_to": "msg-uuid",        // id wiadomości, na którą odpowiadamy
  "ontology": "MAS.Core",
  "protocol": "fipa-request",       // albo "fipa-query" dla zapytań
  "language": "application/json",
  "timestamp": "2025-11-07T19:55:00Z",
  "content": { }
}


Wymogi techniczne:

conversation_id nadawany u Presentera i niezmienny w całym torze.

Korelacja: odpowiedź musi mieć in_reply_to równy reply_with z wiadomości źródłowej.

Limit rozmiaru content: ≤ 64 kB (POC).

Kodowanie UTF-8, czas w UTC (ISO-8601).

2. Typy treści (content) – minimum POC
2.1. Wejście człowieka

REQUEST.USER_MSG (Presenter → Coordinator)

{"type":"USER_MSG","text":"…","attachments":[]}

2.2. Rejestr (DF)

REQUEST.REGISTER (Specialist → Registry)

{"type":"REGISTER","profile":{
  "jid":"specialist@xmpp…","name":"Specialist","version":"1.0.0",
  "capabilities":["ASK_EXPERT"],"description":"…"
}}


INFORM.HEARTBEAT (Specialist → Registry)

{"type":"HEARTBEAT","jid":"specialist@xmpp…"}


QUERY-REF.need (Coordinator → Registry)

{"need":"ASK_EXPERT"}


INFORM.candidates (Registry → Coordinator)

{"candidates":["specialist@xmpp…"]}

2.3. Zlecenie do Specjalisty

REQUEST.ASK_EXPERT (Coordinator → Specialist)

{
  "type":"ASK_EXPERT",
  "args":{"question":"…"},
  "context_ref":{"session_id":"sess-abc123"}
}


AGREE / REFUSE (Specialist → Coordinator)

{"status":"accepted"}             // albo {"reason":"busy"}


INFORM.RESULT (Specialist → Coordinator)

{"type":"RESULT","result":{"answer":"…","meta":{}}}


FAILURE (Specialist → Coordinator)

{"reason":"TIMEOUT|INVALID_ARGS|INTERNAL_ERROR"}

2.4. KB (zawsze przez Coordinatora)

INFORM.STORE_FACT (Coordinator → KB)

{"type":"STORE_FACT","fact":{
  "session_id":"sess-abc123",
  "slot":"answer",
  "value":"…",
  "confidence":0.9,
  "ts":"2025-11-07T19:55:00Z"
}}


QUERY-REF.GET_FACTS (Coordinator → KB)

{"type":"GET_FACTS","filter":{"session_id":"sess-abc123","slot":"answer"}}


INFORM.FACTS (KB → Coordinator)

{"facts":[{"slot":"answer","value":"…","confidence":0.9,"ts":"…"}]}

2.5. Odpowiedź do człowieka

INFORM.PRESENTER_REPLY (Coordinator → Presenter)

{"type":"PRESENTER_REPLY","text":"…","rich":{}}

3. Stany i czasy (koordynacja)

FSM Coordinatora:
RECEIVED → LOOKUP(DF) → DISPATCHED(REQUEST) → IN_PROGRESS(AGREE) → DONE(INFORM.RESULT → STORE_FACT → PRESENTER_REPLY)
W razie kłopotów: REFUSE/timeout → RETRY_NEXT (kolejny kandydat) lub FAILED.

Timeouts (POC): AGREE 3 s, RESULT 30 s.

Retry: do wyczerpania listy DF; potem FAILURE.NO_CANDIDATE do Presentera.

4. Błędy (FAILURE.reason – słownik POC)

NO_CANDIDATE, SPECIALIST_TIMEOUT, INVALID_ARGS, INTERNAL_ERROR, KB_UNAVAILABLE, KB_STORE_FAILED.

5. Zasady porządku i bezpieczeństwa

Brak skrótów: Specialist nie rozmawia z Presenterem ani KB; KB rozmawia tylko z Coordinatorem.

DF nie zleca prac – wyłącznie rejestr (REGISTER/HEARTBEAT) i wyszukiwanie (QUERY-REF need).

Spójność sesji: conversation_id = session_id = XMPP thread.

Logowanie (telemetria): nagłówki koperty, decyzje Coordinatora, czasy, powody retry/FAILURE.

6. Definition of Done (Krok 2)

Plik docs/acl_contract.md (niniejszy) w repo – wersja 1.0.

Agenci w POC rozumieją co najmniej:
REQUEST.USER_MSG, REQUEST.ASK_EXPERT, INFORM.RESULT,
REQUEST.REGISTER, INFORM.HEARTBEAT, QUERY-REF.need/INFORM.candidates,
INFORM.STORE_FACT, QUERY-REF.GET_FACTS/INFORM.FACTS,
INFORM.PRESENTER_REPLY, FAILURE.*.

Przykładowy ślad przepływu (od USER_MSG do PRESENTER_REPLY) z poprawną korelacją reply_with/in_reply_to i jednym conversation_id.