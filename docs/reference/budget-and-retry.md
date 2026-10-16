# Budget, Timeout & Retry-Verhalten (F05 / Design §44)

Jede UniFi-HTTP-Anfrage und jeder Retry ist so begrenzt, dass die
(Worst-Case-)Wall-Clock-Zeit eines einzelnen MCP-Tool-Calls **streng unter dem
externen MCP-/LiteLLM-Tool-Timeout von 60 Sekunden** bleibt. Dadurch ist die
Worst-Case-Latenz eines Tool-Calls planbar und es kann sich kein
Retry-Feedback-Loop bilden, der einen Call über den externen Timeout hinaus
ausdehnt (Retry-Verstärkung / unbekanntes Write-Ergebnis).

## Zahlen (Default-Konfiguration)

| Größe | Wert |
| --- | --- |
| Read-Timeout (pro Versuch) | 15 s |
| Connect-Timeout (pro Versuch) | 5 s |
| Pool-Timeout (pro Versuch) | 5 s |
| Ein einzelner Versuch (connect+read+pool) | 25 s |
| Retry-Versuche nach dem ersten (Default) | 1 (insgesamt max. 2) |
| Worst-Case eines *nicht*-retryenden Reads | 20 s |
| Worst-Case eines retryenden Reads (Default) | 40.5 s |
| Höchste Retry-Zahl, die ins Budget passt | 1 |
| Gesamtbudget pro Tool-Call (Default) | 45 s |
| Reserve zum externen 60-s-Timeout | 15 s |

Der Worst-Case eines retryenden Reads ist die Summe über alle Versuche von
``connect + read`` zuzüglich der Backoff-Obergrenzen zwischen den Versuchen:
`(1+1)·(5+15) + Σ min(0.5·2^i, 2) = 40.5 s`. Der
Pool-Acquire-Wait zählt nicht in die Request-Dauer selbst, ist aber über
`UNIFI_POOL_TIMEOUT_SECONDS` (5 s) pro Versuch gedeckelt und zusätzlich durch das
Tool-Call-Budget zur Laufzeit begrenzt.

## Regeln

- **Ein einzelner Versuch** ist auf ``connect + read + pool`` gedeckelt; der
  Budget-Guard verweigert einen Versuch, dessen Worst-Case nicht mehr in das
  verbleibende Tool-Call-Budget passt — ein Request läuft nie über den
  externen 60-s-Timeout hinaus.
- **Read** (`GET`) wird bei transienten Status (503/5xx) und Transportfehlern
  (Timeout, DNS, Netzwerk, Pool) bis zu `UNIFI_MAX_RETRIES` Mal wiederholt;
  **PUT** nur bei transienten *Status* (das Gateway antwortete, also ist der
  Write nachweislich *nicht* ausgeführt). Exponentieller Backoff mit vollem
  Jitter; jede Backoff- und Pool-Wartezeit respektiert das Budget.
- **Write** (`PUT`) bei *Transport*fehler wird **nie blind wiederholt** — das
  Ergebnis ist unbekannt. Der Aufrufer macht danach genau einen Re-Read
  (frischer `GET`, §29) und klärt per State-Hash, ob der Write ausgeführt
  wurde: ja → Erfolg melden, nein → sauberen Fehler liefern, unklar →
  `write_outcome_unknown` mit dem aktuellen State-Hash (der Client liest nach und entscheidet).
- **Pro Tool-Call** gilt ein hartes Gesamtbudget (`UNIFI_TOOL_CALL_BUDGET_SECONDS`,
  Default 45 s), via `contextvars` isoliert pro Call. Reicht das Budget
  vor einem nächsten Versuch nicht mehr, bricht der Call mit dem strukturierten
  Fehlercode `budget_exhausted` ab — er wird nie vom LiteLLM-Client hart
  über den 60-s-Timeout abgebrochen.
- **Parallelität** ist pro Client auf `UNIFI_MAX_CONCURRENT_REQUESTS`
  (Default 8) begrenzt, damit ein Call (oder ein Burst) den Verbindungspool
  nicht sättigt und so einen degradierenden Gateway nicht weiter verstärkt.
- **Capability-Cache** (TTL, §29) ist *single-flight*: wird ein TTL-Expire gleichzeitig von mehreren Tool-Calls beobachtet, führt nur der erste die bis zu elf Requests teure Neudetection durch, die anderen warten und verwenden das Ergebnis — gegen einen degradierenden Gateway wäre jede eigene Sonde pro Miss genau die Request-Amplifikation, die F05 beseitigen soll. Ein fehlgeschlagenes Redetect wird nicht gecacht: es wirft für alle Warte auf, der nächste Aufrufer versucht es erneut.

## Konfiguration

| Variable | Default | Bedeutung |
| --- | --- | --- |
| `UNIFI_TIMEOUT_SECONDS` | 15 | Read-Timeout pro Versuch (s) |
| `UNIFI_CONNECT_TIMEOUT_SECONDS` | 5 | Connect-Timeout pro Versuch (s) |
| `UNIFI_POOL_TIMEOUT_SECONDS` | 5 | Pool-Acquire-Timeout pro Versuch (s) |
| `UNIFI_MAX_RETRIES` | 1 | Wiederholungen nach dem ersten Versuch (0 = keine) |
| `UNIFI_RETRY_BACKOFF_BASE_SECONDS` | 0.5 | Basis des exponentiellen Backoffs (s) |
| `UNIFI_RETRY_BACKOFF_CAP_SECONDS` | 2 | Obergrenze je Backoff-Stufe (s) |
| `UNIFI_MAX_CONCURRENT_REQUESTS` | 8 | Max. parallele Gateway-Requests pro Client |
| `UNIFI_TOOL_CALL_BUDGET_SECONDS` | 45 | Gesamtbudget pro Tool-Call (s), < externer Timeout |

`Settings` validiert beim Start, dass `UNIFI_TOOL_CALL_BUDGET_SECONDS < 60`
und dass der Worst-Case eines retryenden Reads in das Budget passt; andernfalls
wird beim Laden mit einem actionable Hinweis (inkl. max. passender
Retry-Zahl) abgebrochen. Die Zeitlimits lassen sich über die genannten
Umgebungsvariablen überschreiben.

## Abweichung von Design §44

Design §44 benennt **48 s** als Budget-Deckel. Der Default ist **45 s**: er hält
einen größeren Sicherheitsabstand (15 statt 12 s) zum externen 60-s-Timeout,
damit der Client vor dem externen Disconnect abbrechen kann und der
strukturierte Fehlercode `budget_exhausted` statt einer harten Socket-Trennung
geliefert wird. Beide Werte erfüllen „streng unter 60 s“; die 45-s-Wahl ist
bewusst konservativer. `UNIFI_TOOL_CALL_BUDGET_SECONDS` bleibt konfigurierbar.

