from datetime import date
from decimal import Decimal

from otto_affiliate_mcp.analytics import detect_fields, normalize_status, parse_date, parse_number, summarize
from otto_affiliate_mcp.client import parse_csv


def test_parse_number():
    assert parse_number("1.249,00") == Decimal("1249.00")
    assert parse_number("1,249.00") == Decimal("1249.00")
    assert parse_number("14,00 €") == Decimal("14.00")
    assert parse_number("10.5") == Decimal("10.5")
    assert parse_number(7) == Decimal(7)
    assert parse_number("") is None
    assert parse_number(None) is None


def test_parse_date():
    assert parse_date("2026-09-02 10:15:00") == date(2026, 9, 2)
    assert parse_date("02.09.2026") == date(2026, 9, 2)
    assert parse_date("2026-09-02T10:15:00Z") == date(2026, 9, 2)
    assert parse_date("nonsense") is None


def test_normalize_status():
    assert normalize_status("Bestätigt") == "confirmed"
    assert normalize_status("storniert") == "canceled"
    assert normalize_status("OPEN") == "open"
    assert normalize_status("") == "unknown"


def test_detect_fields_german_columns():
    fields = detect_fields([{"Bestellnummer": 1, "Datum": "x", "Status": "offen", "Umsatz": "1", "Provision": "1"}])
    assert fields["order_id"] == "Bestellnummer"
    assert fields["turnover"] == "Umsatz"
    assert fields["commission"] == "Provision"
    assert fields["date"] == "Datum"


def test_detect_fields_override():
    fields = detect_fields([{"x_val": 1}], {"turnover": ["x_val"]})
    assert fields["turnover"] == "x_val"


def test_summarize_by_status(fixture_csv):
    result = summarize(parse_csv(fixture_csv), group_by="status")
    total = result["total"]
    assert total["transactions"] == 4
    assert total["turnover"] == 1858.39
    assert total["commission"] == 130.09
    assert total["commission_secured"] == 38.47
    assert total["commission_open"] == 87.43
    assert result["groups"]["canceled"]["commission"] == 4.19
    assert result["warnings"] == []


def test_summarize_by_month_and_day(fixture_csv):
    rows = parse_csv(fixture_csv)
    assert list(summarize(rows, group_by="month")["groups"]) == ["2026-09"]
    days = summarize(rows, group_by="day")["groups"]
    assert days["2026-09-02"]["transactions"] == 2
    assert days["2026-09-02"]["commission_by_status"] == {"confirmed": 14.0, "open": 87.43}


def test_summarize_empty():
    result = summarize([], group_by="status")
    assert result["total"]["transactions"] == 0
    assert result["groups"] == {}


def test_summarize_real_otto_format():
    from pathlib import Path

    rows = parse_csv((Path(__file__).parent / "fixtures" / "transactions_otto.csv").read_text())
    fields = detect_fields(rows)
    assert fields["order_id"] == "criterion"
    assert fields["date"] == "trackingtime"
    assert fields["commission"] == "provision"
    assert fields["status"] == "status"
    assert fields["attributed_turnover"] == "attributed_turnover"

    result = summarize(rows, group_by="status")
    total = result["total"]
    assert total["transactions_by_status"] == {"canceled": 1, "confirmed": 1, "open": 3}
    assert total["commission"] == 9.74
    assert total["commission_open"] == 5.24
    assert total["commission_canceled"] == 1.5
    assert total["commission_secured"] == 3.0
    assert total["attributed_turnover"] == 121.6
    assert total["commission_by_event"] == {"lead": 5.0, "sale": 4.74}

    by_ref = summarize(rows, group_by="referrer", top=1)
    assert by_ref["groups_total"] == 2
    assert list(by_ref["groups"]) == ["https://www.example.de/artikel-b"]  # höchste Provision zuerst

    assert summarize(rows, group_by="day")["groups"]["2026-09-30"]["transactions"] == 2


def test_numeric_status_codes():
    assert normalize_status("0") == "open"
    assert normalize_status("1") == "confirmed"
    assert normalize_status("2") == "canceled"
    assert normalize_status("3") == "paid"
