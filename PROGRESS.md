# Entwicklungstracking — unifi-mcp

Stand: 2026-09-16 (WP-0 … WP-3 done)
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
| WP-4  | UniFi-Client-Kern                      | 1        | open        | WP-2, WP-3|
| WP-5  | Normalisierung, Redaction, Limits      | 1        | open        | WP-4      |
| WP-6  | MCP-Server-Gerüst + Auth               | 1        | open        | WP-3–5    |
| WP-7  | Tool-Framework + System/Sites/Devices  | 2        | open        | WP-6      |
| WP-8  | Clients + inspect_client_path          | 2        | open        | WP-7      |
| WP-9  | Networks / WiFi / Firewall             | 2        | open        | WP-7      |
| WP-10 | ACL / Traffic / Reference              | 2        | open        | WP-7      |
| WP-11 | Dockerfile + GH-Actions (ghcr)         | 3        | open        | WP-6      |
| WP-12 | Tests, Smoke, README, DoD              | 3        | open        | WP-7–11   |
| WP-13 | Safe-Writes-Fundament                  | post-MVP | open        | WP-12     |
| WP-14 | Erste Write-Tools                      | post-MVP | open        | WP-13     |
| WP-15 | Restliche Write-Tools                  | post-MVP | open        | WP-14     |
| WP-16 | Actions                                | post-MVP | open        | WP-15     |
| WP-17 | Delete + Deep Diagnostics (optional)   | post-MVP | open        | WP-16     |

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

Status: `open`

### WP-5 — Normalisierung, Redaction, Limits

`unifi/normalization.py`: Summary/Detail-Ebenen, interne Felder entfernen.
`safety/redaction.py`: case-insensitive Feldliste §8 (password, passphrase, psk, secret, token, apiKey, private_key, credential, authorization, …); WiFi-PSKs nie ausgeben.
Pagination-Helper: default 50, max 200, Antwortformat `{items, count, total_count, next_offset}`.
Hartes Response-Size-Limit (`response_too_large`-Fehler), strukturierte LLM-Fehler (`not_found`, `ambiguous_match`, §30).

Abnahme: Redaction-, Pagination- und Size-Tests.

Status: `open`

### WP-6 — MCP-Server-Gerüst + Auth

`app.py`: ASGI, `/mcp` (stateless Streamable HTTP), `/healthz` (nur Prozess), `/readyz` (Config + Client + Application-Info-Check mit 30 s-Cache).
`auth/middleware.py`: Bearer-Token, 401/403, Auth-Failure-Metrik.
`server.py`: MCP-Instanz, Tool-Registrierung gesteuert durch Feature Flags (aus → nicht in `tools/list`), Server Instructions (§32).

Abnahme: Contract-Test — initialize, `tools/list`, 401 ohne Token, keine Write-Tools sichtbar.

Status: `open`

## Phase 2 — Read-only Tools

### WP-7 — Tool-Framework + System/Sites/Devices

Gemeinsame Tool-Helper (Pagination, Limits, Error-Mapping), Tool-Description-Konvention (§31).
Tools: `get_system_info`, `list_sites`, `list_devices` (Filter: site_id, device_type, state, search, limit).

Abnahme: E2E gegen Fake-API; `get_system_info` liefert Version + Capabilities.

Status: `open`

### WP-8 — Clients + inspect_client_path

`list_clients` (Filter: site_id, network_id, connected_to_device_id, search, limit), `get_client` (exakt einer von client_id / mac / ip / hostname, sonst `ambiguous_match`).
`inspect_client_path`: Client + Network/VLAN + Attachment (Device/Port) + Beobachtungen — nur Fakten aggregieren, keine spekulative Root-Cause-Analyse (§11).

Abnahme: Fixture-Test zum Szenario „Finde wk-5 und zeig mir, worüber er verbunden ist" + Ambiguitätsfall.

Status: `open`

### WP-9 — Networks / WiFi / Firewall

`list_networks`, `get_network`, `list_wifi`, `get_wifi` (nie PSK), `list_firewall_zones`, `get_firewall_zone`.

Abnahme: Fixture-Integrationstests; Redaction auf WiFi-Objekten verifiziert.

Status: `open`

### WP-10 — ACL / Traffic / Reference

`list_acl_rules` (Filter: enabled, source, destination, action), `get_acl_rule`, `list_traffic_matching_lists`, `get_traffic_matching_list`, `list_reference_resources` (8 Resource-Typen in einem Tool, §10.10).

Abnahme: Fixture-Integrationstests.

Status: `open`

## Phase 3 — Container & MVP-Abschluss

### WP-11 — Dockerfile + GitHub Actions (ghcr)

Dockerfile: Multi-stage (uv-Build-Stage), schlankes Runtime-Image, non-root, gepinnte Dependencies, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONUNBUFFERED=1`, read-only root-FS-kompatibel, Healthcheck.
GH-Actions: Build (buildx, `linux/amd64`) + Push auf **Tags** (`vX.Y.Z`) und **master** (SHA-Tag) → `ghcr.io/FabianLiske/unifi-mcp`, kein nacktes `latest`.

Abnahme: Build lokal + in CI grün; `docker run` + `/healthz` OK.

Status: `open`

### WP-12 — Tests, Smoke, README, DoD

MCP-Contract-Tests (§37), Fake-UniFi-API-Integrationstests mit Fixtures (§36), Live-Smoke-Test per `docker run` gegen das echte Gateway (Checkliste §38, Gateway-Cert gemountet), README (§46), MVP-DoD-Checkliste (§47).

Abnahme: MVP-DoD vollständig (Cluster-Punkte als „→ Deploy-Repo" markiert).

Status: `open`

## Post-MVP (Phasen 2–5 laut Design-Doc)

### WP-13 — Safe-Writes-Fundament

`state_hash` (stabil, Key-Reihenfolge-unabhängig, volatile Stats ignoriert), read-before-write-Guard, Patch-Semantik statt Replace, Field-Allowlists, Write-Guards, Audit-Log (redigiert), `ENABLE_WRITE_TOOLS`.

Abnahme: §35 Write-Guard-Tests: Flag aus → Tool nicht registriert; falscher Hash → kein Write; nicht erlaubtes Feld → kein Write; korrekter Hash → genau ein Write.

Status: `open`

### WP-14 — Erste Write-Tools

Bewusst risikoarm: `update_wifi` (name/enabled), `update_acl_rule` (enabled).

Status: `open`

### WP-15 — Restliche Write-Tools

create/update für Networks, WiFi, Firewall-Zonen, ACL, Traffic-Matching-Lists.

Status: `open`

### WP-16 — Actions

`restart_device`, `cycle_port`, `reconnect_client` — strikte semantische Allowlist, kein generisches `action: string`-Passthrough, `ENABLE_ACTION_TOOLS`. Factory reset / remove / adopt nicht exponieren.

Status: `open`

### WP-17 — Delete + Deep Diagnostics (optional)

Delete-Tools mit `expected_state_hash` + `confirm_name` (exakter Name-Abgleich), `ENABLE_DELETE_TOOLS`.
Deep Diagnostics erst wenn die offizielle API nicht reicht; SSH nur als strikt separates Modul mit eigenen Credentials.

Status: `open`

## Offene Punkte

- [x] TLS zum Gateway: entschieden — `UNIFI_TLS_MODE=strict|extract-once|insecure` (Default `extract-once`); `strict` per CA-Bundle (SOPS-Secret im Deploy-Repo, falls gepinnt werden soll), `insecure` nur lokale Dev
- [x] API-Key vorhanden — **aber voller Admin** (keine pro-Key-Scopes); Read-only nur server-seitig. Empfehlung: view-only Admin (Details: `docs/unifi-api-notes.md` §9)
- [ ] SOPS/Flux im Deploy-Repo: Secrets für API-Key + MCP-Token (Cert-Secret nur bei `strict`)
- [ ] GH-Actions: nur `linux/amd64` oder auch arm64? (Default-Annahme: amd64)
- [ ] Design-Doc-Dateiname/-Titel sagt „Kubernetes", Deployment-Abschnitte betreffen aber das Deploy-Repo — evtl. im Doc vermerken

## MVP Definition of Done (Referenz §47)

### Server-seitig (dieses Repo)

- [ ] offizieller lokaler UniFi Network API Client funktioniert
- [ ] keine Legacy-/undokumentierten Endpunkte
- [ ] Streamable HTTP `/mcp` funktioniert, stateless
- [ ] MCP-Upstream-Auth (Bearer) funktioniert
- [ ] non-root Container-Image
- [ ] Read-only Toolset verfügbar
- [ ] Write-/Action-/Delete-Tools sind nicht registriert
- [ ] Secrets werden redigiert
- [ ] Pagination und Response-Limits existieren
- [ ] Logs sind strukturiert
- [ ] Unit-, Mock-Integration- und MCP-Contract-Tests existieren
- [ ] Live-Smoke-Test gegen echtes Gateway erfolgreich
- [ ] README und `docs/unifi-api-notes.md` vorhanden

### Cluster-seitig (→ Deploy-Repo)

- [ ] Deployment läuft non-root, ServiceAccount-Token deaktiviert
- [ ] SOPS-verschlüsselte Secrets (API-Key, MCP-Token; Cert-Secret nur bei `strict`)
- [ ] NetworkPolicy begrenzt Ingress (nur LiteLLM) und Egress (DNS + Gateway:443)
- [ ] LiteLLM verbindet sich per ClusterIP
- [ ] LiteLLM-Permissions: nur freigeschaltete Keys
- [ ] Test über LiteLLM erfolgreich
