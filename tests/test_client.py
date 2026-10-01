import httpx
import pytest
import respx

from otto_affiliate_mcp.client import (
    OttoAffiliateClient,
    OttoApiError,
    build_condition_params,
    parse_csv,
    parse_json,
)
from otto_affiliate_mcp.config import Settings

SETTINGS = Settings(access_token="SECRET", publisher_id="4711", cache_ttl_seconds=60)
URL = "https://partnerprogramm.otto.de/api/SECRET/publisher/4711/get-statistic_transactions.csv"


def test_build_condition_params():
    params = build_condition_params(
        {
            "period": {"from": "01.09.2026", "to": "30.09.2026"},
            "timetype": 0,
            "l:processingstate": ["confirmed", "paid"],
            "ignored": None,
        }
    )
    assert params == {
        "condition[period][from]": "01.09.2026",
        "condition[period][to]": "30.09.2026",
        "condition[timetype]": "0",
        "condition[l:processingstate]": "confirmed,paid",
    }


def test_parse_csv_semicolon(fixture_csv):
    rows = parse_csv(fixture_csv)
    assert len(rows) == 4
    assert rows[1]["turnover"] == "1.249,00"
    assert rows[0]["ordertoken"] == "A-1001"


def test_parse_csv_comma():
    rows = parse_csv('id,turnover\n1,"10.50"\n2,"3.00"\n')
    assert rows == [{"id": "1", "turnover": "10.50"}, {"id": "2", "turnover": "3.00"}]


def test_parse_json_variants():
    assert parse_json('[{"a": 1}]') == [{"a": 1}]
    assert parse_json('{"data": [{"a": 1}]}') == [{"a": 1}]
    assert parse_json('{"1": {"a": 1}, "2": {"a": 2}}') == [{"_key": "1", "a": 1}, {"_key": "2", "a": 2}]
    with pytest.raises(OttoApiError):
        parse_json('{"error": "invalid token"}')


@respx.mock
async def test_fetch_builds_url_and_caches(fixture_csv):
    route = respx.get(URL).mock(return_value=httpx.Response(200, text=fixture_csv))
    client = OttoAffiliateClient(SETTINGS)
    rows = await client.fetch("get-statistic_transactions", {"period": {"from": "01.09.2026"}})
    assert len(rows) == 4
    assert route.calls[0].request.url.params["condition[period][from]"] == "01.09.2026"
    await client.fetch("get-statistic_transactions", {"period": {"from": "01.09.2026"}})
    assert route.call_count == 1  # zweiter Aufruf aus dem Cache
    await client.aclose()


@respx.mock
async def test_fetch_error_redacts_token():
    respx.get(URL).mock(return_value=httpx.Response(403, text="token SECRET invalid"))
    client = OttoAffiliateClient(SETTINGS)
    with pytest.raises(OttoApiError) as exc:
        await client.fetch("get-statistic_transactions")
    assert "SECRET" not in str(exc.value)
    assert "403" in str(exc.value)
    await client.aclose()


async def test_fetch_rejects_bad_method():
    client = OttoAffiliateClient(SETTINGS)
    with pytest.raises(OttoApiError):
        await client.fetch("../../admin/delete")
    await client.aclose()


async def test_fetch_requires_credentials():
    client = OttoAffiliateClient(Settings())
    with pytest.raises(OttoApiError, match="OTTO_API_ACCESS_TOKEN"):
        await client.fetch("get-statistic_daily")
    await client.aclose()
