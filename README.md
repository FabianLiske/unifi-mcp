# unifi-mcp

MCP-Server, der LLM-Clients via MCP (Streamable HTTP) nur-Read-only-Zugang auf die offizielle lokale UniFi Network API eines Cloud Gateways gewährt — ohne SSH, ohne beliebigen REST-Passthrough, ohne Secrets im Output.

## Zweck

unifi-mcp ist ein Adapter zwischen MCP und der lokalen UniFi Network API. Ein LLM kann damit:

- Netzwerkzustand und Topologie untersuchen (Versionen, Sites, Geräte),
- Clients finden und diagnostizieren (inkl. `inspect_client_path`),
- Networks/VLANs, WiFi, Firewall-Zonen und -Policies, ACL-Regeln und Traffic-Matching-Lists lesen.

Der MVP ist strikt read-only: Es werden ausschließlich Read-Tools registriert, und das Gateway bekommt nur `GET`-Requests. Details zum geplanten Write-/Action-Design stehen im [Design-Doc](unifi-mcp-kubernetes-design.md) (Kapitel 13–18, 40) und in [PROGRESS.md](PROGRESS.md) (WP-13 ff.).

## Architektur

```text
OpenCode / LiteLLM / andere MCP-Clients
        │ MCP, Streamable HTTP, Bearer-Auth
        ▼
   /mcp   (stateless, Starlette-App via offiziellem MCP Python SDK)
        │
   UniFi HTTP-Client (httpx, Header X-API-Key, TLS-Modi)
        │ HTTPS
        ▼
   UniFi Cloud Gateway — lokale Network API
   {UNIFI_BASE_URL}/proxy/network/integration/v1
```

- `unifi/` — HTTP-Client gegen die offizielle API (Stateless, keine Sessions), Capability-Check beim Start, TLS-Handling, Normalisierung (Uppercase-Enums, Paging-Envelope).
- `tools/` — Tool-Registry mit Feature-Flag-Gates pro Gruppe; jedes Tool läuft durch `wrap_tool` (Metriken, Response-Size-Cap, strukturierte Fehler).
- `auth/` — ASGI-Middleware: statisches Bearer-Token auf `/mcp`, `/healthz` und `/readyz` bleiben öffentlich (Kubernetes-Probes).
- `safety/` — rekursive, field-name-basierte Secret-Redaction.
- `observability/` — structlog (JSON/Text, `request_id`) und Prometheus-Metriken mit begrenzten Labels.

Der Server ist stateless ausgelegt: keine langlebigen MCP-Sessions — trivial horizontal skalierbar, restart-freundlich. Endpunkte: `/mcp` (MCP), `/healthz` (Liveness, nur Prozess), `/readyz` (Readiness, Gateway-Reachability, max. 1×/30 s gecacht, 503 wenn Gateway nicht erreichbar).

## Voraussetzungen

- Python **3.12+** und [uv](https://docs.astral.sh/uv/) (lokale Entwicklung und Tests),
- ein erreichbares UniFi Cloud Gateway (z. B. UCG Ultra) mit lokaler Network API (HTTPS),
- ein UniFi API-Key (siehe unten),
- für den Betrieb: Docker bzw. ein Kubernetes-Cluster und LiteLLM (Deployment liegt in einem separaten Deploy-Repo).

## UniFi API Key erstellen

1. UniFi Network-UI öffnen, rechts oben **Integrations** (bzw. „API Keys“) aufrufen.
2. API-Key erstellen. Der Server nutzt den Key als Header `X-API-Key` — stateless, kein Bearer/Session.

Wichtig: UniFi API-Keys haben **keine eigenen Scopes** und erben die Rechte des erstellenden Accounts. Für den MVP daher einen dedizierten **view-only Admin**-User verwenden und den Key nur für unifi-mcp nutzen. Details und Begründung: [docs/unifi-api-notes.md](docs/unifi-api-notes.md), Abschnitt 9. Der Key gehört in ein Secret (K8s: SOPS), niemals in Images, ConfigMaps, Logs oder das Repo.

## Lokale Entwicklung

```bash
cp .env.example .env    # Werte ausfuellen (UNIFI_BASE_URL, UNIFI_API_KEY, MCP_AUTH_TOKEN)
uv sync                 # Dependencies aus uv.lock installieren
uv run unifi-mcp        # Server starten (Default: 0.0.0.0:8000)
```

Smoke-Check: `curl -s http://localhost:8000/healthz` (→ `{"status":"ok"}`) und `curl -s http://localhost:8000/readyz` (→ 200/`ready` wenn Gateway erreichbar).

## Environment Variables

Laden über `pydantic-settings`; echte Umgebungsvariablen überschreiben Werte aus `.env`. `.env` ist gitignored, Vorlage: [.env.example](.env.example).

| Variable | Default | Bedeutung |
| --- | --- | --- |
| `UNIFI_BASE_URL` | — (Pflicht) | Base URL des Cloud Gateways inkl. Scheme (z. B. `https://172.26.x.x`) |
| `UNIFI_API_KEY` | — (Pflicht) | API-Key des Gateways (Secret) |
| `UNIFI_SITE_ID` | leer | Site-ID; leer = Default-Site (erste Site des Gateways) |
| `UNIFI_TLS_MODE` | `extract-once` | `strict` (CA-Verifikation), `extract-once` (Cert beim Start ziehen), `insecure` (nur lokale Entwicklung) |
| `UNIFI_CA_BUNDLE` | — (Pflicht bei `strict`) | Pfad zum CA-Bundle, das das Gateway-Zertifikat verifiziert |
| `UNIFI_TIMEOUT_SECONDS` | `15` | Request-Timeout zum Gateway |
| `UNIFI_CONNECT_TIMEOUT_SECONDS` | `5` | Verbindungs-Timeout |
| `UNIFI_MAX_RETRIES` | `2` | Retries bei transienten Fehlern |
| `MCP_AUTH_TOKEN` | — (Pflicht) | Bearer-Token für MCP-Clients (Secret), z. B. `openssl rand -hex 32` |
| `MCP_BIND_HOST` | `0.0.0.0` | Bind-Adresse des MCP-Endpoints |
| `MCP_BIND_PORT` | `8000` | Port von `/mcp`, `/healthz`, `/readyz` |
| `LOG_LEVEL` | `INFO` | `DEBUG` … `CRITICAL` |
| `LOG_FORMAT` | `json` | `json` oder `text` |
| `ENABLE_WRITE_TOOLS` | `false` | Feature-Flag (MVP: aus, Tools nicht implementiert) |
| `ENABLE_ACTION_TOOLS` | `false` | Feature-Flag (MVP: aus, Tools nicht implementiert) |
| `ENABLE_DELETE_TOOLS` | `false` | Feature-Flag (MVP: aus, Tools nicht implementiert) |
| `MAX_LIST_ITEMS` | `200` | Obergrenze für List-Tools (limit-Parameter) |
| `MAX_TOOL_RESPONSE_BYTES` | `262144` | Harte Obergrenze der Tool-Response in Bytes |

## Docker Build

Lokaler Build (Multi-Arch):

```bash
docker buildx build --platform linux/amd64,linux/arm64 -t unifi-mcp .
```

Laufen mit den Env-Vars aus `.env.example` (Secrets als Platzhalter):

```bash
docker run --rm -it --read-only \
  -e UNIFI_BASE_URL=https://172.26.x.x \
  -e UNIFI_API_KEY="<api-key>" \
  -e MCP_AUTH_TOKEN="<token>" \
  -e UNIFI_TLS_MODE=extract-once \
  -p 8000:8000 \
  unifi-mcp
```

`--read-only` funktioniert, weil das Image für ein read-only Root-Filesystem gebaut ist (`HOME=/tmp`).

CI/CD (`.github/workflows/build-image.yaml`): Push auf `master` → Tag `sha-<kurzhash>`, Push von Tags `vX.Y.Z` → Semver-Tag, Repository `ghcr.io/fabianliske/unifi-mcp`, Plattformen `linux/amd64` + `linux/arm64`, bewusst **kein** `latest`-Tag.

Dockerfile: Multi-stage (Builder mit gepinnten Dependencies via `uv sync --frozen` aus `uv.lock`; Runtime `python:3.12-slim`), non-root (uid `10001`), `PYTHONDONTWRITEBYTECODE=1`/`PYTHONUNBUFFERED=1`, Healthcheck gegen `/healthz`, kein Compiler im Runtime-Image.

## Kubernetes Deployment

Die Kubernetes-Manifeste liegen **nicht** in diesem Repo, sondern in einem separaten Deploy-Repo (Namespace, Deployment, Service, NetworkPolicy, Secrets); Architektur-Anforderungen: Design-Doc, Abschnitte 21–25. Wichtigste Punkte:

- Deployment non-root, `readOnlyRootFilesystem: true`, `capabilities.drop: ["ALL"]`, 1 Replica; `emptyDir` auf `/tmp`, falls Schreibplatz nötig.
- ServiceAccount **ohne** Token: `automountServiceAccountToken: false`, keine RBAC-Roles — der MCP braucht keinen K8s-API-Zugriff.
- ClusterIP-Service auf Port 8000; LiteLLM spricht den MCP intern an (kein Ingress nötig, optionaler Ingress nur für internes Debugging).
- NetworkPolicy: Ingress nur aus dem LiteLLM-Pod/Selector, Egress nur DNS + Gateway (IP:443). Kein freier Internet-Egress.
- Secrets (`UNIFI_API_KEY`, `MCP_AUTH_TOKEN`, bei `strict` zusätzlich `UNIFI_CA_BUNDLE`) via **SOPS**; niemals im Image oder in einer ConfigMap.
- Probes: `readinessProbe` auf `/readyz`, `livenessProbe` auf `/healthz`.

## LiteLLM Integration

LiteLLM läuft im selben Cluster und verbindet den MCP-Client (z. B. OpenCode) per Streamable HTTP + Bearer-Token mit `/mcp`. Konfiguration im LiteLLM-Deploy (aus Design-Doc, Abschnitt 20; Syntax je nach LiteLLM-Version prüfen):

```yaml
mcp_servers:
  unifi:
    server_id: "unifi"
    url: "http://unifi-mcp.ai-tools.svc.cluster.local:8000/mcp"
    transport: "http"
    description: "Local UniFi Network management and diagnostics"
    auth_type: "bearer_token"
    auth_value: os.environ/UNIFI_MCP_TOKEN
```

`UNIFI_MCP_TOKEN` muss `MCP_AUTH_TOKEN` des MCP-Servers entsprechen. Key-Permissions eng halten: nur die Keys/Teams, die den UniFi-MCP wirklich brauchen, freischalten (kein `allow_all_keys`) — die Sicherheit darf nicht allein an LiteLLM hängen, der MCP schützt sich selbst (Bearer-Auth, Feature-Flags, Read-only-Default).

## Tool-Liste

Alle Tools sind read-only und geben normalisierte, redigierte Daten zurück. List-Tools unterstützen `limit`/`offset` (Default 50, max. `MAX_LIST_ITEMS`); `next_offset` im Ergebnis signalisiert, dass weitere Seiten folgen.

### System

| Tool | Beschreibung |
| --- | --- |
| `get_system_info` | Gateway-Version, aktive Site, MCP-Version und Verfügbarkeit der API-Kategorien (z. B. `firewall: not_configured`) — als ersten Call verwenden |

### Sites

| Tool | Beschreibung |
| --- | --- |
| `list_sites` | Alle Sites des Gateways (id, name); `site_id` für alle site-scoped Tools |

### Devices

| Tool | Beschreibung |
| --- | --- |
| `list_devices` | Adoptierte Geräte (Gateways, Switches, Access Points) mit State, Modell, Firmware, IP; Filter `site_id`, `device_type`, `state`, `search` |
| `get_device` | Detailobjekt eines Geräts per ID inkl. Firmware-Status, Features und Interfaces (Radios, Ports) |
| `get_device_statistics` | Letzte Live-Statistiken eines Geräts: Uptime, CPU-/Speicherauslastung, Load-Average, Uplink-Raten |
| `list_pending_devices` | Noch nicht adoptierte Geräte am Gateway (Top-Level-Endpunkt, kein `site_id`) |

### Clients

| Tool | Beschreibung |
| --- | --- |
| `list_clients` | Verkabelte und drahtlose Clients mit Name, IP, MAC, Verbindungstyp und uplink-Gerät; Filter `site_id`, `network_id`, `connected_to_device_id`, `search` (exakte IP/MAC oder Namens-Teilstring) |
| `get_client` | Detailobjekt eines Clients; genau ein Identifier: `client_id`, `mac`, `ip` oder `hostname` |
| `inspect_client_path` | Composite-Diagnose für einen Client: Detailobjekt + zugehöriges Network + uplink-Gerät + neutrale Fakten („observations“), keine Root-Cause-Analyse |

### Networks

| Tool | Beschreibung |
| --- | --- |
| `list_networks` | Networks (VLANs) der Site mit Name, VLAN-ID, Status und Management-Zweck (Summary) |
| `get_network` | Vollständige Konfiguration eines Networks (IPv4, DHCP, Isolation etc.) |

### DNS

| Tool | Beschreibung |
| --- | --- |
| `list_dns_policies` | DNS-Policies der Site (eigene DNS-Records und Forward-Domains) mit Typ, Domain, Record-Wert und enabled-Flag; Filter `site_id`, `record_type`, `domain` (Teilstring), `enabled` — alle gateway-seitig |
| `get_dns_policy` | Eine DNS-Policy per ID inkl. typspezifischer Felder (`ipv4Address`, `targetDomain`, Forwarder-Adresse, …) |

### WiFi

| Tool | Beschreibung |
| --- | --- |
| `list_wifi` | WiFi-Profile (SSIDs) mit Sicherheitsmodus, gekoppeltem Network und Bands; PSK wird redigiert und nie zurückgegeben |
| `get_wifi` | Vollständige Konfiguration eines WiFi-Profils; PSK wird redigiert und nie zurückgegeben |

### Firewall

| Tool | Beschreibung |
| --- | --- |
| `list_firewall_zones` | Zone-Based-Firewall-Zonen der Site; liefert strukturiert `unsupported`, wenn das Gateway die Zone-Based-Firewall nicht konfiguriert hat |
| `get_firewall_zone` | Eine Firewall-Zone per ID |
| `list_firewall_policies` | Zone-Based-Firewall-Policies (Zonenpaar-Regeln) mit Action, enabled, Index und Zone-Zuordnung (Summary, tiefe Traffic-Filter weggeprunkt); Filter `name`, `origin` (user \| system), `source_zone_id`, `destination_zone_id` — alle gateway-seitig. Abgeleitete System-Policies können ohne `id` sein (dann nicht per Detail abrufbar) |
| `get_firewall_policy` | Eine Firewall-Policy per ID inkl. vollständiger Source-/Destination-Traffic-Filter (Netzwerke, IPs/Subnets, Ports, DPI-Apps) |
| `get_firewall_policy_ordering` | Evaluationsreihenfolge der Policies für ein Zonenpaar: ID-Listen `before_system_defined` / `after_system_defined` (vor/nach den System-Policies) |

### ACL

| Tool | Beschreibung |
| --- | --- |
| `list_acl_rules` | ACL-Regeln mit Action, enabled-Flag, Regel-Index und Endpoint-Filtern (Summary); Filter `action`, `enabled`, `source`, `destination` |
| `get_acl_rule` | Eine ACL-Regel inkl. vollständiger Source-/Destination-Filter |
| `get_acl_rule_ordering` | Evaluationsreihenfolge aller ACL-Regeln der Site als ID-Liste (Liste = `index`-Reihenfolge, niedriger = zuerst) |

### Traffic

| Tool | Beschreibung |
| --- | --- |
| `list_traffic_matching_lists` | Traffic Matching Lists (benannte Sammlungen von IPv4-Adressen/Subnets oder Ports) |
| `get_traffic_matching_list` | Eine Traffic Matching List inkl. aller Einträge |

### Reference

| Tool | Beschreibung |
| --- | --- |
| `list_reference_resources` | Selten genutzte Lookup-Tabellen in einem Tool: `countries`, `dpi_applications`, `dpi_categories`, `device_tags`, `radius_profiles`, `site_to_site_vpn_tunnels`, `vpn_servers`, `wan_interfaces` |

Hinweis: Im Design-Doc sind zusätzlich `get_device`, `get_device_stats` und `list_pending_devices` skizziert; diese sind im aktuellen Stand noch nicht implementiert (siehe PROGRESS.md).

## Read/Write-Sicherheitsmodell

- **Read-only-Default:** Es werden nur Read-Tools registriert. Write/Action/Delete-Gruppen hängen hinter den Feature-Flags `ENABLE_WRITE_TOOLS` / `ENABLE_ACTION_TOOLS` / `ENABLE_DELETE_TOOLS` (alle `false`) und werden beim Gating **bei der Registrierung** übersprungen — abgeschaltete Tools fehlen in `tools/list` komplett, statt erst zur Laufzeit abgelehnt zu werden. Im MVP sind Write-Tools zudem noch gar nicht implementiert.
- **Bearer-Auth:** `/mcp` verlangt `Authorization: Bearer <MCP_AUTH_TOKEN>` (Konstantenzeit-Vergleich); fehlendes Token → 401, falsches Token → 403, inkl. Auth-Failure-Metrik. `/healthz` und `/readyz` sind bewusst öffentlich (Kubernetes-Probes senden kein Token).
- **Redaction:** Alle UniFi-Antworten werden vor dem LLM-Output rekursiv nach Feldnamen redigiert (case-insensitiv, `apiKey`/`api_key`/`API-Key` matchen gleich): u. a. `password`, `passphrase`, `psk`, `secret`, `token`, `apikey`, `privatekey`, `credential`, `authorization` → `[REDACTED]`. Der Key bleibt sichtbar, der Wert wird ersetzt. Over-Redaction ist beabsichtigt.
- **Limits und Pagination:** List-Tools liefern max. `MAX_LIST_ITEMS` (200) Einträge pro Aufruf mit `next_offset`; jede Tool-Response ist hart auf `MAX_TOOL_RESPONSE_BYTES` (256 KiB) begrenzt.
- **Strukturierte Fehler:** Tools liefern kurze JSON-Fehler (`not_found`, `ambiguous_match`, `authentication`, `authorization`, `rate_limited`, `unsupported`, `validation`, `unavailable`) statt roher HTTP-Fehler oder Tracebacks.
- **Kein Passthrough:** Es gibt kein generisches `unifi_request(method, path, body)` und kein SSH — die Angriffsfläche ist die semantische Tool-Allowlist selbst.
- **Schichtung** (Design-Doc, Abschnitt 41): LiteLLM-Key-Permissions, MCP-Bearer-Auth, Kubernetes-NetworkPolicy, Feature-Flags, Tool-Allowlist, UniFi-Key-Rechte. Keine einzelne Schicht gilt als alleiniger Schutz.
- **Einschränkung:** Da UniFi API-Keys keine eigenen Scopes haben, ist die Read-only-Garantie **nur server-seitig**. Mitigation: dedizierter view-only-Admin-Account für den Key (Empfehlung, [docs/unifi-api-notes.md](docs/unifi-api-notes.md) §9) und Key nur in SOPS-Secrets.

## Tests

- `tests/unit/` — Unit-Tests: Config/Validierung, UniFi-Client (Auth, Timeouts, Error-Mapping 401/403/404/429), TLS-Modi, Redaction, Normalisierung, Auth-Middleware, Metriken, Logging, Readiness sowie je Modul Registration- und Verhaltenstests der Tools (mit `respx`-Mocks).
- `tests/integration/` — MCP-Contract-Test (stateless Streamable HTTP, Bearer-Auth, Tool-Liste) und E2E-Tests gegen eine Fake-UniFi-API (gesamter Pfad: Client → Tool → redigierter Output).
- `tests/fixtures/` — Platzhalter für API-Response-Fixtures.

```bash
uv run pytest              # Unit + Integration
uv run ruff check .        # Lint
uv run ruff format --check .
uv run mypy src            # Typcheck (strict)
```

CI (`.github/workflows/ci.yaml`) führt auf Push/PR exakt diese Schritte aus. Der Live-Smoke-Test gegen ein echtes Gateway (Checkliste Design-Doc, Abschnitt 38) wird manuell per `docker run` mit gemountetem Gateway-Cert bzw. `extract-once` ausgeführt.

## Troubleshooting

- **401 am `/mcp`:** Kein `Authorization: Bearer`-Header gesendet — Token auf Client-Seite (LiteLLM `auth_value`) prüfen. **403:** Token vorhanden, aber nicht identisch zu `MCP_AUTH_TOKEN` des MCP-Servers.
- **`/readyz` liefert 503:** Der Server läuft, aber das Gateway ist nicht erreichbar (bzw. Auth zum Gateway fehlerhaft). `UNIFI_BASE_URL`, TLS-Modus und `UNIFI_API_KEY` prüfen; `/healthz` bleibt dabei `200`. Die Readiness-Prüfung läuft max. alle 30 s — ein just repariertes Gateway kann bis zu 30 s nicht sichtbar werden.
- **TLS-Modi:** `strict` verifiziert gegen `UNIFI_CA_BUNDLE` (Pflichtfeld, Server startet sonst nicht) — nur nutzen, wenn das Gateway-Zertifikat verifizierbar ist (z. B. internes CA-Bundle aus SOPS). `extract-once` (Default) für self-signed Gateway-Certs: ein unverifizierter Bootstrap-Handshake, Peer-Cert wird als Session-CA genutzt, SHA-256-Fingerprint prominent geloggt. `insecure` schaltet die Verifikation komplett ab: **nur für lokale Entwicklung**, mit prominentem Warnlog.
- **Fingerprint-Warnung bei `extract-once`:** Beim Bootstrap-Handshake wird der SHA-256-Fingerprint des Gateway-Zertifikats geloggt. Einmal prüfen, dass der Fingerprint zum eigenen Gateway passt (z. B. gegen `certs/gateway.crt` aus der API-Discovery). Passt er nicht: möglicher MITM oder falsches Ziel — nicht ignorieren.
- **429 / `rate_limited`:** Das Gateway drosselt die API. Der Fehler enthält `retry_after_seconds` — Aufrufhäufigkeit reduzieren (weniger parallele List-Calls, gezielt filtern statt breit ziehen).
- **„Tool nicht gefunden“:** Write-/Action-/Delete-Tools sind bei `ENABLE_*_TOOLS=false` schlicht nicht registriert und erscheinen nicht in `tools/list`. Im MVP ist das der korrekte Zustand; die Flags sind für spätere Phasen reserviert und die Tools noch nicht implementiert.
- **`unsupported` bei Firewall-Tools:** Die Zone-Based-Firewall ist auf dem Gateway nicht konfiguriert — das ist ein Gateway-Konfigurationszustand, kein Fehler des MCP (Details: [docs/unifi-api-notes.md](docs/unifi-api-notes.md) §6).
