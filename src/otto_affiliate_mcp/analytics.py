"""Auswertung von Transaktionsdaten: Spaltenerkennung, Zahlen-/Datumsparsing, Aggregation."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

Row = dict[str, Any]

# Mögliche Spaltennamen (klein geschrieben, ohne Sonderzeichen) je Kennzahl.
# Der jeweils erste Eintrag entspricht den Spalten, die die OTTO-Publisher-API tatsächlich liefert
# (get-statistic_transactions, Stand 10/2026): criterion, trackingtime, event, status, provision,
# turnover, attributed_turnover, admedia_id, subid, referrer, payoutdate, ...
# Weitere Namen dienen als Fallback für andere Formate/Sprachen.
FIELD_CANDIDATES: dict[str, list[str]] = {
    "turnover": [
        "turnover", "umsatz", "warenkorbwert", "orderturnover", "ordervalue", "basketvalue",
        "originalturnover", "salesvolume", "sale", "amount", "netturnover", "nettoumsatz",
    ],
    "commission": [
        "provision", "commission", "publishercommission", "publisherprovision", "verguetung",
        "earnings", "payout", "revenue",
    ],
    "status": [
        "status", "processingstate", "paymentstatus", "state", "transactionstatus",
        "bearbeitungsstatus", "zahlungsstatus",
    ],
    "date": [
        "trackingtime", "timestamp", "date", "datum", "createdat", "created", "orderdate",
        "transactiondate", "erstelltam", "zeitpunkt", "time", "day", "tag",
    ],
    "order_id": ["criterion", "ordertoken", "orderid", "ordernumber", "bestellnummer", "transactionid", "id"],
    "admedia": ["admediaid", "admedia", "werbemittelid", "werbemittel", "admedianame"],
    # Provisionsrelevanter (zugeordneter) Anteil am Warenkorb – entspricht dem "Umsatz" der Tagesstatistik
    "attributed_turnover": ["attributedturnover", "attributed_turnover"],
    "event": ["event", "eventtype", "trackingtype"],  # sale | lead
    "payout_date": ["payoutdate", "auszahlungsdatum"],
    "referrer": ["referrer", "referer"],
    "subid": ["subid", "sub_id"],
}

STATUS_ALIASES: dict[str, str] = {
    # Numerische Codes der OTTO-API, verifiziert gegen get-statistic_daily (10/2026):
    # 0 = offen, 1 = bestätigt, 2 = storniert, 3 = ausgezahlt (hat payoutdate + salary_id;
    # die Tagesstatistik zählt 1 und 3 gemeinsam als "confirmed").
    "0": "open", "1": "confirmed", "2": "canceled", "3": "paid",
    "open": "open", "offen": "open", "pending": "open",
    "confirmed": "confirmed", "bestaetigt": "confirmed", "bestätigt": "confirmed",
    "approved": "confirmed", "freigegeben": "confirmed",
    "paid": "paid", "ausgezahlt": "paid", "bezahlt": "paid",
    "canceled": "canceled", "cancelled": "canceled", "storniert": "canceled",
    "abgelehnt": "canceled", "rejected": "canceled", "declined": "canceled",
}

VALID_STATUSES = ("open", "confirmed", "paid", "canceled")


def _norm_key(key: str) -> str:
    return re.sub(r"[^a-z0-9äöü]", "", key.lower())


def detect_field(
    columns: Iterable[str], kind: str, overrides: Mapping[str, list[str]] | None = None
) -> str | None:
    """Findet die Spalte für eine Kennzahl (z. B. 'turnover') anhand bekannter Namen."""
    cols = list(columns)
    normalized = {_norm_key(c): c for c in cols}
    candidates = list((overrides or {}).get(kind, [])) + FIELD_CANDIDATES[kind]
    for cand in candidates:
        if cand in cols:
            return cand
        hit = normalized.get(_norm_key(cand))
        if hit:
            return hit
    return None


def detect_fields(rows: list[Row], overrides: Mapping[str, list[str]] | None = None) -> dict[str, str | None]:
    columns: list[str] = []
    for row in rows[:50]:
        for key in row:
            if key not in columns:
                columns.append(key)
    return {kind: detect_field(columns, kind, overrides) for kind in FIELD_CANDIDATES}


def parse_number(value: Any) -> Decimal | None:
    """Parst Beträge wie '1.234,56 €', '1234.56', '12,5' oder 7."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    text = re.sub(r"[^\d,.\-]", "", str(value))
    if not text or text in "-.,":
        return None
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(".", "").replace(",", ".") if text.count(",") == 1 else text.replace(",", "")
    elif text.count(".") > 1:
        text = text.replace(".", "")
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y", "%d.%m.%y",
)


def parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if re.fullmatch(r"\d{9,10}", text):
        return datetime.fromtimestamp(int(text), tz=UTC).date()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def normalize_status(value: Any) -> str:
    if value is None or str(value).strip() == "":
        return "unknown"
    text = str(value).strip().lower()
    return STATUS_ALIASES.get(text, text)


def row_status(row: Row, fields: Mapping[str, str | None]) -> str:
    """Status einer Transaktion; bestätigte Transaktionen mit Auszahlungsdatum gelten als ausgezahlt."""
    status = normalize_status(row.get(fields["status"])) if fields["status"] else "unknown"
    if status == "confirmed" and fields.get("payout_date"):
        payout = str(row.get(fields["payout_date"]) or "").strip()
        if payout not in ("", "0", "-1", "0000-00-00", "0000-00-00 00:00:00"):
            return "paid"
    return status


def _normalize_url(value: Any) -> str:
    text = str(value or "").strip()
    return re.split(r"[?#]", text, maxsplit=1)[0] if text else ""


# Gruppierungen, die eine Spalte direkt verwenden (Ergebnis wird nach Provision sortiert)
COLUMN_GROUPINGS = ("admedia", "event", "referrer", "subid")
TIME_GROUPINGS = ("day", "week", "month", "year")


def _group_key(row: Row, fields: Mapping[str, str | None], group_by: str) -> str:
    if group_by == "none":
        return "total"
    if group_by == "status":
        return row_status(row, fields)
    if group_by in COLUMN_GROUPINGS:
        col = fields.get(group_by)
        value = row.get(col) if col else None
        if group_by == "referrer":
            value = _normalize_url(value)
        return str(value).strip() if value not in (None, "") else "unknown"
    d = parse_date(row.get(fields["date"])) if fields["date"] else None
    if d is None:
        return "unknown"
    if group_by == "day":
        return d.isoformat()
    if group_by == "week":
        iso = d.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    if group_by == "month":
        return f"{d.year}-{d.month:02d}"
    if group_by == "year":
        return str(d.year)
    # beliebige Spalte als Gruppierung
    return str(row.get(group_by) or "unknown")


def _money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01")))


def summarize(
    rows: list[Row],
    group_by: str = "status",
    overrides: Mapping[str, list[str]] | None = None,
    top: int | None = None,
) -> dict[str, Any]:
    """Aggregiert Umsatz, Provision und Anzahl Transaktionen, gesamt und je Gruppe.

    - turnover: kompletter Warenkorbwert der Bestellungen
    - attributed_turnover: provisionsrelevanter Anteil (entspricht dem Umsatz der Tagesstatistik)
    - commission: Provision des Publishers
    Bei Gruppierung nach Spalten (admedia, event, referrer, subid) werden die Gruppen nach Provision
    absteigend sortiert und optional auf ``top`` begrenzt.
    """
    fields = detect_fields(rows, overrides)

    def empty() -> dict[str, Any]:
        return {
            "transactions": 0,
            "turnover": Decimal(0),
            "attributed_turnover": Decimal(0),
            "commission": Decimal(0),
            "by_status": defaultdict(lambda: Decimal(0)),
            "count_by_status": defaultdict(int),
            "by_event": defaultdict(lambda: Decimal(0)),
        }

    def num(row: Row, kind: str) -> Decimal:
        col = fields.get(kind)
        return (parse_number(row.get(col)) if col else None) or Decimal(0)

    groups: dict[str, dict[str, Any]] = defaultdict(empty)
    total = empty()
    for row in rows:
        turnover = num(row, "turnover")
        attributed = num(row, "attributed_turnover")
        commission = num(row, "commission")
        status = row_status(row, fields)
        event = str(row.get(fields["event"]) or "unknown") if fields.get("event") else "unknown"
        key = _group_key(row, fields, group_by)
        for bucket in (groups[key], total):
            bucket["transactions"] += 1
            bucket["turnover"] += turnover
            bucket["attributed_turnover"] += attributed
            bucket["commission"] += commission
            bucket["by_status"][status] += commission
            bucket["count_by_status"][status] += 1
            bucket["by_event"][event] += commission

    def render(bucket: dict[str, Any], *, full: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "transactions": bucket["transactions"],
            "turnover": _money(bucket["turnover"]),
            "commission": _money(bucket["commission"]),
        }
        if fields.get("attributed_turnover"):
            out["attributed_turnover"] = _money(bucket["attributed_turnover"])
        if full or group_by != "status":
            out["commission_by_status"] = {k: _money(v) for k, v in sorted(bucket["by_status"].items())}
        if full:
            out["transactions_by_status"] = dict(sorted(bucket["count_by_status"].items()))
            if fields.get("event"):
                out["commission_by_event"] = {k: _money(v) for k, v in sorted(bucket["by_event"].items())}
        return out

    total_out = render(total, full=True)
    # "Sichere" Provision = bestätigt + ausgezahlt; offen = kann noch storniert werden
    secured = sum((v for k, v in total["by_status"].items() if k in ("confirmed", "paid")), Decimal(0))
    total_out["commission_secured"] = _money(secured)
    total_out["commission_open"] = _money(total["by_status"].get("open", Decimal(0)))
    total_out["commission_canceled"] = _money(total["by_status"].get("canceled", Decimal(0)))

    if group_by in COLUMN_GROUPINGS:
        ordered = sorted(groups.items(), key=lambda kv: (-kv[1]["commission"], kv[0]))
    else:
        ordered = sorted(groups.items())
    groups_total = len(ordered)
    if top is not None and top > 0:
        ordered = ordered[:top]

    warnings = []
    if rows and not fields["turnover"]:
        warnings.append("Keine Umsatz-Spalte erkannt – per OTTO_FIELD_TURNOVER konfigurieren.")
    if rows and not fields["commission"]:
        warnings.append("Keine Provisions-Spalte erkannt – per OTTO_FIELD_COMMISSION konfigurieren.")
    if rows and not fields["status"]:
        warnings.append("Keine Status-Spalte erkannt – per OTTO_FIELD_STATUS konfigurieren.")
    if rows and group_by in TIME_GROUPINGS and not fields["date"]:
        warnings.append("Keine Datums-Spalte erkannt – per OTTO_FIELD_DATE konfigurieren.")
    if rows and group_by in COLUMN_GROUPINGS and not fields.get(group_by):
        warnings.append(f"Keine Spalte für Gruppierung '{group_by}' erkannt.")

    return {
        "group_by": group_by,
        "total": total_out,
        "groups_total": groups_total,
        "groups": {k: render(v) for k, v in ordered},
        "detected_fields": fields,
        "warnings": warnings,
    }
