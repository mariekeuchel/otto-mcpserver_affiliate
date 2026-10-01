"""MCP-Server für Umsatz- und Provisionsdaten aus dem OTTO-Partnerprogramm."""

from __future__ import annotations

import argparse
import hmac
import logging
from datetime import date, datetime, timedelta
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from .analytics import VALID_STATUSES, detect_fields, summarize
from .client import OttoAffiliateClient, OttoApiError
from .config import Settings

logger = logging.getLogger("otto_affiliate_mcp")

DATE_TYPES = {"created": 0, "processed": 1, "paid": 2}
DateType = Literal["created", "processed", "paid"]
Status = Literal["open", "confirmed", "paid", "canceled"]
GroupBy = Literal["status", "day", "week", "month", "year", "admedia", "none"]
DynamicDate = Literal["today", "yesterday", "currentweek", "lastweek", "currentmonth", "lastmonth", "currentyear"]

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True)

INSTRUCTIONS = """\
Dieser Server liefert Affiliate-Daten des Publishers aus dem OTTO-Partnerprogramm
(partnerprogramm.otto.de, Plattform easy.AFFILIATE).

- Für Umsatz-/Provisionsfragen zuerst `get_revenue_summary` nutzen (aggregiert).
- Für einzelne Bestellungen `get_transactions`.
- Für Klicks/Views/Conversion-Raten pro Tag `get_daily_statistics`.
- Datumsangaben immer im Format YYYY-MM-DD. Ohne Angabe gilt der laufende Monat.
- Transaktionsstatus: open (offen), confirmed (bestätigt), paid (ausgezahlt), canceled (storniert).
  Nur confirmed + paid sind sicherer Umsatz; open kann noch storniert werden.
- Beträge sind in EUR.
"""


def _parse_iso(value: str | None, name: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise OttoApiError(f"{name} muss im Format YYYY-MM-DD angegeben werden, erhalten: {value!r}") from None


def resolve_period(date_from: str | None, date_to: str | None, today: date | None = None) -> tuple[date, date]:
    """Standardzeitraum: laufender Monat bis heute (deutsche Zeit – Cloud Run läuft in UTC)."""
    today = today or datetime.now(ZoneInfo("Europe/Berlin")).date()
    start = _parse_iso(date_from, "date_from")
    end = _parse_iso(date_to, "date_to")
    if start is None and end is None:
        start, end = today.replace(day=1), today
    elif start is None:
        start = end.replace(day=1)  # type: ignore[union-attr]
    elif end is None:
        end = max(today, start)
    if start > end:  # type: ignore[operator]
        raise OttoApiError("date_from liegt nach date_to.")
    if (end - start) > timedelta(days=731):  # type: ignore[operator]
        raise OttoApiError("Zeitraum ist zu groß (maximal 2 Jahre pro Abfrage).")
    return start, end  # type: ignore[return-value]


def _fmt(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def transaction_conditions(
    start: date,
    end: date,
    date_type: str = "created",
    statuses: list[str] | None = None,
) -> dict[str, Any]:
    conditions: dict[str, Any] = {
        "period": {"from": _fmt(start), "to": _fmt(end)},
        "timetype": DATE_TYPES[date_type],
    }
    if statuses:
        invalid = [s for s in statuses if s not in VALID_STATUSES]
        if invalid:
            raise OttoApiError(f"Ungültiger Status: {invalid}. Erlaubt: {list(VALID_STATUSES)}")
        conditions["l:processingstate"] = statuses
    return conditions


def filter_admedia(rows: list[dict[str, Any]], admedia_id: str | None, settings: Settings) -> list[dict[str, Any]]:
    """Filtert lokal nach Werbemittel – unabhängig davon, ob die API einen passenden Filter kennt."""
    if not admedia_id:
        return rows
    column = detect_fields(rows, settings.field_overrides)["admedia"]
    if column is None:
        raise OttoApiError("Werbemittel-Spalte nicht erkannt – per OTTO_FIELD_ADMEDIA konfigurieren.")
    return [r for r in rows if str(r.get(column, "")).strip() == admedia_id.strip()]


def build_server(settings: Settings, client: OttoAffiliateClient | None = None) -> MCPServer:
    state: dict[str, OttoAffiliateClient] = {}

    def get_client() -> OttoAffiliateClient:
        if client is not None:
            return client
        if "client" not in state:
            state["client"] = OttoAffiliateClient(settings)
        return state["client"]

    mcp = MCPServer(
        name="otto-affiliate",
        title="OTTO Partnerprogramm (Affiliate)",
        instructions=INSTRUCTIONS,
        version="1.0.0",
    )

    @mcp.tool(annotations=READ_ONLY)
    async def get_revenue_summary(
        date_from: Annotated[str | None, Field(description="Startdatum YYYY-MM-DD (Standard: Monatsanfang)")] = None,
        date_to: Annotated[str | None, Field(description="Enddatum YYYY-MM-DD (Standard: heute)")] = None,
        group_by: Annotated[GroupBy, Field(description="Gruppierung der Ergebnisse")] = "status",
        date_type: Annotated[
            DateType, Field(description="Bezugsdatum: created=Bestelldatum, processed=Bearbeitung, paid=Auszahlung")
        ] = "created",
        status: Annotated[list[Status] | None, Field(description="Nur diese Status berücksichtigen")] = None,
        admedia_id: Annotated[str | None, Field(description="Nur Transaktionen dieses Werbemittels")] = None,
    ) -> dict[str, Any]:
        """Umsatz, Provision und Anzahl der Transaktionen für einen Zeitraum – gesamt und gruppiert
        (nach Status, Tag, Woche, Monat, Jahr oder Werbemittel). Enthält gesicherte (confirmed+paid)
        und offene Provision."""
        start, end = resolve_period(date_from, date_to)
        rows = await get_client().fetch(
            "get-statistic_transactions", transaction_conditions(start, end, date_type, status)
        )
        rows = filter_admedia(rows, admedia_id, settings)
        result = summarize(rows, group_by=group_by, overrides=settings.field_overrides)
        result["period"] = {"from": start.isoformat(), "to": end.isoformat(), "date_type": date_type}
        result["currency"] = "EUR"
        return result

    @mcp.tool(annotations=READ_ONLY)
    async def get_transactions(
        date_from: Annotated[str | None, Field(description="Startdatum YYYY-MM-DD (Standard: Monatsanfang)")] = None,
        date_to: Annotated[str | None, Field(description="Enddatum YYYY-MM-DD (Standard: heute)")] = None,
        date_type: Annotated[
            DateType, Field(description="Bezugsdatum: created=Bestelldatum, processed=Bearbeitung, paid=Auszahlung")
        ] = "created",
        status: Annotated[list[Status] | None, Field(description="Nur diese Status, z. B. ['open','confirmed']")] = None,
        admedia_id: Annotated[str | None, Field(description="Nur Transaktionen dieses Werbemittels")] = None,
        limit: Annotated[int, Field(ge=1, le=1000, description="Max. Anzahl zurückgegebener Zeilen")] = 100,
        offset: Annotated[int, Field(ge=0, description="Zeilen überspringen (Paginierung)")] = 0,
    ) -> dict[str, Any]:
        """Einzelne Transaktionen (Bestellungen) aus dem OTTO-Partnerprogramm mit Bestellnummer,
        Zeitpunkt, Status, Warenkorbwert (Umsatz), Provision, Werbemittel und SubID."""
        start, end = resolve_period(date_from, date_to)
        rows = await get_client().fetch(
            "get-statistic_transactions", transaction_conditions(start, end, date_type, status)
        )
        rows = filter_admedia(rows, admedia_id, settings)
        page = rows[offset : offset + limit]
        return {
            "period": {"from": start.isoformat(), "to": end.isoformat(), "date_type": date_type},
            "total_rows": len(rows),
            "returned": len(page),
            "offset": offset,
            "has_more": offset + len(page) < len(rows),
            "detected_fields": detect_fields(rows, settings.field_overrides),
            "transactions": page,
        }

    @mcp.tool(annotations=READ_ONLY)
    async def get_daily_statistics(
        date_from: Annotated[str | None, Field(description="Startdatum YYYY-MM-DD")] = None,
        date_to: Annotated[str | None, Field(description="Enddatum YYYY-MM-DD")] = None,
        dynamic_date: Annotated[
            DynamicDate | None, Field(description="Alternativ zu date_from/date_to: relativer Zeitraum")
        ] = None,
    ) -> dict[str, Any]:
        """Tagesstatistik des Publishers: Views, Klicks, Transaktionen, Umsatz und Provision pro Tag."""
        conditions: dict[str, Any]
        if dynamic_date and not (date_from or date_to):
            conditions = {"dynamicdate": dynamic_date}
            period: dict[str, str] = {"dynamic_date": dynamic_date}
        else:
            start, end = resolve_period(date_from, date_to)
            conditions = {"period": {"from": _fmt(start), "to": _fmt(end)}}
            period = {"from": start.isoformat(), "to": end.isoformat()}
        rows = await get_client().fetch("get-statistic_daily", conditions)
        return {"period": period, "rows": len(rows), "statistics": rows}

    @mcp.tool(annotations=READ_ONLY)
    async def get_advertiser_statistics(
        date_from: Annotated[str | None, Field(description="Startdatum YYYY-MM-DD")] = None,
        date_to: Annotated[str | None, Field(description="Enddatum YYYY-MM-DD")] = None,
    ) -> dict[str, Any]:
        """Aggregierte Statistik je Advertiser/Programm (Klicks, Transaktionen, Provision)."""
        start, end = resolve_period(date_from, date_to)
        rows = await get_client().fetch(
            "get-statistic_advertiser", {"period": {"from": _fmt(start), "to": _fmt(end)}}
        )
        return {"period": {"from": start.isoformat(), "to": end.isoformat()}, "rows": len(rows), "statistics": rows}

    @mcp.tool(annotations=READ_ONLY)
    async def list_admedia() -> dict[str, Any]:
        """Liste der verfügbaren Werbemittel (Admedia) inkl. IDs – nützlich als Filter für andere Tools."""
        rows = await get_client().fetch("get-campaigns_admedialist")
        return {"rows": len(rows), "admedia": rows}

    @mcp.tool(annotations=READ_ONLY)
    async def query_publisher_api(
        method: Annotated[
            str,
            Field(
                description="API-Methode der easy.AFFILIATE Publisher-API, z. B. 'get-statistic_transactions', "
                "'get-statistic_daily', 'get-statistic_advertiser', 'get-campaigns_admedialist'",
                pattern=r"^get-[a-z0-9]+(?:_[a-z0-9]+)*$",
            ),
        ],
        conditions: Annotated[
            dict[str, Any] | None,
            Field(
                description="Filter als Objekt; wird zu condition[...]-Parametern, z. B. "
                '{"period": {"from": "01.09.2026", "to": "30.09.2026"}, "l:processingstate": ["confirmed"]}'
            ),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=1000)] = 200,
    ) -> dict[str, Any]:
        """Direkter, nur lesender Zugriff auf beliebige Methoden der Publisher-API (für Sonderfälle).
        Datumsangaben in conditions im Format TT.MM.JJJJ."""
        rows = await get_client().fetch(method, conditions or {})
        return {"method": method, "total_rows": len(rows), "returned": min(limit, len(rows)), "rows": rows[:limit]}

    @mcp.resource("otto://info", mime_type="text/markdown")
    def info() -> str:
        """Hinweise zur Datenquelle und Konfiguration."""
        return (
            "# OTTO Partnerprogramm – Publisher-API\n\n"
            f"- Basis-URL: {settings.base_url}\n"
            f"- Publisher-ID konfiguriert: {'ja' if settings.publisher_id else 'nein'}\n"
            f"- Access-Token konfiguriert: {'ja' if settings.access_token else 'nein'}\n"
            f"- Abrufformat: {settings.api_format}\n"
            f"- Cache: {settings.cache_ttl_seconds}s\n\n"
            "Status: open = offen, confirmed = bestätigt, paid = ausgezahlt, canceled = storniert.\n"
        )

    return mcp


class BearerAuthMiddleware:
    """Schützt den MCP-Endpunkt mit statischen Bearer-Tokens (MCP_AUTH_TOKEN)."""

    def __init__(self, app: Any, tokens: list[str], open_paths: tuple[str, ...] = ("/healthz",)):
        self.app = app
        self.tokens = [t.encode() for t in tokens]
        self.open_paths = open_paths

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not self.tokens or scope.get("path") in self.open_paths:
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"")
        # X-API-Key erlaubt die Kombination mit Cloud-Run-IAM, wo der Authorization-Header
        # bereits das Google-ID-Token trägt.
        candidates = [headers.get(b"x-api-key", b"").strip()]
        if auth[:7].lower() == b"bearer ":
            candidates.append(auth[7:].strip())
        if any(c and hmac.compare_digest(c, t) for c in candidates for t in self.tokens):
            await self.app(scope, receive, send)
            return
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})


def build_http_app(settings: Settings) -> Any:
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    mcp = build_server(settings)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "configured": settings.is_configured})

    app = mcp.streamable_http_app(
        streamable_http_path=settings.mcp_path,
        stateless_http=True,  # Cloud Run skaliert horizontal – keine Session-Affinität nötig
        json_response=True,
        host=settings.host,
    )
    if not settings.mcp_auth_tokens:
        logger.warning("MCP_AUTH_TOKEN ist nicht gesetzt – der Endpunkt ist nur über Cloud-Run-IAM geschützt.")
    return BearerAuthMiddleware(app, settings.mcp_auth_tokens)


def main() -> None:
    parser = argparse.ArgumentParser(description="OTTO Partnerprogramm MCP-Server")
    parser.add_argument("--transport", choices=["http", "stdio"], default="http")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx loggt die vollständige URL – darin steckt der Access-Token. Daher nur Warnungen loggen.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    settings = Settings.from_env()

    if args.transport == "stdio":
        build_server(settings).run("stdio")
        return

    import uvicorn

    uvicorn.run(
        build_http_app(settings),
        host=settings.host,
        port=settings.port,
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_level="info",
    )


if __name__ == "__main__":
    main()
