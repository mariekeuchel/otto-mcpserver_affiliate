# OTTO Affiliate MCP-Server

Dieser MCP-Server (Model Context Protocol) liefert **Umsatz- und Provisionsdaten aus dem OTTO-Partnerprogramm**
an KI-Assistenten wie Claude. Er ist für Publisher gedacht, die in Artikeln auf otto.de verlinken.
Der Server läuft auf **Google Cloud Run**.

## Ergebnis der Recherche: Welche „OTTO-API“?

OTTO hat zwei verschiedene APIs. Nur eine davon ist für Publisher relevant:

| API | Zweck | Für uns relevant? |
|---|---|---|
| **OTTO Market API** (`api.otto.market`) | Für Händler (Seller) auf dem OTTO-Marktplatz: Produkte, Bestellungen, Retouren | ❌ Nein |
| **OTTO Partnerprogramm** (`partnerprogramm.otto.de`) | Affiliate-Programm für Publisher; läuft als „Private Network“ auf **easy.AFFILIATE** von easy.marketing | ✅ Ja |

Die Daten des Partnerprogramms kommen über die **Publisher-API von easy.AFFILIATE**:

```
https://partnerprogramm.otto.de/api/<ACCESS-TOKEN>/publisher/<PUBLISHER-ID>/<METHODE>.<FORMAT>
```

- **Weiterleitung:** `partnerprogramm.otto.de/api/…` leitet per HTTP 302 auf `ottode.easyapi.de/api/…` weiter.
  Bei einer Netzwerk-Allowlist bzw. Firewall müssen **beide** Domains freigegeben sein.
- **Zugangsdaten:** Im Publisher-Account unter *Statistiken → API* (`/statistic-api.do`) stehen der
  Access-Token und die Publisher-ID.
- **Formate:** `csv`, `json`, `xml`, `xls`. Der Server nutzt standardmäßig `csv`.
- **Methoden, die der Server nutzt:**
  - `get-statistic_transactions`: einzelne Transaktionen mit Bestellnummer (`ordertoken`), Zeitpunkt, Status, Umsatz und Provision
  - `get-statistic_daily`: Tagesstatistik (Views, Klicks, Transaktionen, Provision)
  - `get-statistic_advertiser`: Statistik je Advertiser
  - `get-campaigns_admedialist`: Werbemittel
- **Filter (Query-Parameter):**
  - `condition[period][from]` und `condition[period][to]` im Format `TT.MM.JJJJ`
  - `condition[timetype]`: `0` = Erstellung, `1` = Bearbeitung, `2` = Auszahlung
  - `condition[l:processingstate]`: `open`, `confirmed`, `paid`, `canceled` (mehrere kommagetrennt)
  - `condition[dynamicdate]`: z. B. `currentmonth`, `lastmonth`

Quellen: [easy.MARKETING Support – Transaktions-API](https://support.easy-m.de/support/solutions/articles/48001171736-transaktions-api),
[Statistik-API](https://support.easy-m.de/support/solutions/articles/48001157529-statistik-api),
[Publisher-API](https://support.easy-m.de/support/solutions/articles/48001173685-publisher-api),
[OTTO Partnerprogramm](https://www.otto.de/partnerprogramm/en/).

### Geprüftes Datenformat (Live-Test 10/2026)

`get-statistic_transactions` liefert CSV mit `;` als Trennzeichen. Wichtige Spalten:

| Spalte | Bedeutung |
|---|---|
| `criterion` | Transaktions-ID |
| `trackingtime` | Zeitpunkt der Transaktion (z. B. `2026-09-30 23:58:38+02`) |
| `event` | `sale` (Otto-Sale) oder `lead` (Neukundenvergütung) |
| `status` | `0` = offen, `1` = bestätigt, `2` = storniert, `3` = ausgezahlt |
| `provision` | Provision des Publishers |
| `turnover` | gesamter Warenkorbwert |
| `attributed_turnover` | provisionsrelevanter Umsatz. Diesen Wert zeigt die Tagesstatistik als „Umsatz“. |
| `subid` | Kennung aus dem Link, z. B. Artikel oder Platzierung (`exp_1193105_einhellotto_otto`) |
| `referrer` | verlinkende Website (nur die Domain) |
| `admedia_id`, `payoutdate`, `processingdate`, `reason_of_cancellation`, … | weitere Details |

Die Statuscodes und Summen des Servers wurden gegen `get-statistic_daily` geprüft und stimmen exakt
überein. Ein Monat umfasst etwa 19.000 Transaktionen (rund 4 MB). Darum ist eine Abfrage auf maximal
ein Jahr begrenzt, und Cloud Run läuft mit 1 GiB Speicher. Erkennt der Server eine Spalte nicht, lässt
sie sich über `OTTO_FIELD_*` festlegen (siehe `.env.example`).

## Tools des MCP-Servers

Alle Tools lesen nur. Datumsangaben im Format `YYYY-MM-DD`; ohne Angabe gilt der laufende Monat.

| Tool | Beschreibung |
|---|---|
| `get_revenue_summary` | Umsatz, Provision und Anzahl Transaktionen, gesamt und gruppiert nach `status`, `day`, `week`, `month`, `year`, `admedia`, `event` (Sale/Lead), `subid` (Artikel/Link) oder `referrer` (Website). Zeigt auch die **gesicherte** Provision (confirmed + paid) und die **offene** Provision. |
| `get_transactions` | Einzelne Transaktionen mit Paginierung (`limit`/`offset`). Filter: Status, Werbemittel, Bezugsdatum. |
| `get_daily_statistics` | Tagesstatistik (Views, Klicks, Sales). Optional ein relativer Zeitraum wie `lastmonth`. |
| `get_advertiser_statistics` | Statistik je Advertiser/Programm |
| `list_admedia` | Werbemittel samt IDs |
| `query_publisher_api` | Direkter Lesezugriff auf weitere Methoden der Publisher-API, für Sonderfälle |

Beispielfragen an Claude:
- „Wie viel Provision haben wir im September mit OTTO verdient, und wie viel davon ist schon bestätigt?“
- „Zeig mir den OTTO-Umsatz der letzten 6 Monate pro Monat.“
- „Welche Bestellungen sind diese Woche storniert worden?“
- „Welche SubIDs bzw. Artikel haben im September die meiste Provision gebracht?“

## Lokal starten

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # Werte eintragen
set -a && source .env && set +a

# HTTP (wie auf Cloud Run): http://localhost:8080/mcp
otto-affiliate-mcp

# oder stdio (z. B. für Claude Desktop direkt)
otto-affiliate-mcp --transport stdio

pytest   # Tests
```

## Deployment auf Google Cloud Run

Das Skript übernimmt alles: APIs aktivieren, Artifact Registry, Service Account, Secrets im
Secret Manager, Build über Cloud Build und Deploy nach Cloud Run (Region `europe-west3`, Frankfurt).

```bash
PROJECT_ID=mein-gcp-projekt ./deploy/deploy.sh
```

Beim ersten Lauf fragt das Skript nach dem OTTO-Access-Token und der Publisher-ID. Das
`MCP_AUTH_TOKEN` erzeugt es selbst und gibt es einmal aus. Spätere Deployments (z. B. aus CI)
gehen auch so:

```bash
gcloud builds submit --config cloudbuild.yaml
```

Aufbau auf Google Cloud:

```
Claude / MCP-Client ──HTTPS + Bearer-Token──▶ Cloud Run (/mcp, stateless)
                                                   │  Secrets aus Secret Manager
                                                   ▼
                              partnerprogramm.otto.de/api/<token>/publisher/<id>/…
```

- **Stateless Streamable HTTP:** Cloud Run kann beliebig skalieren und braucht keine Session-Affinität.
- **Secrets:** Access-Token, Publisher-ID und MCP-Token liegen nur im Secret Manager, nicht im Image.
- **Logging:** Der Access-Token steht in der Upstream-URL. Darum loggt der Server keine Request-URLs
  von httpx, und Fehlermeldungen werden geschwärzt.
- **Cache:** API-Antworten werden 5 Minuten im Speicher gehalten (`OTTO_CACHE_TTL`).
- **Health-Check:** `GET /healthz`

### Zugriffsschutz

- **Standard (`ACCESS_MODE=public`):** Der Endpunkt ist öffentlich erreichbar, aber jeder Request
  braucht `Authorization: Bearer <MCP_AUTH_TOKEN>` oder `X-API-Key: <MCP_AUTH_TOKEN>`.
- **`ACCESS_MODE=iam`:** Zusätzlich prüft Cloud-Run-IAM den Zugriff (`roles/run.invoker`). Der Client
  schickt dann das Google-ID-Token im `Authorization`-Header und das MCP-Token als `X-API-Key`.

## MCP-Client anbinden

**Claude Code:**

```bash
claude mcp add --transport http otto-affiliate https://<cloud-run-url>/mcp \
  --header "Authorization: Bearer <MCP_AUTH_TOKEN>"
```

**Claude Desktop** (über `mcp-remote`) in `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "otto-affiliate": {
      "command": "npx",
      "args": ["mcp-remote", "https://<cloud-run-url>/mcp", "--header", "Authorization: Bearer ${MCP_TOKEN}"],
      "env": { "MCP_TOKEN": "<MCP_AUTH_TOKEN>" }
    }
  }
}
```

> Hinweis: Custom Connectors in der Web-Oberfläche von claude.ai erwarten OAuth. Ein statisches
> Bearer-Token lässt sich dort nicht eintragen. Für eine teamweite Anbindung über claude.ai müsste
> man OAuth ergänzen, z. B. mit Google Identity oder einem Identity-Aware-Proxy davor.

## Projektstruktur

```
src/otto_affiliate_mcp/
  config.py     Konfiguration aus Umgebungsvariablen
  client.py     Client für die easy.AFFILIATE Publisher-API (URL, Filter, CSV/JSON-Parsing, Cache)
  analytics.py  Spaltenerkennung, Zahlen-/Datumsparsing (deutsches Format), Aggregation
  server.py     MCP-Tools, Bearer-Auth, HTTP-App für Cloud Run
deploy/deploy.sh  Setup und Deployment auf Google Cloud
cloudbuild.yaml   Build/Deploy über Cloud Build
Dockerfile
tests/            Unit- und Integrationstests (Upstream gemockt)
```
