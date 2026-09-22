# Entwicklungstracking — unifi-mcp

Stand: 2026-09-22 (WP-0 … WP-14 done)
Quelldokument: [unifi-mcp-kubernetes-design.md](unifi-mcp-kubernetes-design.md)

## Scope

Dieses Repo: **MCP-Server-App + Container-Image + CI**
Image: `ghcr.io/FabianLiske/unifi-mcp`, Build & Push via GitHub Actions (Push auf Tags + master).

Nicht in diesem Repo (→ Deploy-Repo):

- Kubernetes-Manifeste (Namespace, Deployment, Service, NetworkPolicy, Secrets)
- LiteLLM `mcp_servers`-Konfiguration und Key-Permissions
- Cluster-Rollout und Cluster-Smoke-Tests

Server-seitig bleibt LiteLLM-Kompatibilität Teil dieses Repos: stateless Streamable HTTP unter `/mcp`, Bearer-Auth, `/healthz`, `/readyz`.

## Statuslegende

- `open` — nicht begonnen
- `in_progress` — in Arbeit
- `done` — fertig (Abnahmekriterien erfüllt)
- `blocked` — blockiert (Grund beim jeweiligen WP)

## Statusübersicht

| WP    | Titel                                  | Phase    | Status      | Deps      |
| ----- | -------------------------------------- | -------- | ----------- | --------- |
| WP-0  | Repo-Setup (Remote, Push)              | 0        | done        | —         |
| WP-1  | Scaffolding & Toolchain                | 0        | done        | —         |
| WP-2  | UniFi-API-Discovery                    | 0        | done        | —         |
| WP-3  | Config & Observability                 | 1        | done        | WP-1      |
| WP-4  | UniFi-Client-Kern                      | 1        | done        | WP-2, WP-3|
| WP-5  | Normalisierung, Redaction, Limits    | 1        | done        | WP-4      |
| WP-6  | MCP-Server-Gerüst + Auth               | 1        | done        | WP-3–5    |
| WP-7  | Tool-Framework + System/Sites/Devices  | 2        | done        | WP-6      |
| WP-8  | Clients + inspect_client_path          | 2        | done        | WP-7      |
| WP-9  | Networks / WiFi / Firewall             | 2        | done        | WP-7      |
| WP-10 | ACL / Traffic / Reference              | 2        | done        | WP-7      |
| WP-11 | Dockerfile + GH-Actions (ghcr)         | 3        | done        | WP-6      |
| WP-12 | Tests, Smoke, README, DoD              | 3        | done        | WP-7–11   |
| WP-13 | ZBF Policies (Read)                    | 3        | done        | WP-12     |
| WP-13b| Read-Tools: Device-Detail, DNS, ACL-Ordering, Pending | 3 | done | WP-13 |
| WP-14 | Safe-Writes-Fundament                  | post-MVP | done        | WP-13     |
| WP-15 | Erste Write-Tools                      | post-MVP | open        | WP-14     |
| WP-16 | Restliche Write-Tools                  | post-MVP | open        | WP-15     |
| WP-17 | Actions                                | post-MVP | open        | WP-16     |
| WP-18 | Delete + Deep Diagnostics (optional)   | post-MVP | open        | WP-17     |

Parallelisierbar: WP-1 ∥ WP-2 · WP-8 ∥ WP-9 ∥ WP-10 · WP-11 ab WP-6.

## Phase 0 — Fundament

### WP-0 — Repo-Setup

Remote `origin` auf `git@github.com:FabianLiske/unifi-mcp.git` setzen, Design-Doc + dieses Tracking-Doc pushen.

- [x] remote gesetzt
- [x] initialer Push

Status: `done`

### WP-1 — Scaffolding & Toolchain

uv-Projekt (Python ≥ 3.12), `src/unifi_mcp/`-Layout laut Design-Doc §5 (ohne `deploy/kubernetes/`), ruff (Lint + Format), Typprüfung, pytest + pytest-asyncio + respx, `.gitignore`, `.dockerignore`, GitHub-Actions-Workflow: lint + typecheck + tests.

Abnahme: `uv sync`, `uv run ruff check`, `uv run pytest` grün; CI grün.

- [x] uv-Projekt (Python 3.12), `src/unifi_mcp/`-Layout (Pakete `auth/ unifi/ tools/ safety/ observability/`)
- [x] `pyproject.toml` (hatchling, ruff Lint+Format, mypy strict, pytest-asyncio), `uv.lock`
- [x] `.gitignore`, `.dockerignore`, `.python-version`
- [x] `.github/workflows/ci.yaml` (uv sync → ruff check → ruff format --check → mypy → pytest)
- [x] Smoke-Test `tests/unit/test_smoke.py`
- [x] lokal: `uv sync` + ruff check + ruff format --check + mypy + pytest grün

Status: `done` (CI-Run in GitHub nach Push verifizieren)

### WP-2 — UniFi-API-Discovery

Gegen das lokale Cloud Gateway: Network-Version + passende API-Doku/OpenAPI erfassen, Base URL, Auth-Schema und Endpunkte bestätigen (u. a. Application Info, Sites, Devices, Clients, Networks, WiFi, Firewall, ACL, Traffic Matching Lists), read-only API-Key verwenden, TLS-Handshake + Gateway-Cert verifizieren (optional für `strict`-Mode: Cert extrahieren → `certs/gateway.crt`, gitignored).

Deliverables: `docs/unifi-api-notes.md` (bestätigte Endpunkte, Beispielantworten, Secrets redigiert) + (optional) `certs/gateway.crt`.

- [x] Base URL + Auth (`X-API-Key`) verifiziert
- [x] Envelope/Pagination (default limit 25, max 200) verifiziert
- [x] Filter-DSL (funktionsbasiert: `field.fn(...)`, `and/or/not`, `like`) live verifiziert
- [x] Enum-Werte sind UPPERCASE (`ONLINE`, `WIRED`, `WIRELESS`) — Normalisierung notiert
- [x] Endpoint-Mapping + Referenz-Resources verifiziert
- [x] ⚠️ `firewall/zones` + `firewall/policies` → 400 `not-configured` → `unsupported`
- [x] ⚠️ `wifi/broadcasts/{id}` enthält Klartext-PSK → Redaction notiert
- [x] ⚠️ Versionsdrift 10.4.57 (Referenz) vs. 10.6.101 (installiert) dokumentiert
- [x] Referenz-OpenAPI → `docs/reference/network-openapi-10.4.57.json`
- [x] Gateway-Cert extrahiert → `certs/gateway.crt` (gitignored)
- [x] `docs/unifi-api-notes.md` geschrieben

Status: `done`

## Phase 1 — Kern

### WP-3 — Config & Observability

`config.py`: alle Env-Vars aus Design-Doc §6 (Validierung, Defaults, Feature Flags `ENABLE_WRITE_TOOLS` / `ENABLE_DELETE_TOOLS` / `ENABLE_ACTION_TOOLS` = false), TLS-Settings: `UNIFI_TLS_MODE=strict|extract-once|insecure` (Default `extract-once`) + `UNIFI_CA_BUNDLE` (nur für `strict`); `UNIFI_VERIFY_TLS` aus dem Design-Doc wird durch `UNIFI_TLS_MODE` ersetzt (Abweichung von §6).
Lade-Logik: `pydantic-settings` — echte Umgebungsvariablen überschreiben Werte aus `.env`. Lokale Entwicklung über `.env` (gitignored, Vorlage `.env.example`); Produktion (Compose/K8s) liefert dieselben Namen via `env_file:` bzw. ConfigMap+Secret.
`observability/logging.py`: JSON-Logs mit `request_id`, keine Secrets.
`observability/metrics.py`: prometheus-client-Skeleton.

Abnahme: Unit-Tests für Parsing, Validierung, Flags.

- [x] `config.py`: `Settings` (pydantic-settings), alle Env-Vars, Validierung, Defaults; `get_settings()` mit Cache
- [x] TLS: `unifi_tls_mode` (strict/extract-once/insecure, Default `extract-once`) + `unifi_ca_bundle` (Pflicht bei `strict`)
- [x] Feature-Flags `enable_write/action/delete_tools` = `false`
- [x] `SecretStr` für API-Key + MCP-Token (nie in Logs/Reprs)
- [x] `observability/logging.py`: structlog JSON/text, `request_id` (ContextVar), Secret-Redaction
- [x] `observability/metrics.py`: prometheus-Skeleton (6 Metriken, begrenzte Labels)
- [x] Unit-Tests: config (12), logging (6), metrics (5); `tests/conftest.py` (`clean_env`)
- [x] `MCP_BIND_HOST`-Default `0.0.0.0` (Design-Doc) + `.env.example` angeglichen
- [x] lokal: ruff + mypy strict + pytest (23 Tests) grün

Status: `done`

### WP-4 — UniFi-Client-Kern

`unifi/client.py`: genau ein `httpx.AsyncClient`, Pooling, explizite Timeouts, Retries nur bei transienten Fehlern, 429-Handling, Response-Size-Cap, niemals Secrets in Logs.
`unifi/errors.py`: `UniFiError` + Authentication / Authorization / NotFound / Conflict / RateLimit / Validation / Unavailable.
`unifi/capabilities.py`: Capability-Check beim Start (fehlende optionale Kategorien → Server startet trotzdem).
TLS-Bootstrap: `extract-once` — einmaliger unverifizierter Handshake (stdlib `ssl`), Peer-Cert ziehen (`getpeercert(binary_form=True)` → PEM), als CA-Bundle für die Session verwenden, SHA-256-Fingerprint prominent loggen; `strict` — Verifikation gegen `UNIFI_CA_BUNDLE`; `insecure` — Verifikation aus, nur mit prominentem Warnlog.

Abnahme: Client-Unit-Tests §35 (Auth-Header, Timeout, 401/403/404/429/5xx, Retry nur transient) + Unit-Tests für alle drei TLS-Modi (lokaler Test-Server mit selbstsigniertem Cert).

- [x] `unifi/errors.py`: `UniFiError(status_code, api_code, message)` + 7 Subklassen (401/403/404/409/429+retry_after/400/5xx+Conn/Timeout) + `UniFiResponseTooLargeError`
- [x] `unifi/models.py`: `Page` (Envelope, `totalCount`-Alias) + `ApplicationInfo`
- [x] `unifi/tls.py` (neue Datei, bewusste Abweichung von §5): `bootstrap_extract_once()` (stdlib-ssl via `asyncio.to_thread`, SHA-256-Fingerprint) + `build_verify()` für alle drei Modi
- [x] `unifi/client.py`: `UniFiClient.create(settings)` (extract-once-Bootstrap vor Client-Erstellung, Fingerprint prominent als WARNING), genau ein wiederverwendeter `AsyncClient` (Base-Path `/proxy/network/integration/v1`, `X-API-Key`, explizite Timeouts, `httpx.Limits`-Pooling)
- [x] Retry nur transient (5xx, Connect-/Timeout-Fehler, 429 mit `Retry-After`, Backoff 0.5·2ⁿ cap 8s, Backoff-Basis injizierbar); 400/401/403/404/409 sofort gemappt ohne Retry
- [x] Error-Mapping inkl. UniFi-Error-Code (`code` aus Envelope); API-Key/Authorization nie in Logs (getestet)
- [x] Response-Size-Cap: Content-Length-Prüfung + Streaming-Count, Cap = `MAX_TOOL_RESPONSE_BYTES`, keine Retries
- [x] Metriken `record_unifi_request` pro Request (bounded labels), Debug-Logs ohne Headers
- [x] `unifi/capabilities.py`: `detect_capabilities()` — Kern `/info` hart (Fehler → Raise), optional: sites/devices/clients/networks/wifi/firewall/acl/traffic_matching_lists per `?limit=1`-Probe; `ok|not_configured|unavailable|error`; fehlende `UNIFI_SITE_ID` → erste Site aus `/sites`
- [x] Client-Unit-Tests (respx): Auth-Header, Base-Path, Timeouts, 400/401/403/404/409, 429 (Retry+`Retry-After`), 5xx-Retries, ConnectError/ReadTimeout-Retries, „nicht transient wird nicht geretryt“, Size-Cap, malformed JSON, keine Secrets in Logs
- [x] TLS-Tests gegen lokalen asyncio-TLS-Server mit selbstsigniertem Cert (nur DNS-SANs, wie reales Gateway; `cryptography` als neue dev-Dependency): extract-once (Pinning + Fingerprint-Log + verifizierte Session), strict (richtiges CA ok / falsches CA fail), insecure (ok + Warning), `http://` ohne TLS, Bootstrap-Fehler → klares `UnavailableError`
- [x] Capability-Tests (respx): alle ok, firewall `not-configured`, 404 → `unavailable`, Auth-Fehler → `unavailable`, Site-Auswahl, Sites-Fehlschlag → Kaskade, Kern-Fehler → Raise
- [x] Live-Verifikation gegen echtes Gateway: extract-once-Fingerprint `db27…45` stimmt mit `openssl x509 -fingerprint -sha256` überein; `/info` 10.6.101; alle Kategorien `ok`, firewall korrekt `not_configured`
- [x] Abweichung: TLS mit `check_hostname=False` (live belegt: Gateway-Cert hat keine SAN für LAN-IP `172.26.1.1`; Chain-Verifikation/Cert-Pinning bleibt wirksam; einmaliges WARNING-Log)
- [x] Abweichung: `UNIFI_SITE_ID=` (leer) wird zu `None` normalisiert (sonst bricht site-scoped Probing mit `/sites//…` ab) — `config.py` + Test
- [x] Abweichung: korrekter ACL-Pfad ist `/sites/{site}/acl-rules` (nicht `acl/rules` → 404); `docs/unifi-api-notes.md` korrigiert
- [x] lokal: ruff + mypy strict + pytest (61 Tests) grün

Status: `done`

### WP-5 — Normalisierung, Redaction, Limits

`unifi/normalization.py`: Summary/Detail-Ebenen, interne Felder entfernen.
`safety/redaction.py`: case-insensitive Feldliste §8 (password, passphrase, psk, secret, token, apiKey, private_key, credential, authorization, …); WiFi-PSKs nie ausgeben.
Pagination-Helper: default 50, max 200, Antwortformat `{items, count, total_count, next_offset}`.
Hartes Response-Size-Limit (`response_too_large`-Fehler), strukturierte LLM-Fehler (`not_found`, `ambiguous_match`, §30).

Abnahme: Redaction-, Pagination- und Size-Tests.

- [x] `safety/redaction.py`: `redact()` rekursiv (Dicts+Listen), Key-Normalisierung (lowercase, `_`/`-` gestrippt → `apiKey`/`api_key`/`API-Key` gleich), Exact-Liste §8 + `preSharedKey`; Suffix-Regel für vorangestellte Varianten (`wpaPsk`, `x_passphrase`, `clientSecret`, `accessToken`); Wert → `[REDACTED]`, Key bleibt sichtbar; Input wird nicht mutiert
- [x] `unifi/normalization.py`: `INTERNAL_FIELDS` = {metadata, etag, revision} rekursiv entfernen; `normalize(level="detail"|"summary")` — detail: intern+Secrets, sonst komplett; summary: zusätzlich Depth-Pruning (Container tiefer als `max_depth`=1 → `…`, Skalare/Scalar-Listen bleiben); Redaction ohne Opt-out
- [x] Pagination: `clamp_limit()` (None/≤0 → 50, >max → 200), `page_to_mcp()` → `{items, count, total_count, next_offset}` (`next_offset` = Offset der nächsten Seite, `None` auf letzter Seite)
- [x] `tools/errors.py`: strukturierte LLM-Fehler `ToolError`/`NotFoundError`/`AmbiguousMatchError`/`ResponseTooLargeError` mit `to_dict()` (Code zuerst, Message zuletzt, §30) + `check_response_size()` (kompaktes JSON, UTF-8-Bytes, > Limit → `response_too_large`)
- [x] Exports: `safety`/`unifi`/`tools` `__init__.py`
- [x] Unit-Tests: Redaction (case-insensitive-Matrix, verschachtelt, echter WiFi-Pfad, keine Mutation), Normalisierung (intern entfernen, detail/summary, Pruning, keine Mutation), Pagination (Default/Max-Cap, next_offset, Item-Normalisierung), Size (Bytes, UTF-8, Grenze, Raise)
- [x] Live-Verifikation: echtes WiFi-Broadcast-Detail → `securityConfiguration.passphrase` wird `[REDACTED]`, roher PSK taucht im normalisierten Output **nicht** auf
- [x] lokal: ruff + mypy strict + pytest (98 Tests) grün

Status: `done`

### WP-6 — MCP-Server-Gerüst + Auth

`app.py`: ASGI, `/mcp` (stateless Streamable HTTP), `/healthz` (nur Prozess), `/readyz` (Config + Client + Application-Info-Check mit 30 s-Cache).
`auth/middleware.py`: Bearer-Token, 401/403, Auth-Failure-Metrik.
`server.py`: MCP-Instanz, Tool-Registrierung gesteuert durch Feature Flags (aus → nicht in `tools/list`), Server Instructions (§32).

Abnahme: Contract-Test — initialize, `tools/list`, 401 ohne Token, keine Write-Tools sichtbar.

- [x] **Entdeckung:** Installiertes `mcp` ist **2.x** (nicht 1.x) — API geändert: `FastMCP`→`MCPServer` (`mcp.server.mcpserver`), `list_tools()`/`initialize()` sind Coroutines, `InitializeResult` nutzt snake_case (`server_info`), `streamable_http_app(stateless_http=True)`, Custom-Routes via `@server.custom_route(...)` (per SDK-Doku auth-exempt → passt für Health)
- [x] `server.py`: `MCPServer` (name/title/description/`instructions` §32/version), `ReadinessProbe` (30 s-Cache, injektierbare Clock), `/healthz`+`/readyz` als Custom-Routes, `mcp_starlette()` = stateless Streamable HTTP unter `/mcp`
- [x] `auth/middleware.py`: reines ASGI-Middleware, konstant-zeitiger `hmac.compare_digest`, 401 (fehlend) / 403 (falsch), `record_auth_failure()`-Metrik, request-id pro Request, `/mcp` geschützt, Health-Endpoints bewusst öffentlich (K8s-Probes, keine Secrets — dokumentiert)
- [x] `tools/registry.py`: `ToolGroup(name, gate, register)` + `register_tools()` — Gating auf **Registrierungszeit** (aus → fehlt in `tools/list`), `TOOL_GROUPS` im WP-6-Gerüst leer (ab WP-7 gefüllt)
- [x] `app.py`: `create_app(settings, client)` (Client wird vom Caller erzeugt/geschlossen → testbar ohne TLS), `main()`-Runner (Config → Client → uvicorn → aclose)
- [x] Contract-Test (in-process ASGI + MCP-Client): initialize (`server_info.name=unifi-mcp`, instructions), `tools/list` (leer, keine Write-Tools), 401 ohne Token, 403 falsches Token, `/healthz`+`/readyz` öffentlich; eigenes `asgi_lifespan`-Helper (ASGITransport läuft keinen lifespan → Session-Manager startet sonst nie; App läuft in einer Task, damit der interne Task-Group nicht Setup/Teardown-Tasks spannt)
- [x] **Live-Smoke** (echtes Gateway + uvicorn): healthz 200, readyz 200 (`applicationVersion=10.6.101`), 401/403 korrekt, MCP-Handshake OK, 0 Tools (erwartet im Gerüst)
- [x] lokal: ruff + mypy strict (21 Dateien) + pytest (116 Tests) grün

Status: `done`

## Phase 2 — Read-only Tools

### WP-7 — Tool-Framework + System/Sites/Devices

Gemeinsame Tool-Helper (Pagination, Limits, Error-Mapping), Tool-Description-Konvention (§31).
Tools: `get_system_info`, `list_sites`, `list_devices` (Filter: site_id, device_type, state, search, limit, offset).

Abnahme: E2E gegen Fake-API; `get_system_info` liefert Version + Capabilities.

- [x] `tools/common.py` (Framework): `wrap_tool` (Timing + `record_mcp_request` §28, Response-Size-Cap §12, Fehler-Mapping), `unifi_error_to_dict` (404→`not_found`, 401→`authentication`, 403→`authorization`, 400→`validation` / `not-configured`→`unsupported`, 429→`rate_limited`(+`retry_after`), 409→`conflict`, 5xx/Conn→`unavailable`, sonst `unifi_error`; unerwartete Exceptions → `internal_error`, Traceback nur server-seitig), `resolve_site` (Tool-Param → `UNIFI_SITE_ID` → erste Site aus `/sites` → sonst `not_found`), `site_overview`, `fetch_list` (Envelope → §12-MCP-Format, Summary-Normalisierung)
- [x] Fehler-Konvention wie WP-5: Tool liefert Success- und Fehler-Paths beide als `dict` (structured output); `InvalidValueError` (code `validation`) für Allowlist-Verstöße neu in `tools/errors.py`
- [x] `tools/system.py`: `get_system_info` → `{application_version, mcp_version, active_site{id,name}, capabilities}` (keine Secrets); Capabilities via neuem `client.capability_cache` (TTL 300 s, §29) — `detect_capabilities` war bis hierhin nie verdrahtet; Start-Detection in `app.main()` best-effort (Fehler → Warnlog, Server startet trotzdem, §3)
- [x] `tools/sites.py`: `list_sites` → §12-Envelope, Items `{id, name}` (§10.2)
- [x] `tools/devices.py`: `list_devices` (site_id, device_type, state, search, limit, offset); Filter-Builder mit Allowlists — `device_type`: switch/ap/access_point/gateway → `features.contains('switching'|'accessPoint'|'gateway')`, `state`: online/offline/pending → UPPERCASE `state.eq(...)`, `search` → `name.like('*…*')` (Quotes gestrippt, DSL hat kein Escaping), Komposition via `and(...)`; kein freier Filter-Passthrough
- [x] **Live-Verifikation (WP-7):** `features.contains('switching')` OK (6/6), `state.eq('ONLINE')` OK, `and(...)`-Kombis OK; `state.eq` akzeptiert auch unbekannte Enums (0/0, kein 400) → `pending→PENDING` sicher; ⚠️ UCG Ultra meldet nur `features=["switching"]` (kein `gateway`) → in Tool-Description dokumentiert; ⚠️ Schema-Drift List vs. Detail: `features` ist Dict im Detail / Liste in der Liste, `interfaces` String-Liste (`["ports"]`/`["radios"]`) im List-Endpoint / Objekte im Detail → notiert, List-Fixture auf List-Form geschnitten
- [x] Tool-Descriptions nach §31 + `title` + `ToolAnnotations(read_only_hint=True)`; Registration über `ToolGroup`s in `TOOL_GROUPS` (Gating bei Registrierung bleibt WP-6-Mechanik)
- [x] Unit-Tests: Error-Mapping (alle Codes + api_code/retry_after), wrap_tool (Metrics, ToolError, UniFiError, Crash→internal_error ohne Leak, Size-Cap, Schema bleibt via `functools.wraps`), resolve_site/site_overview (Precedence, Fallbacks), Filter-Builder (Alias-Matrix, Kombis, Invalid-Werte), clamp_params
- [x] E2E (Abnahme) gegen Fake-API (Fixtures `application_info/sites/devices.json` aus Live-Daten, redigiert) über echte `/mcp`-Streamable-HTTP-Session: tools/list exakt die 3 Tools + read-only-hints; `get_system_info` (Version 10.6.101 + Capabilities inkl. `firewall: not_configured`); `list_sites` (Envelope + Projektion); `list_devices` (Summary-Pruning `interfaces→["…"]`, Default limit 50/offset 0, Filter-Expression im Request verifiziert, Limit-Cap 200, `validation`-Fehler, `not_found` bei unbekannter Site, `unavailable` bei 503, `response_too_large` bei kleinem Cap)
- [x] Contract-Test: `tools/list` → exakt `{get_system_info, list_sites, list_devices}` + read-only-hints
- [x] Abweichung: `mcp-types` (2.2.0) als direkte Dependency ergänzt (canonical Import für `ToolAnnotations`; war nur Transitiv-Dep), `mcp`-Lower-Bound auf `>=2.2.0` erhöht (Code nutzt 2.x-API)
- [x] Lokal: ruff + mypy strict (25 Dateien) + pytest (156 Tests) grün

Status: `done`

### WP-8 — Clients + inspect_client_path

`list_clients` (Filter: site_id, network_id, connected_to_device_id, search, limit), `get_client` (exakt einer von client_id / mac / ip / hostname, sonst `ambiguous_match`).
`inspect_client_path`: Client + Network/VLAN + Attachment (Device/Port) + Beobachtungen — nur Fakten aggregieren, keine spekulative Root-Cause-Analyse (§11).

Abnahme: Fixture-Test zum Szenario „Finde wk-5 und zeig mir, worüber er verbunden ist" + Ambiguitätsfall.

- [x] `tools/clients.py`: `list_clients`, `get_client`, `inspect_client_path` (alle read-only, §31-Descriptions, `wrap_tool`)
- [x] **Live-Verifikation (WP-8):** Client-List-/Detail-Objekt **identisch** geformt: `id, name, type (WIRED|WIRELESS), ipAddress, macAddress, uplinkDeviceId, connectedAt, access` — **kein `hostname`-Feld (→ `name`), kein `networkId`, kein Port-/SSID-Feld**. Filter verifiziert: `type.eq`, `ipAddress.eq`, `macAddress.eq`, `or(...)` akzeptiert (21 Treffer), `id.eq(UUID ohne Quotes)`, `connectedAt.gt`; **nicht** filterbar: `name.like`, `networkId.eq`, `uplinkDeviceId.eq`, `ipAddress.like` (400). `uplinkDeviceId` ist bei wired **und** wireless das Attachment-Feld.
- [x] **Abweichung (API-Limit, dokumentiert in DESCRIPTIONs):** `network_id`-Filter → client-seitiges IPv4-Subnet-Matching (Network-Detail → CIDR-Containment; 404 → `not_found`); `connected_to_device_id` → client-seitiges `uplinkDeviceId`-Matching; `search` → serverseitig bei exaktem IPv4/MAC (`ipAddress.eq`/`macAddress.eq`), sonst case-insensitive Substring über name/IP/MAC (max. 200 Clients). `get_client(hostname=...)` matcht `name` exakt (case-insensitive).
- [x] `get_client`: exakt-einer-Identifier-Regel (0/2/3 → `validation`), 0 Treffer → `not_found`, >1 → `ambiguous_match` mit kompakten Matches (`id/name/ipAddress/macAddress`)
- [x] `inspect_client_path`: `{client, network, attachment{device, port}, observations}`; live **kein Client→Network-Mapping** (kein `networkId`, `/networks/{id}/clients` existiert nicht, `/networks/{id}/references` → 500) → `network` nur bei vorhandenem `networkId`, sonst null + `no_network_assignment`; `attachment.port` ist immer null + Observation (`port_not_reported_by_api`) — weder Client- noch Device-Objekt tragen Port-/SSID-Info (OpenAPI 10.4.57 bestätigt); observations rein faktenbasiert
- [x] Abnahme (E2E): Szenario `inspect_client_path(hostname="wk-5")` → Client + Network „Clients" (vlan 40) + AP-Attachment + wireless-Observations; Ambiguitätsfall (2× hostname „printer" → `ambiguous_match`, 2 matches); Filter-Expression im Request verifiziert; limit-Clamp 200; `not_found` (Site + Network); `validation` (2 Identifier)
- [x] Fixtures: `clients.json` (5 Clients inkl. wk-5 + 2× „printer"), `client_path_networks.json`
- [x] Lokal: 48 Tests (39 unit + 9 e2e), ruff + mypy strict grün

Status: `done`

### WP-9 — Networks / WiFi / Firewall

`list_networks`, `get_network`, `list_wifi`, `get_wifi` (nie PSK), `list_firewall_zones`, `get_firewall_zone`.

Abnahme: Fixture-Integrationstests; Redaction auf WiFi-Objekten verifiziert.

- [x] `tools/networks.py` (`list_networks`, `get_network`), `tools/wifi.py` (`list_wifi`, `get_wifi`), `tools/firewall.py` (`list_firewall_zones`, `get_firewall_zone`) — alle read-only, §31-Descriptions, `wrap_tool`
- [x] **Live-Verifikation (WP-9):** Networks: **15** — List-Item `id, name, enabled, vlanId, management (GATEWAY|SWITCH|UNMANAGED), default, metadata` (kein `type`, **kein** `ipv4Configuration` in der Liste); Detail zusätzlich `ipv4Configuration{hostIpAddress, prefixLength, dhcpConfiguration{mode, ipAddressRange, leaseTimeSeconds, ...}}`, `isolationEnabled`, ... . WiFi-Broadcasts: **2** — `id, name` (= SSID), `enabled, type (STANDARD|IOT_OPTIMIZED), network{type,networkId}, securityConfiguration{type}`; **PSK-Feldpfad exakt: `securityConfiguration.passphrase`** (nur im Detail, WPA2/WPA3_PERSONAL Klartext)
- [x] Redaction: `passphrase` (und `psk`/`*psk`-Suffixe) → `[REDACTED]`; **PSK-Absenz verifiziert**: rohe Fixture-Werte tauchen weder in List- noch Detail-Output auf (JSON-Dump-Assertion), `passphrase == "[REDACTED]"` bei List **und** Detail
- [x] Firewall: 400 `api.firewall.zone-based-firewall-not-configured` → strukturierter Fehler `unsupported` (via `unifi_error_to_dict`), E2E-verifiziert
- [x] Abweichung: Design-§10.5/10.6 listet Ideal-Summary-Felder (subnet, gateway, dhcp_mode, bands …), die das Gateway in der Liste nicht liefert → Tools geben normalisiertes Objekt (summary/detail) aus statt fester Projektion; Normalisierung ist drift-tolerant (Felder optional)
- [x] Abnahme: Fixture-Integrationstests (27 Tests: 10+7+6 unit + 4 e2e), alle 5 Pflicht-Szenarien + tools/list + Default-Site-Resolution
- [x] Fixtures: `networks.json` (LAN/IoT/Guest), `wifi.json` (2 Broadcasts mit rohen PSKs für die Absence-Assertions)
- [x] Lokal: ruff + mypy strict grün

Status: `done`

### WP-10 — ACL / Traffic / Reference

`list_acl_rules` (Filter: enabled, source, destination, action), `get_acl_rule`, `list_traffic_matching_lists`, `get_traffic_matching_list`, `list_reference_resources` (8 Resource-Typen in einem Tool, §10.10).

Abnahme: Fixture-Integrationstests.

- [x] `tools/acl.py` (`list_acl_rules`, `get_acl_rule`), `tools/traffic.py` (`list_traffic_matching_lists`, `get_traffic_matching_list`), `tools/reference.py` (`list_reference_resources`) — alle read-only, §31-Descriptions, `wrap_tool`
- [x] **Live-Verifikation (WP-10):** ACL: **8** Regeln — `{type: "IPV4", id, enabled, name, action, index, sourceFilter, destinationFilter, metadata}`; **Action-Enum ist `ALLOW`/`BLOCK` (kein `DENY`!)**; serverseitig filterbar: `action.eq('ALLOW')` (case-sensitive), `enabled.eq(true/false)` (unquoted), **nested** `sourceFilter.type.eq('NETWORKS')` — Filter-Typen: `NETWORKS`, `IP_ADDRESSES_OR_SUBNETS`, `PORTS`, `MAC_ADDRESSES`. TMLs: **17** — `{type: "IPV4_ADDRESSES"|"PORTS", id, name, items[{type, value}]}`. Referenz: `/countries` 248, `/dpi/applications` 2112, `/dpi/categories` 35, `/radius/profiles` 2, `/wans` 2, `/vpn/servers` 0, `/device-tags` 0. ⚠️ **Pfad-Korrektur: `/vpn/site-to-site` → `/vpn/site-to-site-tunnels`** (api-notes korrigiert)
- [x] `action`-Allowlist: `allow`→`ALLOW`, `deny`→`BLOCK` (`block` als Alias, Pattern wie devices Raw-Werte)
- [x] `source`/`destination`-Semantik: *Art* des Endpoint-Filters (`networks|ip_addresses|ports|mac_addresses` → `sourceFilter.type.eq(...)`), 100 % gateway-seitig filterbar (API speichert nur IDs, keine Namen) — in DESCRIPTION dokumentiert
- [x] `list_reference_resources`: 8 snake_case-Typen — `countries`, `dpi_applications`, `dpi_categories` (top-level, site ignoriert); `wan_interfaces`, `site_to_site_vpn_tunnels`, `vpn_servers`, `radius_profiles`, `device_tags` (site-scoped); invalid type → `validation`
- [x] Abnahme: 60 Tests (49 unit + 11 e2e), alle 5 Pflicht-Szenarien + Aliase/Kombi-Filter/404s/invalid-values
- [x] Fixtures: `acl_rules.json` (2×ALLOW/1×BLOCK, Filter-Typ-Mix, enabled-Mix), `traffic_matching_lists.json` (2 TMLs); Referenz-Data inline in E2E (klein)
- [x] Lokal: ruff + mypy strict grün

Status: `done`

## Phase 3 — Container & MVP-Abschluss

### WP-11 — Dockerfile + GitHub Actions (ghcr)

Dockerfile (Multi-stage, uv-Build-Stage mit gepinntem `uv:0.12.15`, `uv sync --frozen --no-dev --no-editable` gegen `uv.lock`; Runtime `python:3.12-slim`, non-root uid 10001, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONUNBUFFERED=1`, read-only root-FS-kompatibel, Healthcheck via python-urllib gegen `/healthz`, ENTRYPOINT `unifi-mcp` als Console-Script, `src/unifi_mcp/cli.py`).
GH-Actions (`.github/workflows/build-image.yaml`): Build (buildx, `linux/amd64,linux/arm64`, GHA-Cache) + Push auf **Tags** (`vX.Y.Z` → Semver-Tag) und **master** (SHA-Tag) → `ghcr.io/FabianLiske/unifi-mcp`, kein nacktes `latest`.

Abnahme: Build lokal + in CI grün; `docker run` + `/healthz` OK.

Umgesetzt: Dockerfile, Workflow, Console-Script. Builder-Logik + Entry-Point lokal verifiziert (simulierte Build-Dir, `/healthz` 200, `/mcp` 401/200, ruff/mypy/292 Tests grün).
CI grün (Commit `1055fd6`); Image `ghcr.io/fabianliske/unifi-mcp:sha-1055fd6` als Multi-Arch-Index (`linux/amd64` + `linux/arm64`) verifiziert.
Abnahme lokal: `docker buildx build --platform linux/amd64,linux/arm64` grün; `docker run --read-only` + non-root (`unifi`): `/healthz` 200, `/readyz` 503 ohne Gateway (erwartet), `/mcp` 401/200.

Status: `done`

### WP-12 — Tests, Smoke, README, DoD

MCP-Contract-Tests (§37), Fake-UniFi-API-Integrationstests mit Fixtures (§36), Live-Smoke-Test per `docker run` gegen das echte Gateway (Checkliste §38, TLS `extract-once`), README (§46), MVP-DoD-Checkliste (§47).

Abnahme: MVP-DoD vollständig (Cluster-Punkte als „→ Deploy-Repo" markiert).

Umgesetzt: Contract-Tests + Fake-API-E2E existierten aus WP-7..10; Gap-Schließung: `device_detail.json` (Detail-Form mit `features`-Dict/`interfaces`-Objekt) + E2E-Ausbau, `firewall_zones.json` + Firewall-Success-Case (neu `test_firewall_e2e.py`), outputSchema-Validierung, Input-Schema-Validierung → 299 Tests grün. Live-Smoke 10/10 (Gateway 10.6.101, Site „Default", 17 Tools; Firewall am Gateway unkonfiguriert → designed `unsupported`-Result). `README.md` neu (13 Punkte §46, Deutsch). DoD-Audit: alle 24 Client-Endpunkte im OpenAPI-Spec, keine Legacy-Pfade genutzt.

Status: `done`

### WP-13 — ZBF Policies (Read)

Die Zone-Based-Firewall wurde am 2026-09-21 auf dem UCG aktiviert (vorher `not-configured`, nur die Zone-Tools + `unsupported`-Pfad aus WP-9). Damit fehlen noch die Read-Endpunkte für Policies. Alle drei fehlenden GETs implementieren: `list_firewall_policies`, `get_firewall_policy`, `get_firewall_policy_ordering` (Zonenpaar).

Abnahme: Live-Discovery der Policies-Endpunkte (Shape, Filter, Quirks) dokumentiert; Fixture-Unit-Tests + E2E (inkl. `unsupported`-Fallback für Gateways ohne ZBF); Contract-Test 17 → 20 Tools; ruff + mypy + pytest grün; Live-Smoke gegen das UCG.

- [x] **Live-Discovery (WP-13, 10.6.106):** Zonen **13**, Policies **~354** (Count driftet — abgeleitete Policies werden neu berechnet); List-Item-Shape inkl. `action{type, allowReturnTraffic?}`, `source/destination{zoneId, trafficFilter?}`, `ipProtocolScope`, `connectionStateFilter`; **`id` fehlt bei ~13 %** (abgeleitete System-Policies wie `000-0-ALLOW-ESTABLISHED-RELATED` → nicht per Detail abrufbar); Detail zusätzlich `description`; Action-Enum `ALLOW`/`BLOCK`/`REJECT`; Ordering nur mit Zonenpaar (`sourceFirewallZoneId` + `destinationFirewallZoneId`)
- [x] **Filter (live verifiziert):** `name.like('…')`, `metadata.origin.eq('USER_DEFINED'|'SYSTEM_DEFINED')`, `source.zoneId.eq`/`destination.zoneId.eq` — UUIDs **unquoted** (mit Quotes → 400); **nicht** filterbar: `action.*`, `enabled.*` (400 `api.request.invalid-filter`, auch nicht in der OpenAPI-Filtertabelle)
- [x] `tools/firewall.py`: `list_firewall_policies` (Filter `name`, `origin` user|system, `source_zone_id`, `destination_zone_id` — Allowlisten + client-seitige UUID-Validierung, kein Filter-Passthrough), `get_firewall_policy` (404 → `not_found` resource `firewall_policy`), `get_firewall_policy_ordering` (→ `before_system_defined`/`after_system_defined`) — alle read-only, §31-Descriptions (inkl. Hinweis: abgeleitete Policies ohne `id`), `wrap_tool`; `unsupported`-Mapping (400 not-configured) bleibt für Gateways ohne ZBF
- [x] Abweichung: OpenAPI 10.4.57 deklariert `id` im Policy-Object als required, live fehlt er bei abgeleiteten Policies → Normalisierung/Tools tragen fehlendes `id` (Summary-Pruning + Detail unaffected)
- [x] Fixtures: `firewall_policies.json` (4 Policies: 3× user-defined mit `id`, 1× abgeleitet ohne `id`, Traffic-Filter mit Subnet/Port/TML-Referenz)
- [x] Unit-Tests: Registration (5 Firewall-Tools), Filter-Builder-Matrix (inkl. Invalid-Value-Cases), Filter-/Pagination-Forwarding, 404-`not_found`, `unsupported`-Mapping (alle 5 Tools), Success-Cases
- [x] E2E: List (Filter-Expression im Request, `id`-lose Items, Summary-Pruning), Detail, 404, Ordering (Request-Params + `unsupported`), `validation` (invalid Zone-UUID); `fake_unifi`-Default-Stub: Policies wie Zones `not-configured` 400
- [x] Contract-Test: exakt 20 Tools (17 MVP + 3 Policies) + read-only-hints + outputSchemas
- [x] Doku: `docs/unifi-api-notes.md` §5/§5e/§6 (Live-Befunde + Historie), README (Firewall-Tabelle), Design-Doc §10.7
- [x] Lokal: ruff + mypy strict + pytest (332 Tests) grün
- [x] Live-Smoke gegen UCG (10.6.106): alle 5 Firewall-Tools erfolgreich

Status: `done`

### WP-13b — Read-Tools: Device-Detail/-Statistics, DNS-Policies, ACL-Ordering, Pending-Devices

Gap-Analyse aller 41 GET-Endpunkte der OpenAPI-Doku gegen die implementierten Tools (Stand WP-13: 27 via 20 Tools). Live-Spec 10.6.106 vom Gateway gezogen und als Referenz ins Repo geholt (`docs/reference/network-openapi-10.6.106.json`, pfadidentisch zu 10.4.57). Sechs fehlende Read-Tools live discovery + implementiert.

Abnahme: Live-Discovery (Shape, Filter, Quirks) dokumentiert; Unit-/E2E-Tests (inkl. Contract 20 → 26 Tools); Capability-Probes um `dns_policies` + `pending_devices` erweitert; ruff + mypy + pytest grün; Live-Smoke gegen das UCG.

- [x] **Live-Discovery (WP-13b, 10.6.106):** Device-Detail (`+ configurationId`, `adoptedAt`, `provisionedAt`, `interfaces{radios[]|ports[]}`), Device-Statistics (Plattdict, `uptimeSec`, `loadAverage*`, `cpuUtilizationPct`, `memoryUtilizationPct`, `uplink{tx/rxRateBps}`), DNS-Policies (6× `A_RECORD`; Union über `type`, typspezifische Felder `ipv4Address`/`targetDomain`/`ipAddress`), ACL-Ordering (`{orderedAclRuleIds[]}`, **keine** Query-Parameter, Listenposition == `index`), Pending-Devices (**Top-Level ohne siteId**) — Details in `docs/unifi-api-notes.md` §5f
- [x] **DNS-Filter (live verifiziert):** `type.eq`, `domain.like`, `enabled.eq` (+ kombiniert) gateway-seitig ok; **`metadata.origin` nicht filterbar** (400 `api.request.invalid-filter`)
- [x] `tools/devices.py`: `get_device` (Detail, 404 → `not_found`), `get_device_statistics`, `list_pending_devices` (Top-Level, kein `site_id`)
- [x] `tools/dns.py` (neu): `list_dns_policies` (Filter `record_type` [Allowliste inkl. Aliase `a`/`forward`], `domain`, `enabled`), `get_dns_policy` (Detail, 404 → `not_found`)
- [x] `tools/acl.py`: `get_acl_rule_ordering` (`orderedAclRuleIds` → `ordered_rule_ids`)
- [x] Capability-Probes: `dns_policies` (site-scoped) + `pending_devices` (top-level) — `get_system_info` meldet beide Kategorien
- [x] Fixtures: `dns_policies.json` (2× `A_RECORD`, 1× `FORWARD_DOMAIN`)
- [x] Unit-Tests: Device-Registration (4 Tools) + Behavior (Detail, Statistics, Pending), DNS-Filter-Matrix + Aliase + 404 + Registration, ACL-Ordering
- [x] E2E: `test_dns_e2e.py` (neu, 6 Tests), `test_tools_e2e.py` +5 Tests (get_device, get_device 404, statistics, pending-devices, acl-ordering), Capability-Assertions in `get_system_info`
- [x] Contract-Test: exakt 26 Tools (20 + 6 WP-13b) + read-only-hints
- [x] Doku: api-notes §5/§5f, README (Devices +3, DNS neu, ACL +1), OpenAPI-10.6.106-Referenz
- [x] Lokal: ruff + mypy strict + pytest (362 Tests) grün
- [x] Live-Smoke gegen UCG (10.6.106): alle 6 neuen Tools erfolgreich (Device-Detail + Statistics am USW Pro 8 PoE, 6 DNS-Policies inkl. `type.eq`-Filter, ACL-Ordering mit 4 Regeln, Pending-Devices leer, Capability-Checks `dns_policies=ok` + `pending_devices=ok`)

Status: `done`

## Post-MVP (Phasen 2–5 laut Design-Doc)

### WP-14 — Safe-Writes-Fundament

Basis für spätere Write-Tools (WP-15/16), **ohne** echte Write-Tools: `state_hash` (stabil, Key-Reihenfolge-unabhängig, volatile Stats + Secrets ignoriert), Read-before-write-Guard, Modifiability-Guard (`metadata.origin == USER_DEFINED`, fail-closed), Field-Allowlist, Full-Replace-PUT-Client, Audit-Log (redigiert, stdout + optional JSONL).

Abnahme: §35 Write-Guard-Tests: Flag aus → Tool nicht registriert; falscher Hash → kein Write; nicht erlaubtes Feld → kein Write; korrekter Hash → genau ein Write.

- [x] **Live-Discovery (WP-14, 10.6.106):** 32 non-GET-Operationen im OpenAPI-Spec; **alle Updates Full-Replace-PUT** (POST/PUT teilen sich das DTO), PUT 200 liefert aktualisiertes Detail-Objekt; einziger `PATCH` = Firewall-Policy `loggingEnabled`; **kein `revision`/`etag`/`If-Match`** im Spec → client-seitiger Hash zwingend; nur `metadata.origin == USER_DEFINED` modifizierbar; ACL-`index` im PUT wirkungslos (Ordering-Endpoint ist separat); WiFi-PUT round-trippt Klartext-PSK → Write-Pfad nutzt **rohes** (unredigiertes) Objekt, PSK bleibt im Prozess; Details in `docs/unifi-api-notes.md` §11
- [x] `safety/state_hash.py`: `compute_state_hash()` → `"sha256:<hex>"` über kanonisches JSON (sortierte Keys) der normalisierten, secret-freien Darstellung; `VOLATILE_FIELDS` (uptime, rx/tx, lastSeen …) ignoriert; **Präsenz** eines redigierten Feldes ändert den Hash, sein **Wert** nicht
- [x] `safety/guards.py`: `verify_expected_state()` (frischer GET, nie gecacht, `hmac.compare_digest` → rohes Objekt), `assert_user_defined()` (fail-closed), `apply_patch()` (Top-Level-Field-Allowlist → `(merged, diff)`, kein Teil-Apply)
- [x] `safety/audit.py`: `AuditLog` (JSONL + `flush`), `redact()`, stdout-Fallback bei Open-/Write-Fehlern (kein Crash), Prozess-Singleton `get_audit_log()`/`reset_audit_log()`
- [x] `tools/errors.py`: `StateMismatchError` (code `state_changed`, Felder `resource` + `current_state_hash` zum Sofort-Retry)
- [x] `unifi/client.py`: `put()` (idempotent, wird bei transienten Fehlern geretryt) + `post()` (Create, **nie** geretryt)
- [x] `config.py`: `audit_log_path` (Default `""` = stdout-only); `.env.example` + `conftest` `ENV_KEYS` + `mcp_app_factory(**settings_overrides)`
- [x] `tools/common.py`: `with_state_hash(detail)` + `wrap_tool(..., write=False)` (`record_write_request`); 7 Detail-Tools liefern jetzt `state_hash`: `get_wifi`, `get_network`, `get_acl_rule`, `get_firewall_zone`, `get_firewall_policy`, `get_traffic_matching_list`, `get_dns_policy` (Clients/Devices korrekt **ohne** — nicht per Update-DTO mutierbar)
- [x] `tools/writes.py`: `guarded_update()` — verify → origin → apply_patch → `_strip_server_managed` (`id` + `INTERNAL_FIELDS` = {metadata, etag, revision}) → `client.put` → frischer `state_hash`; jeder Rejection-Exit audited; `changes` = diff nur bei Write-Exit
- [x] **Unit-Tests:** `test_state_hash.py`, `test_guards.py`, `test_client_writes.py` (PUT-Body/Retry, POST no-retry, Body nie geloggt), `test_audit.py` (JSONL/Append/Redaction/Fallback/Singleton), `test_tools_errors.py` (`state_changed`-Shape), `test_config.py`
- [x] **E2E (Abnahme, §35):** `tests/integration/test_write_e2e.py` (6 Tests) per nur-zur-Test-Write-Tool in synthetischer `WRITE_GROUP`: Flag off → 26 Tools (kein Write-Tool), Flag on → 27 + `read_only_hint=False`; stale-Hash → `state_changed` + **0 PUT** + Audit; `metadata.origin ≠ USER_DEFINED` → `validation` (Field `metadata.origin`) + **0 PUT**; fremdes Feld → `validation` (Field `changes`, value `name`, allowed `[description]`) + **0 PUT**; korrekter Hash → **genau 1 PUT** (Body ohne `id`/`metadata`/`etag`/`revision`) + frischer `state_hash` + Audit `result: "ok"`
- [x] Doku: `docs/unifi-api-notes.md` §11 „Write-Endpunkte", README (Safe-Writes-Fundament-Abschnitt + `AUDIT_LOG_PATH`), `.env.example`
- [x] Lokal: ruff + mypy strict (38 Dateien) + pytest (427 Tests) grün
- [x] Live-Smoke (Read-only) gegen UCG (10.6.106): Gateway erreichbar, Site aufgelöst, `state_hash` über zwei unabhängige Reads einer ACL-Regel **stabil** (identisch)
- [ ] Live-Write (ACL-`description`) — **bewusst vom User abgesagt**: alle 4 ACL-Regeln haben `description=None` (kein Platzhalter wie vermutet); Write-Pfad ist bereits per E2E (Fake-API) + Live-Read (Hash-Stabilität) abgedeckt

Status: `done`

### WP-15 — Erste Write-Tools

Bewusst risikoarm: `update_wifi` (name/enabled), `update_acl_rule` (enabled).

Status: `open`

### WP-16 — Restliche Write-Tools

create/update für Networks, WiFi, Firewall-Zonen, ACL, Traffic-Matching-Lists.

Status: `open`

### WP-17 — Actions

`restart_device`, `cycle_port`, `reconnect_client` — strikte semantische Allowlist, kein generisches `action: string`-Passthrough, `ENABLE_ACTION_TOOLS`. Factory reset / remove / adopt nicht exponieren.

Status: `open`

### WP-18 — Delete + Deep Diagnostics (optional)

Delete-Tools mit `expected_state_hash` + `confirm_name` (exakter Name-Abgleich), `ENABLE_DELETE_TOOLS`.
Deep Diagnostics erst wenn die offizielle API nicht reicht; SSH nur als strikt separates Modul mit eigenen Credentials.

Status: `open`

## Offene Punkte

- [x] TLS zum Gateway: entschieden — `UNIFI_TLS_MODE=strict|extract-once|insecure` (Default `extract-once`); `strict` per CA-Bundle (SOPS-Secret im Deploy-Repo, falls gepinnt werden soll), `insecure` nur lokale Dev
- [x] API-Key vorhanden — **aber voller Admin** (keine pro-Key-Scopes); Read-only nur server-seitig. Empfehlung: view-only Admin (Details: `docs/unifi-api-notes.md` §9)
- [ ] SOPS/Flux im Deploy-Repo: Secrets für API-Key + MCP-Token (Cert-Secret nur bei `strict`)
- [x] GH-Actions: beide Architekturen — `linux/amd64,linux/arm64` (buildx-Cross-Build, Muster `workflow-example.yaml`)
- [ ] Design-Doc-Dateiname/-Titel sagt „Kubernetes", Deployment-Abschnitte betreffen aber das Deploy-Repo — evtl. im Doc vermerken

## MVP Definition of Done (Referenz §47)

### Server-seitig (dieses Repo)

- [x] offizieller lokaler UniFi Network API Client funktioniert (WP-4, live gegen 10.6.101 verifiziert)
- [x] keine Legacy-/undokumentierten Endpunkte (Endpoint-Audit WP-12: 24/24 im OpenAPI-Spec, alle GET, keine Legacy-Pfade)
- [x] Streamable HTTP `/mcp` funktioniert, stateless (`stateless_http=True` + Contract-Tests)
- [x] MCP-Upstream-Auth (Bearer) funktioniert (Middleware + Tests 401/403)
- [x] non-root Container-Image (Dockerfile `USER unifi`, uid 10001)
- [x] Read-only Toolset verfügbar (17 MVP-Tools; + 3 ZBF-Policy-Tools in WP-13; + 6 Read-Tools in WP-13b = 26, Registry + Contract-Tests)
- [x] Write-/Action-/Delete-Tools sind nicht registriert (nur Read-Gruppen, Gating-Tests)
- [x] Secrets werden redigiert (`redaction.py` + Unit/E2E, PSK live verifiziert)
- [x] Pagination und Response-Limits existieren (`normalization.py` + Tests)
- [x] Logs sind strukturiert (structlog-JSON + Tests)
- [x] Unit-, Mock-Integration- und MCP-Contract-Tests existieren (299 Tests, grün)
- [x] Live-Smoke-Test gegen echtes Gateway erfolgreich (WP-12, 10/10, Gateway 10.6.101)
- [x] README und `docs/unifi-api-notes.md` vorhanden (WP-12)

### Cluster-seitig (→ Deploy-Repo)

- [ ] Deployment läuft non-root, ServiceAccount-Token deaktiviert
- [ ] SOPS-verschlüsselte Secrets (API-Key, MCP-Token; Cert-Secret nur bei `strict`)
- [ ] NetworkPolicy begrenzt Ingress (nur LiteLLM) und Egress (DNS + Gateway:443)
- [ ] LiteLLM verbindet sich per ClusterIP
- [ ] LiteLLM-Permissions: nur freigeschaltete Keys
- [ ] Test über LiteLLM erfolgreich
