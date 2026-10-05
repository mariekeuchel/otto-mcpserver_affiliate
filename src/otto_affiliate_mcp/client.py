"""Client für die Publisher-API des OTTO-Partnerprogramms.

Das OTTO-Partnerprogramm (partnerprogramm.otto.de) läuft auf der Plattform
easy.AFFILIATE von easy.marketing. Die Publisher-API wird so aufgerufen:

    https://partnerprogramm.otto.de/api/<ACCESS-TOKEN>/publisher/<PUBLISHER-ID>/<METHODE>.<FORMAT>

Beispiele für Methoden:
    get-statistic_transactions   Transaktionen (Sales/Leads) inkl. Umsatz & Provision
    get-statistic_daily          Tagesstatistik (Views, Klicks, Transaktionen, Provision)
    get-statistic_advertiser     Statistik nach Advertiser
    get-campaigns_admedialist    Werbemittel

Filter werden als Query-Parameter im Format ``condition[...]`` übergeben, z. B.
``condition[period][from]=01.09.2026``. Den Access-Token und die Publisher-ID
findet man im Publisher-Account unter "Statistiken -> API" (/statistic-api.do).
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import time
from collections.abc import Mapping
from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from .config import Settings

METHOD_PATTERN = re.compile(r"^get-[a-z0-9]+(?:_[a-z0-9]+)*$")

Row = dict[str, Any]


class OttoApiError(ToolError):
    """Fehler beim Aufruf der OTTO-Publisher-API (wird dem Modell als Tool-Fehler angezeigt)."""


class _RedactTokenFilter(logging.Filter):
    """Entfernt den Access-Token aus Log-Einträgen (httpx loggt die vollständige Request-URL)."""

    def __init__(self, token: str):
        super().__init__()
        self.token = token

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if self.token in message:
            record.msg = message.replace(self.token, "***")
            record.args = None
        return True


def _install_log_redaction(token: str | None) -> None:
    if not token:
        return
    for name in ("httpx", "httpcore"):
        log = logging.getLogger(name)
        if not any(isinstance(f, _RedactTokenFilter) and f.token == token for f in log.filters):
            log.addFilter(_RedactTokenFilter(token))


class OttoAffiliateClient:
    def __init__(self, settings: Settings, http_client: httpx.AsyncClient | None = None):
        self._settings = settings
        _install_log_redaction(settings.access_token)
        self._http = http_client or httpx.AsyncClient(
            timeout=settings.timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": "otto-affiliate-mcp/1.0"},
        )
        self._cache: dict[str, tuple[float, list[Row]]] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    def _build_url(self, method: str, fmt: str) -> str:
        s = self._settings
        if not s.is_configured:
            raise OttoApiError(
                "OTTO_API_ACCESS_TOKEN und OTTO_PUBLISHER_ID sind nicht gesetzt. "
                "Beide Werte stehen im Publisher-Account unter 'Statistiken -> API'."
            )
        return f"{s.base_url}/api/{s.access_token}/publisher/{s.publisher_id}/{method}.{fmt}"

    def _redact(self, text: str) -> str:
        token = self._settings.access_token
        return text.replace(token, "***") if token else text

    async def fetch(
        self,
        method: str,
        conditions: Mapping[str, Any] | None = None,
        *,
        use_cache: bool = True,
    ) -> list[Row]:
        """Ruft eine API-Methode auf und gibt die Antwort als Liste von Zeilen zurück."""
        if not METHOD_PATTERN.match(method):
            raise OttoApiError(f"Ungültiger Methodenname: {method!r}")

        params = build_condition_params(conditions or {})
        fmt = self._settings.api_format
        cache_key = json.dumps([method, fmt, sorted(params.items())])
        now = time.monotonic()
        if use_cache and self._settings.cache_ttl_seconds > 0:
            hit = self._cache.get(cache_key)
            if hit and now - hit[0] < self._settings.cache_ttl_seconds:
                return hit[1]

        url = self._build_url(method, fmt)
        try:
            response = await self._http.get(url, params=params)
        except httpx.HTTPError as exc:
            raise OttoApiError(self._redact(f"Verbindungsfehler zur OTTO-API: {exc}")) from None

        if response.status_code >= 400:
            snippet = self._redact(response.text[:300])
            raise OttoApiError(f"OTTO-API antwortete mit HTTP {response.status_code}: {snippet}")

        body = decode_body(response.content)
        rows = parse_json(body) if fmt == "json" else parse_csv(body)

        if use_cache and self._settings.cache_ttl_seconds > 0:
            self._cache[cache_key] = (now, rows)
            if len(self._cache) > 16:  # Antworten können mehrere MB groß sein
                oldest = min(self._cache, key=lambda k: self._cache[k][0])
                self._cache.pop(oldest, None)
        return rows


def build_condition_params(conditions: Mapping[str, Any]) -> dict[str, str]:
    """Wandelt verschachtelte Filter in ``condition[...]``-Query-Parameter um.

    ``{"period": {"from": "01.09.2026"}, "l:processingstate": ["open", "confirmed"]}``
    wird zu ``condition[period][from]=01.09.2026`` und
    ``condition[l:processingstate]=open,confirmed``.
    Schlüssel, die bereits mit ``condition[`` beginnen, werden unverändert übernommen.
    """
    params: dict[str, str] = {}

    def walk(prefix: str, value: Any) -> None:
        if value is None:
            return
        if isinstance(value, Mapping):
            for key, sub in value.items():
                walk(f"{prefix}[{key}]", sub)
        elif isinstance(value, (list, tuple, set)):
            items = [str(v) for v in value if v is not None and str(v) != ""]
            if items:
                params[prefix] = ",".join(items)
        elif isinstance(value, bool):
            params[prefix] = "1" if value else "0"
        else:
            params[prefix] = str(value)

    for key, value in conditions.items():
        if key.startswith("condition["):
            walk(key, value)
        else:
            walk(f"condition[{key}]", value)
    return params


def decode_body(content: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def parse_csv(text: str) -> list[Row]:
    text = text.strip()
    if not text:
        return []
    if text[0] in "[{":  # manche Endpunkte liefern trotz .csv JSON oder Fehlermeldungen
        try:
            return parse_json(text)
        except OttoApiError:
            pass
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        first_line = sample.splitlines()[0]
        delimiter = max(";,\t|", key=first_line.count)
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    rows: list[Row] = []
    for raw in reader:
        row = {(k or "").strip(): (v.strip() if isinstance(v, str) else v) for k, v in raw.items() if k}
        if any(v not in (None, "") for v in row.values()):
            rows.append(row)
    return rows


def parse_json(text: str) -> list[Row]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise OttoApiError(f"Antwort ist kein gültiges JSON: {exc}") from None
    return _extract_rows(data)


def _extract_rows(data: Any) -> list[Row]:
    if isinstance(data, list):
        return [r if isinstance(r, dict) else {"value": r} for r in data]
    if isinstance(data, dict):
        for key in ("error", "errors", "message"):
            if key in data and len(data) <= 2 and not any(isinstance(v, list) for v in data.values()):
                raise OttoApiError(f"OTTO-API meldet einen Fehler: {data[key]}")
        for key in ("data", "items", "rows", "transactions", "result", "results", "statistic", "statistics"):
            if key in data:
                return _extract_rows(data[key])
        values = list(data.values())
        if values and all(isinstance(v, dict) for v in values):
            return [{"_key": k, **v} for k, v in data.items()]
        list_values = [v for v in values if isinstance(v, list)]
        if len(list_values) == 1:
            return _extract_rows(list_values[0])
        return [data]
    return [{"value": data}]
