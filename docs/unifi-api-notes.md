# UniFi Network API — Live Discovery Notes

Live verifiziert gegen das lokale Cloud Gateway (**UCG Ultra**, Network **10.6.101**).
Referenz-OpenAPI: **v10.4.57** (neueste veröffentlichte Version) unter
`docs/reference/network-openapi-10.4.57.json` (44 Pfade).

Alle Befunde unten wurden in WP-2 mit read-only `GET`-Requests verifiziert.
Was nicht als *verified* markiert ist, muss vor Implementierung erneut geprüft werden.

## 1. Base URL & Auth

- Base path: `{UNIFI_BASE_URL}/proxy/network/integration/v1`
  - Beispiel: `https://172.26.1.1/proxy/network/integration/v1`
- Auth: Header **`X-API-Key: <UNIFI_API_KEY>`** (kein `Authorization: Bearer`).
- Stateless REST: keine Session/Token, keine Cookies — jeder Request trägt den Key.
- Der Key wurde von einem **vollen Admin** angelegt (keine pro-Key-Scopes). →
  Die Read-only-Garantie ist nur server-seitig (siehe §9).

## 2. Envelope & Pagination

Listen-Endpunkte liefern ein Paging-Envelope:

```json
{
  "offset": 0,
  "limit": 25,
  "count": 25,
  "totalCount": 47,
  "data": [ ... ]
}
```

- **API-Default-`limit` ist 25** (nicht 100). Max `limit` ist 200.
- Parameter: `?limit=<n>&offset=<m>`. `count` = Einträge auf der Seite,
  `totalCount` = Gesamtzahl.
- Der Client muss paginieren: `offset += limit` bis `count < limit`
  (bzw. `offset >= totalCount`).
- Detail-Endpunkte (`/{resource}/{id}`) liefern das Objekt direkt (kein Envelope).

## 3. Filter-DSL (verified)

`filter` ist eine **funktionsbasierte Expression** (URL-encoded), kein JSON/Infix:

- Property-Expression: `field.function(arg1, [arg2, ...])`
  - `name.like('USW*')` — Wildcard-Match (`*` = beliebiger String, `.` = einzelnes Zeichen)
  - `state.eq('ONLINE')` — equality
  - `id.eq(123)`, `name.isNotNull()`, `createdAt.in(a, b)`
- Compound: `and(expr1, expr2, ...)`, `or(expr1, expr2)`
  - z. B. `and(state.eq('ONLINE'),name.like('USW*'))`
- Negation: `not(expr)` — z. B. `not(name.like('guest*'))`
- String-Args in **Single Quotes**; der gesamte Value wird URL-encoded.
- Ungültige Syntax → HTTP 400 `api.request.invalid-filter` (mit Parse-Fehler).

Verifizierte Filter: `name.like('USW*')` (5 Devices), `name.like('*Gateway*')` (1),
`type.eq('WIRELESS')` (21 Clients), `and(...)`-Kombinationen.

### Wichtig: Enum-Werte sind UPPERCASE

Die Design-Doc unterstellte lowercase. Das Gateway liefert **uppercase**:

- device `state`: `ONLINE` (erwartet zusätzlich: `OFFLINE`, `PENDING`)
- client `type`: `WIRED`, `WIRELESS`
- client `access.type`: `DEFAULT`

→ Tool-Parameter (z. B. `list_devices(state?)`) sollten nutzerfreundliches lowercase
annehmen und vor dem Filter-Build auf die API-Uppercase normalisieren.

## 4. Site-scoped Pfade

Alle per-Site-Resources liegen unter `/sites/{siteId}/{resource}`.
Hier gibt es eine Site:

- `Default` → UUID `88f7af54-98f8-306a-a1c7-c9349722b1f6`

Top-level (nicht site-scoped): `/info`, `/sites`, `/pending-devices`,
`/countries`, `/dpi/applications`, `/dpi/categories`.

## 5. Verifizierter Endpunkt-Mapping

| Tool (Design) | Verifizierter Pfad | Anmerkung |
|---|---|---|
| list_devices | `/sites/{site}/devices` | Liste |
| get_device | `/sites/{site}/devices/{id}` | Detail |
| get_device_status | `/sites/{site}/devices/{id}/statistics/latest` | `cpuUtilizationPct`, `memoryUtilizationPct`, `uptimeSec` |
| list_clients | `/sites/{site}/clients` | `type` WIRED/WIRELESS |
| get_client | `/sites/{site}/clients/{id}` | `uplinkDeviceId`, `access.type` |
| list_networks | `/sites/{site}/networks` | |
| get_network | `/sites/{site}/networks/{id}` | `ipv4Configuration.dhcpConfiguration.*`, `vlanId` |
| get_network_client | `/sites/{site}/networks/{id}/clients` | |
| list_wifi_bssids | `/sites/{site}/wifi/broadcasts` | |
| get_wlan_settings | `/sites/{site}/wifi/broadcasts/{id}` | ⚠️ enthält Klartext-PSK |
| list_acl_rules | `/sites/{site}/acl-rules` | ⚠️ `acl/rules` → 404, korrekt ist `acl-rules` (WP-4, live: 8 Regeln) |
| get_acl_rule | `/sites/{site}/acl-rules/{id}` | `sourceFilter/destinationFilter.{type,networkIds[]}` |
| list_firewall_policies | `/sites/{site}/firewall/policies` | ⚠️ 400 not-configured |
| get_firewall_policy | `/sites/{site}/firewall/policies/{id}` | ⚠️ 400 not-configured |
| list_device_tags | `/sites/{site}/device-tags` | (leer) |
| get_device_tag | `/sites/{site}/device-tags/{id}` | |
| list_sites | `/sites` | |
| get_site | `/sites/{id}` | |
| get_wan_status / get_uplink | `/sites/{site}/wans` | (2 WANs) |
| list_pending_devices | `/pending-devices` | top-level |
| get_system_info | `/info` | top-level |
| list_security_events | TML-Endpunkt | vor Implementierung verifizieren |

Referenz-only-Resources (lesbar, nicht als MVP-Tools): `/countries` (248),
`/dpi/applications` (2112), `/dpi/categories` (35), `/sites/{site}/radius/profiles` (2),
`/sites/{site}/vpn/site-to-site` (0).

## 6. Zone-Based-Firewall nicht konfiguriert (⚠️)

Auf diesem Gateway liefern **beide**:

- `GET /sites/{site}/firewall/zones`
- `GET /sites/{site}/firewall/policies`

**HTTP 400** `api.firewall.zone-based-firewall-not-configured` zurück.

→ Der MVP muss das als `unsupported` behandeln (nicht `error`), gemäß Design §40.
Capability-Check in WP-4, so dass das Tool "Firewall auf diesem Gateway nicht
konfiguriert" meldet statt einer rohen 400. Das ist ein Gateway-Konfigurationszustand,
kein Auth-/Parameterfehler.

## 7. Klartext-Secrets in API-Antworten (⚠️ Redaction)

- `GET /sites/{site}/wifi/broadcasts/{id}` liefert `securityConfiguration.passphrase`
  (den WLAN-PSK) **im Klartext** für `WPA2_PERSONAL` / `WPA3_SAE`-Netzwerke.

→ Muss vom Safety-Layer (WP-5) hart redigiert werden, bevor ein Tool Daten zurückgibt.
Niemals darauf vertrauen, dass die API Secrets versteckt — die client-seitige
Redaction ist die einzige Garantie.

## 8. Versionsdrift (⚠️)

- Installierte Gateway-Firmware: **Network 10.6.101**.
- Neueste **veröffentlichte** OpenAPI-Referenz: **v10.4.57** (einzige öffentliche Version).
- Das Gateway serviert **keine eigene** OpenAPI-Doku.

→ Der Client wird gegen die v10.4.57-Referenz gebaut. Die von den MVP-Tools
verwendeten Feldnamen wurden gegen 10.6.101 verifiziert und stimmten überein.
Bei Feld-Drift: als `unsupported`/Degradation behandeln statt hart zu failen;
die Referenzdatei in `docs/reference/` für Diffs behalten.

## 9. Auth / Read-only-Garantie (⚠️)

- Der MCP-`UNIFI_API_KEY` stammt aus einem **vollen Admin**-Account.
- UniFi-API-Keys haben **keinen eigenen** read-only-Scope; sie erben die Rechte des Erstellers.
- Die "read-only"-Garantie ist daher **nur server-seitig** (dieser Server registriert
  nur Read-Tools). Jeder, der den Key lesen kann, kann die volle API direkt bedienen.

Mitigation (dokumentiert, im Deploy-Repo erzwungen):

- Dedizierten **view-only Admin**-User für den Key verwenden (empfohlen).
- Key in einem SOPS-verschlüsselten Secret (K8s Secret) halten, nie im Image.
- Key nie loggen; in Logs redigieren.

## 10. TLS

- Das Gateway serviert ein **self-signed** Zertifikat (`CN=unifi.local`).
- TLS-Modi (Design §24, ersetzt `UNIFI_VERIFY_TLS`): `UNIFI_TLS_MODE=strict|extract-once|insecure`,
  Default **`extract-once`**.
- `extract-once`: einmaliger unverifizierter Bootstrap-Handshake, Peer-Cert ziehen
  (`getpeercert(binary_form=True)` → PEM), als Session-CA verwenden,
  SHA-256-Fingerprint prominent loggen.
- `strict`: Verifikation gegen `UNIFI_CA_BUNDLE` (im Deploy-Repo als SOPS-Secret).
- `insecure`: Verifikation aus, nur für lokale Entwicklung + prominentes Warnlog.
- `certs/gateway.crt` (dieses Repo) ist nur **lokales Dev-Komfort** — die Produktions-CA
  kommt aus einem SOPS-Secret im Deploy-Repo.
