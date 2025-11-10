# MAS — Punkt 7 (KB-Agent) komplet wdrożenia

Poniżej zestaw gotowych plików (do wklejenia do repo) oraz łatki do istniejących modułów. Zamyka to punkt **7. KB-Agent — STORE/GET tylko przez Koordynatora; jednolite klucze i wersjonowanie** wraz z „dopięciem śrubek”.

---

## docs/mas_kb_contract.md

````markdown
# MAS.KB — Kontrakt 1.0 (STORE/GET)

**Cel:** Spójna, wersjonowana pamięć rozmów i artefaktów, dostępna wyłącznie przez Koordynatora.

## 1. Klucze i tagi
- **Format klucza (regex):** `^[a-z0-9._-]+:[a-z0-9._-]+:[a-z0-9._-]+:[a-z0-9._-]+:[a-z0-9._-]+$`
- **Konwencja (5 segmentów):**
  - `session:{conv_id}:chat:frame:{ts_ms}` — pojedyńczy wpis historii (frame)
  - `session:{conv_id}:chat:timeline:main` — główna oś czasu sesji
- **Przykładowe tagi:** `conv:{conv_id}`, `kind:frame|timeline`, `from:presenter|registry|specialist|coordinator`

## 2. Operacje
### STORE (append-only)
Wejście (`REQUEST` do KB):
```json
{
  "type": "STORE",
  "key": "session:sess-123:chat:timeline:main",
  "content_type": "application/json",
  "value": [ {"ts":"...","agent":"Presenter","pf":"REQUEST","type":"USER_MSG","text":"..."} ],
  "tags": ["conv:sess-123","kind:timeline"],
  "if_match": "v8"  // opcjonalnie: "vN" lub etag
}
````

Odpowiedź (`INFORM`):

```json
{ "type": "STORED", "key": "...", "version": 9, "etag": "uuid", "stored_at": "ISO-8601" }
```

Błędy (`FAILURE`): `FAILURE.CONFLICT`, `FAILURE.INVALID_KEY`, `FAILURE.EXCEPTION`.

### GET

Wejście (`REQUEST` do KB):

```json
{ "type": "GET", "key": "session:sess-123:chat:timeline:main", "version": 9 | null, "as_of": "ISO-8601" | null }
```

Odpowiedź (`INFORM`):

```json
{ "type": "VALUE", "key": "...", "version": 9, "etag": "uuid", "content_type": "application/json", "value": [...], "stored_at": "ISO-8601" }
```

Błędy (`FAILURE`): `FAILURE.NOT_FOUND`, `FAILURE.INVALID_KEY`, `FAILURE.EXCEPTION`.

## 3. Zasady wersjonowania i RMW

* **Append-only:** Każdy STORE tworzy nową `version` + nowy `etag`.
* **RMW (read-modify-write):** Aktualizacja `timeline:main` **musi** używać `if_match=vN`. W konflikcie: `GET` → ponowny `STORE` z nowym `if_match`.

## 4. Autoryzacja i dostęp

* Dostęp do KB wyłącznie przez Koordynatora (JID twardo wpisany w konfiguracji KB i w ACL ejabberd).
* KB odrzuca (`REFUSE.UNAUTHORIZED`) żądania od innych JID-ów.

## 5. Telemetria

* Liczniki: `kb_store_ok`, `kb_store_conflict`, `kb_store_fail`, `kb_get_ok`, `kb_get_not_found`, `kb_get_fail`.
* Czas obsługi: histogramy p50/p95/p99.

## 6. Retencja

* `timeline:main` przechowuje n ostatnich wpisów (np. 100). Archiwizacja starszych do `timeline:archive` jest opcjonalna.

## 7. Przykłady

* **Frame USER_MSG:** `session:{id}:chat:frame:{ts_ms}` z `value` = pojedynczy obiekt wpisu.
* **Timeline RMW:** `GET(version=vN)` → dopisz element → `STORE(if_match=vN)`.

````

---

## ejabberd/acl_kb_only_coordinator.yml (snippet)
```yaml
### Włącz w głównym ejabberd.yml (lub dołącz jako plik konfiguracyjny)
acl:
  coordinator:
    user:
      - "coordinator@xmpp.pawelhaladyj.pl"
  kb_user:
    user:
      - "kb@xmpp.pawelhaladyj.pl"

access_rules:
  kb_only_from_coord:
    allow: coordinator

modules:
  mod_filter:
    rules:
      - name: restrict_kb_incoming
        what: message
        to:
          acl: kb_user
        from:
          access: kb_only_from_coord
        action: allow
      - name: restrict_kb_incoming_deny_others
        what: message
        to:
          acl: kb_user
        action: deny
````

> Uwaga: `mod_filter` musi być włączony. Snippet ogranicza *dowolne wiadomości* do `kb@…` tylko z `coordinator@…`.
