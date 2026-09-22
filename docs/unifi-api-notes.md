# UniFi Network API — Live Discovery Notes

Live verifiziert gegen das lokale Cloud Gateway (**UCG Ultra**, Network **10.6.101**).
Referenz-OpenAPI: **v10.6.106** (live vom Gateway gezogen, 2026-09-22) unter
`docs/reference/network-openapi-10.6.106.json` (44 Pfade).

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

WP-7 (live, 2026-09-17):

- **Array-Filter:** `features.contains('switching')` → OK (6/6 Devices),
  `features.contains('gateway')` → 0/0 (Syntax gültig).
- **Enum-Prüfung:** `state.eq` validiert den Wert **nicht** — `state.eq('BOGUS')`
  liefert 0/0 statt 400. Tool-seitige Allowlists sind trotzdem Pflicht.
- `name.like('')` → 0/0 (leerer Term matcht nichts) → leere Suche wird
  tool-seitig weggelassen.

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
| list_clients | `/sites/{site}/clients` | `type` WIRED/WIRELESS; Filter nur `type/ipAddress/macAddress/id/connectedAt` (siehe §5b) |
| get_client | `/sites/{site}/clients/{id}` | `uplinkDeviceId`, `access.type`; List- und Detail-Objekt identisch |
| list_networks | `/sites/{site}/networks` | 15; `ipv4Configuration` **nur im Detail** |
| get_network | `/sites/{site}/networks/{id}` | `ipv4Configuration.dhcpConfiguration.*`, `vlanId` |
| get_network_client | `/sites/{site}/networks/{id}/clients` | ⚠️ existiert **nicht** auf 10.6.101; `/networks/{id}/references` → 500 `api.unexpected-error` |
| list_wifi_bssids | `/sites/{site}/wifi/broadcasts` | |
| get_wlan_settings | `/sites/{site}/wifi/broadcasts/{id}` | ⚠️ enthält Klartext-PSK |
| list_acl_rules | `/sites/{site}/acl-rules` | ⚠️ `acl/rules` → 404, korrekt ist `acl-rules` (WP-4, live: 8 Regeln) |
| get_acl_rule | `/sites/{site}/acl-rules/{id}` | `sourceFilter/destinationFilter.{type,networkIds[]}`; **Action-Enum `ALLOW`/`BLOCK` (kein `DENY`!)**, nested-Filter `sourceFilter.type.eq(...)` + `enabled.eq(true/false)` ok (WP-10) |
| get_acl_rule_ordering | `/sites/{site}/acl-rules/ordering` | 200, **keine Query-Parameter**; `{orderedAclRuleIds[]}`; Listenposition == `index` der Regeln (WP-13b) |
| list_dns_policies | `/sites/{site}/dns/policies` | 6 Policies (alle `A_RECORD`); Filter siehe §5f (WP-13b) |
| get_dns_policy | `/sites/{site}/dns/policies/{id}` | 200; typspezifische Felder (§5f) |
| list_traffic_matching_lists | `/sites/{site}/traffic-matching-lists` | 17; `{type: "IPV4_ADDRESSES"\|"PORTS", id, name, items[{type,value}]}` |
| list_firewall_policies | `/sites/{site}/firewall/policies` | 200 (seit 2026-09-21 ZBF konfiguriert); ~350 Policies; `id` **fehlt bei abgeleiteten System-Policies**; Filter siehe §5e |
| get_firewall_policy | `/sites/{site}/firewall/policies/{id}` | 200; Detail zusätzlich `description` |
| get_firewall_policy_ordering | `/sites/{site}/firewall/policies/ordering?sourceFirewallZoneId=…&destinationFirewallZoneId=…` | 200 nur mit **Zonenpaar** (beide Params, sonst 400); `{orderedFirewallPolicyIds{beforeSystemDefined[], afterSystemDefined[]}}` |
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
`/sites/{site}/wans` (2), `/sites/{site}/vpn/servers` (0), `/sites/{site}/device-tags` (0),
`/sites/{site}/vpn/site-to-site-tunnels` (0).
⚠️ **Pfad-Korrektur (WP-10, live):** korrekt ist `vpn/site-to-site-tunnels` — der
früher notierte Pfad `vpn/site-to-site` liefert 404.

## 5a. Devices — Felder und Feature-Drift (WP-7, live 2026-09-17)

- List-Item (`GET /sites/{site}/devices`): `id, name, model, state, features[],
  firmwareVersion, firmwareUpdatable, supported, ipAddress, macAddress,
  interfaces[]`. `state` ist UPPERCASE (hier: alle 9 Geräte `ONLINE`).
- `features` (List, API-10.4.57-Enum): `switching`, `accessPoint`, `gateway`.
  Geräte melden **nur die Features, die sie tatsächlich anbieten**.
- ⚠️ **UCG Ultra meldet nur `features: ["switching"]`** — es hat kein
  `gateway`-Feature. `device_type=gateway` ist also auf dieser Deployment
  leer (gültiger Filter, 0 Treffer). Gateway-Modell erkennen stattdessen
  über `search`/`model` (z. B. `UCG Ultra`). In der `list_devices`-
  Description dokumentiert.
- ⚠️ **Schema-Drift List vs. Detail:** Im Detail-Objekt
  (`GET /sites/{site}/devices/{id}`) ist `features` ein **Dict**
  (`{"switching": {"lags": []}}`), in der Liste eine Liste. Normalisierung
  (WP-5) behandelt beides; Detail-Tools (WP-7b/WP-8) müssen das kennen.
  Ebenso `interfaces`: im **List**-Endpoint nur ein String-Liste mit
  Kategorienamen (z. B. `["ports"]`, bei APs `["radios"]`), im Detail
  vollständige Interface-Objekte. Die List-Fixture (`devices.json`) ist
  auf die List-Form geschnitten.
- `/pending-devices` (top-level): OK, liefert Envelope, hier 0 Einträge.

## 5b. Clients — Felder und Filter (WP-8, live 2026-09-17)

- List- **und** Detail-Objekt sind **identisch geformt**:
  `id, name, type (WIRED|WIRELESS), ipAddress, macAddress, uplinkDeviceId,
  connectedAt, access{type:"DEFAULT"}`.
- ⚠️ **Kein `hostname`-Feld** (der Name steht in `name`), **kein `networkId`,
  kein `state`, keine Port-/SSID-Info** — weder im Client noch im Device-Objekt
  (OpenAPI 10.4.57 bestätigt). `inspect_client_path.attachment.port` ist daher
  immer null; die Network-Zuordnung existiert live nicht.
- `uplinkDeviceId` ist das Attachment-Feld bei **wired und wireless**
  (kein `connectedTo`, kein separates Wireless-Feld).
- **Filter an `/clients`** (verifiziert + Treffer): `type.eq('WIRELESS'|'WIRED')`
  (22/20), `type.in('WIRED','WIRELESS')` (42), `ipAddress.eq('...')` (1),
  `macAddress.eq('...')` (1), `or(...)` **wird akzeptiert** (21), `id.eq(<UUID
  ohne Quotes>)` (1; mit Quotes → 400 „expected UUID"), `connectedAt.gt(<ts ohne
  Quotes>)` (42), `access.type.eq('DEFAULT')` (42).
- **Nicht filterbar** (400 „unknown filter property" / „'like' not allowed"):
  `name.like`, `hostname*`, `networkId.eq`, `uplinkDeviceId.eq`, `state.*`,
  `isGuest.*`, `ipAddress.like`, `macAddress.like`.
  → `list_clients` filtert `network_id`/`connected_to_device_id`/`search`
  client-seitig (siehe Tool-Description).
- `GET /networks/{id}/clients` existiert nicht; `GET /networks/{id}/references`
  → 500 `api.unexpected-error`.

## 5c. Networks & WiFi — Felder und PSK (WP-9, live 2026-09-17)

- Networks: **15**. List-Item: `id, name, enabled, vlanId, management
  (GATEWAY|SWITCH|UNMANAGED), default, metadata{origin,configurable}`
  (+`deviceId` bei switch-lokalen). **Kein `type`-Feld, kein
  `ipv4Configuration` in der Liste** (nur Detail).
- Network-Detail: zusätzlich `ipv4Configuration{hostIpAddress, prefixLength,
  dhcpConfiguration{mode, ipAddressRange{start,stop}, leaseTimeSeconds,
  domainName, ...}}`, `isolationEnabled`, `cellularBackupEnabled`,
  `internetAccessEnabled`, `mdnsForwardingEnabled`.
- WiFi-Broadcasts: **2**. List-Item: `id, name` (= SSID), `enabled, type
  (STANDARD|IOT_OPTIMIZED), network{type,networkId}, securityConfiguration{type}`
  (+ optional `broadcastingFrequenciesGHz`). **Kein PSK in der Liste.**
- ⚠️ **PSK-Feldpfad exakt: `securityConfiguration.passphrase`** — nur im
  Detail-Objekt (`GET /wifi/broadcasts/{id}`), bei WPA2/WPA3_PERSONAL im
  Klartext. Redaction-Match über den verbotenen exakten Namen `passphrase`
  (greift auch auf `psk`/`*psk`-Suffixe) → `[REDACTED]`.

## 5d. ACL / Traffic / Referenz (WP-10, live 2026-09-17)

- ACL: **8** Regeln. Shape: `{type: "IPV4", id (UUID), enabled (bool), name,
  action, index, sourceFilter, destinationFilter, metadata}`.
- ⚠️ **Action-Enum ist `ALLOW`/`BLOCK` (kein `DENY`!)** — case-sensitive.
- **Serverseitig filterbar** (live): `action.eq('ALLOW')` (2/8),
  `enabled.eq(true|false)` (4/8, unquoted), **nested**
  `sourceFilter.type.eq('NETWORKS')` (7/8),
  `sourceFilter.type.eq('IP_ADDRESSES_OR_SUBNETS')` (1/8).
- Endpoint-Filter-`type`-Werte (OpenAPI + live): `NETWORKS` (`networkIds[]`),
  `IP_ADDRESSES_OR_SUBNETS` (`ipAddressesOrSubnets[]`), `PORTS`
  (`portFilter[]`), `MAC_ADDRESSES` (MAC-Regeln).
- TMLs: **17**. Shape: `{type: "IPV4_ADDRESSES"|"PORTS", id, name,
  items: [{type: "SUBNET"|"PORT_NUMBER", value}]}`.
- Referenz-Endpunkte (Anzahlen + Shape): `/countries` 248 `{code,name}`;
  `/dpi/applications` 2112 `{id:int,name}`; `/dpi/categories` 35 `{id:int,name}`;
  `/sites/{site}/radius/profiles` 2 `{id,name,metadata}`; `/sites/{site}/wans` 2
  `{id,name}`; `/sites/{site}/vpn/servers` 0; `/sites/{site}/device-tags` 0;
  `/sites/{site}/vpn/site-to-site-tunnels` 0.
- ⚠️ **Pfad-Korrektur:** `vpn/site-to-site-tunnels` (nicht `vpn/site-to-site` → 404).

## 5e. Firewall Zones & Policies (WP-13, live 2026-09-21, 10.6.106)

Seit 2026-09-21 ist auf diesem Gateway die Zone-Based-Firewall konfiguriert
(Capability-Check meldet `firewall: ok`).

- **Zonen: 13** — `BenchNet, Hotspot, Dmz, External, Vpn, Internal,
  Management, Gameserver, IoT, Monitoring, Services, VoIP, Gateway`. Shape:
  `{id, name, networkIds[], metadata{origin}}`. System-Zonen
  (`Hotspot`, `Management`, `Gateway`) haben `networkIds: []`.
- **Policies: ~354** — Count driftet leicht zwischen Aufrufen
  (350 → 352 → 354), abgeleitete Policies werden neu berechnet.
  List-Item: `{enabled, name, index, action{type, allowReturnTraffic?},
  source{zoneId, trafficFilter?}, destination{zoneId, trafficFilter?},
  ipProtocolScope{ipVersion, protocolFilter?}, connectionStateFilter?,
  loggingEnabled, metadata{origin}}`. Detail zusätzlich: `id`, `description`.
- ⚠️ **`id` fehlt bei ~13 % der List-Items** (OpenAPI 10.4.57 gibt `id` als
  required an): abgeleitete System-Policies (z. B.
  `000-0-ALLOW-ESTABLISHED-RELATED`) werden vom Gateway berechnet und
  haben keine ID → nicht per `GET /policies/{id}` abrufbar. Tools dürfen
  das fehlende `id` vertragen.
- **Action-Enum: `ALLOW`/`BLOCK`/`REJECT`** (diskriminiert über `type`);
  `allowReturnTraffic` nur bei `ALLOW`. `connectionStateFilter`:
  `NEW|INVALID|ESTABLISHED|RELATED`.
- **Serverseitig filterbar (live verifiziert):** `name.like('…')`,
  `metadata.origin.eq('USER_DEFINED'|'SYSTEM_DEFINED')`,
  `source.zoneId.eq(<uuid>)`, `destination.zoneId.eq(<uuid>)`.
  UUIDs **ohne Quotes** (mit Quotes → 400 „expected UUID, actual STRING").
  **Nicht filterbar** (400 `api.request.invalid-filter`): `action.*`,
  `enabled.*` — sie stehen auch nicht in der OpenAPI-10.4.57-Filtertabelle
  (dort nur `id`, `name`, `source.zoneId`, `destination.zoneId`,
  `metadata.origin`).
- **Ordering-Endpunkt:** `GET /firewall/policies/ordering` verlangt
  **beide** Parameter `sourceFirewallZoneId` + `destinationFirewallZoneId`
  (je 400, wenn einer fehlt); liefert die Evaluationsreihenfolge des
  Zonenpaars als ID-Listen `beforeSystemDefined` / `afterSystemDefined`.
  Die Reihenfolge ist zudem an `index` der Policies ablesbar
  (niedriger = zuerst).
- `trafficFilter`-Typen (diskriminiert über `type`): Source u. a.
  `IP_ADDRESS` (+MAC, +Ports), `NETWORK`, `MAC_ADDRESS`, `PORT`, `REGION`,
  `SITE_TO_SITE_VPN_TUNNEL`, `VPN_SERVER`, `IPV6_IID`; Destination u. a.
  `APPLICATION`, `APPLICATION_CATEGORY`, `DOMAIN`, `IP_ADDRESS`, `NETWORK`,
  `PORT`. IP-Filter referenzieren optional Traffic Matching Lists
  (`trafficMatchingListId`); Port-Filter: `PORTS` (Werte/Range) oder
  `TRAFFIC_MATCHING_LIST`.

## 5f. Devices-Detail/Statistics, DNS-Policies, ACL-Ordering, Pending-Devices (WP-13b, live 2026-09-22, 10.6.106)

- **Device-Detail** `GET /sites/{site}/devices/{id}`: 200. List-Item-Felder
  plus `configurationId`, `adoptedAt`, `provisionedAt`, `uplink{deviceId}`,
  `features` als **Objekt** (z. B. `{"switching": {"lags": []}}`, im
  Gegensatz zum List-Item dort Array), `interfaces` (z. B. `radios[]` mit
  `channel/channelWidthMHz/frequencyGHz/wlanStandard`, bei Switches
  `ports[]`) und `metadata{origin}`. 404 → `not-found`.
- **Device-Statistics** `GET /sites/{site}/devices/{id}/statistics/latest`:
  200, **kein Pagination-Envelope** (Plattdict). Felder:
  `uptimeSec`, `lastHeartbeatAt`, `nextHeartbeatAt`,
  `loadAverage1Min/5Min/15Min`, `cpuUtilizationPct`,
  `memoryUtilizationPct`, `uplink{txRateBps,rxRateBps}`, `interfaces`
  (pro Interface/Radio Raten). Kein Filter, keine Query-Parameter.
- **Pending-Devices** `GET /pending-devices`: **Top-Level, ohne `siteId`**
  (site-scoped Pfad gibt 404). 200, Pagination-Envelope. Item:
  `macAddress, ipAddress, model, state` (`PENDING_ADOPTION`), `supported`,
  `firmwareVersion`, `firmwareUpdatable`, `features[]`,
  `adoptionTargetSiteIds[]`.
- **DNS-Policies** `GET /sites/{site}/dns/policies`: 200 (6 Policies, hier
  alle `A_RECORD`). Item ist ein Diskriminierungs-Union über `type`:
  `A_RECORD`/`AAAA_RECORD`/`CNAME_RECORD`/`MX_RECORD`/`TXT_RECORD`/
  `SRV_RECORD`/`FORWARD_DOMAIN`. Gemeinsame Felder: `type, id, enabled,
  domain, metadata{origin}`; typspezifisch u. a. A: `ipv4Address,
  ttlSeconds`, CNAME: `targetDomain, ttlSeconds`, FORWARD_DOMAIN:
  `ipAddress` (Forwarder). Detail `GET …/dns/policies/{id}`: 200, gleiche
  Felder.
  - **Serverseitig filterbar (live verifiziert):** `type.eq('A_RECORD')`,
    `domain.like('*…*')`, `enabled.eq(true|false)` (auch kombiniert mit
    `and(…)`).
  - **Nicht filterbar** (400 `api.request.invalid-filter`):
    `metadata.origin` — im Gegensatz zu anderen Ressourcen.
- **ACL-Ordering** `GET /sites/{site}/acl-rules/ordering`: 200, **keine
  Query-Parameter** (im Gegensatz zum Zonenpaar-Ordering der
  Firewall-Policies). `{orderedAclRuleIds[]}`; die Listenposition entspricht
  dem `index` der Regeln (niedriger = zuerst).
- **Netzwerk-Referenzen** `GET /sites/{site}/networks/{id}/references`:
  500 `api.unexpected-error` (Gateway-Bug, auch in 10.6.106) → **nicht**
  implementiert (siehe §5 `get_network_client`).
- **Optional, nicht implementiert (200, hier 0 Einträge):**
  `GET /sites/{site}/hotspot/vouchers`, `GET /sites/{site}/switching/…`
  (Stacks, Ports, VLANs).

## 6. Zone-Based-Firewall nicht konfiguriert (⚠️, Historie)

Bis 2026-09-21 war auf diesem Gateway die ZBF **nicht** konfiguriert:
**beide** Endpunkte

- `GET /sites/{site}/firewall/zones`
- `GET /sites/{site}/firewall/policies`

lieferten **HTTP 400** `api.firewall.zone-based-firewall-not-configured`
zurück. Das Verhalten gilt weiterhin für Gateways ohne ZBF und wird als
`unsupported` behandelt (nicht `error`), gemäß Design §40. Capability-Check
in WP-4, so dass das Tool "Firewall auf diesem Gateway nicht konfiguriert"
meldet statt einer rohen 400. Das ist ein Gateway-Konfigurationszustand,
kein Auth-/Parameterfehler. Seit 2026-09-21 ist die ZBF hier konfiguriert
(§5e); die `unsupported`-Pfade bleiben getestet (Unit/E2E mit 400-Fixtures).

## 7. Klartext-Secrets in API-Antworten (⚠️ Redaction)

- `GET /sites/{site}/wifi/broadcasts/{id}` liefert `securityConfiguration.passphrase`
  (den WLAN-PSK) **im Klartext** für `WPA2_PERSONAL` / `WPA3_SAE`-Netzwerke.

→ Muss vom Safety-Layer (WP-5) hart redigiert werden, bevor ein Tool Daten zurückgibt.
Niemals darauf vertrauen, dass die API Secrets versteckt — die client-seitige
Redaction ist die einzige Garantie.

## 8. Versionsdrift (gelöst, 2026-09-22)

- Installierte Gateway-Firmware: **Network 10.6.106**.
- Das Gateway serviert **seine eigene** OpenAPI-Doku (authenticated):
  `GET /proxy/network/api-docs/integration.json` (Header `X-API-Key`, kein
  eigener Base-Path — liegt neben `/proxy/network/integration/v1`).
- Referenz im Repo: `docs/reference/network-openapi-10.6.106.json` (live
  gezogen). Ältere Pfade: `developer.ui.com/network/<version>` bzw.
  `apidoc-cdn.ui.com/network/<version>/integration.json`.
- **Diff 10.4.57 → 10.6.106:** keine neuen/entfallenen Pfade oder Methoden
  (44 Pfade identisch); einziges Schema-Delta:
  `IntegrationSwitchStackMemberDto` → `IntegrationSwitchStackLagMemberDto`
  + `IntegrationSwitchStackUnitDto` (Switch-Stack-Member-Umbenennung).

→ Der Client wird gegen die 10.6.106-Referenz entwickelt. Bei Feld-Drift:
als `unsupported`/Degradation behandeln statt hart zu failen; die
Referenzdatei in `docs/reference/` für Diffs behalten.

Referenz aktualisieren (nach Gateway-Update):

```bash
source .env
curl -sk -H "X-API-Key: $UNIFI_API_KEY" \
  "https://${UNIFI_BASE_URL#https://}/proxy/network/api-docs/integration.json" \
  -o docs/reference/network-openapi-<version>.json
```

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
