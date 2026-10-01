import json
from datetime import date

import httpx
import pytest
import respx
from mcp import Client

from otto_affiliate_mcp.client import OttoApiError
from otto_affiliate_mcp.config import Settings
from otto_affiliate_mcp.server import BearerAuthMiddleware, build_server, resolve_period

SETTINGS = Settings(access_token="SECRET", publisher_id="4711", cache_ttl_seconds=0)
TX_URL = "https://partnerprogramm.otto.de/api/SECRET/publisher/4711/get-statistic_transactions.csv"


def _payload(result):
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


def test_resolve_period_defaults():
    assert resolve_period(None, None, today=date(2026, 10, 15)) == (date(2026, 10, 1), date(2026, 10, 15))
    assert resolve_period(None, "2026-09-20") == (date(2026, 9, 1), date(2026, 9, 20))
    with pytest.raises(OttoApiError):
        resolve_period("2026-09-30", "2026-09-01")
    with pytest.raises(OttoApiError):
        resolve_period("30.09.2026", None)


@respx.mock
async def test_revenue_summary_tool(fixture_csv):
    route = respx.get(TX_URL).mock(return_value=httpx.Response(200, text=fixture_csv))
    async with Client(build_server(SETTINGS)) as client:
        result = await client.call_tool(
            "get_revenue_summary",
            {"date_from": "2026-09-01", "date_to": "2026-09-30", "group_by": "admedia", "status": ["confirmed", "paid"]},
        )
    data = _payload(result)
    assert data["total"]["commission"] == 130.09
    assert set(data["groups"]) == {"501", "777"}
    params = route.calls[0].request.url.params
    assert params["condition[period][from]"] == "01.09.2026"
    assert params["condition[period][to]"] == "30.09.2026"
    assert params["condition[timetype]"] == "0"
    assert params["condition[l:processingstate]"] == "confirmed,paid"


@respx.mock
async def test_transactions_tool_paginates_and_filters(fixture_csv):
    respx.get(TX_URL).mock(return_value=httpx.Response(200, text=fixture_csv))
    async with Client(build_server(SETTINGS)) as client:
        page = _payload(await client.call_tool("get_transactions", {"limit": 1, "offset": 1, "date_from": "2026-09-01"}))
        filtered = _payload(await client.call_tool("get_transactions", {"admedia_id": "777", "date_from": "2026-09-01"}))
    assert page["total_rows"] == 4 and page["returned"] == 1 and page["has_more"] is True
    assert page["transactions"][0]["ordertoken"] == "A-1002"
    assert [t["ordertoken"] for t in filtered["transactions"]] == ["A-1003", "A-1004"]


@respx.mock
async def test_daily_statistics_dynamic_date():
    route = respx.get("https://partnerprogramm.otto.de/api/SECRET/publisher/4711/get-statistic_daily.csv").mock(
        return_value=httpx.Response(200, text="date;views;clicks\n2026-10-01;100;12\n")
    )
    async with Client(build_server(SETTINGS)) as client:
        data = _payload(await client.call_tool("get_daily_statistics", {"dynamic_date": "lastmonth"}))
    assert data["statistics"] == [{"date": "2026-10-01", "views": "100", "clicks": "12"}]
    assert route.calls[0].request.url.params["condition[dynamicdate]"] == "lastmonth"


async def test_tool_error_is_reported():
    async with Client(build_server(Settings())) as client:
        result = await client.call_tool("get_transactions", {})
    assert result.is_error
    assert "OTTO_API_ACCESS_TOKEN" in result.content[0].text


async def _call(app, headers):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(msg):
        sent.append(msg)

    await app({"type": "http", "path": "/mcp", "headers": headers}, receive, send)
    return sent[0]["status"]


async def test_bearer_middleware():
    async def ok_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})

    app = BearerAuthMiddleware(ok_app, ["s3cret"])
    assert await _call(app, []) == 401
    assert await _call(app, [(b"authorization", b"Bearer wrong")]) == 401
    assert await _call(app, [(b"authorization", b"Bearer s3cret")]) == 200
    assert await _call(app, [(b"x-api-key", b"s3cret")]) == 200
    # IAM-Modus: Authorization trägt das Google-ID-Token, der API-Key steckt in X-API-Key
    assert await _call(app, [(b"authorization", b"Bearer google-id-token"), (b"x-api-key", b"s3cret")]) == 200
