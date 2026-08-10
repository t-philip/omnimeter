"""Parse Vattenfall "Tarievenspecificatie" PDFs into rate_schedule /
gas_rate_schedule rows.

Text extraction (PDF -> raw text) is a thin wrapper around pdfplumber, kept
separate from the actual parsing logic so the regex/period-matching code can
be unit tested against plain text fixtures without needing a real PDF.

Layout observed across 7 years (2019-2026) of real documents: each rate
period is introduced by a "Periode DD-MM-YYYY t/m DD-MM-YYYY [verbruik
tot/boven N kWh]" line, followed (not necessarily immediately — other rows
land in between) by a "Totaal Stroom per kWh" or "Totaal Gas per m³" line
carrying the all-in (incl. BTW) rate(s). Rather than trying to carve exact
block boundaries (fragile against whatever whitespace/line-break shape the
PDF text layer actually has), each "Totaal ..." match is paired with the
*nearest preceding* "Periode ..." match by text position — robust to minor
extraction noise since the two always appear in that relative order.

Two known variations per period, both handled:
  - "verbruik tot N kWh/m3" vs "verbruik boven N kWh/m3" tier split (used
    when annual consumption crosses a threshold) — only "tot" (or untiered)
    periods are kept; "boven" periods double up the same date range with a
    different tier's rate and would corrupt the schedule if both were kept.
  - Normaaltarief/Daltarief (peak/off-peak) rates before the 2024-05
    contract, where "Totaal Stroom per kWh" carries 4 numbers (Normaal
    incl/excl, Dal incl/excl); post-2024 contracts have them fold to the
    same rate, so this dashboard doesn't split daily import by T1/T2 and a
    period's rate is always the average of whatever's captured (identical
    average when N == D).
"""

import csv
import io
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from . import db, localtime

_LOCAL_TZ = localtime.LOCAL_TZ


def _today() -> str:
    """Seam for tests -- monkeypatch this, not datetime.date, to freeze
    "today" for the two live-webpage parsers below.

    Must be the *local* date, not the host's. The reference deployment runs
    Etc/UTC, so a bare date.today() returns yesterday between 00:00 and 02:00 Amsterdam time
    (00:00-01:00 in winter) -- which would date a rate period a day early and
    shrink the preceding open-ended row a day too far back."""
    return datetime.now(_LOCAL_TZ).date().isoformat()


@dataclass
class RatePeriod:
    period_start: str  # YYYY-MM-DD
    period_end: str  # YYYY-MM-DD
    rate: float  # EUR per kWh or per m3, all-in incl. BTW


_PERIODE_RE = re.compile(
    r"Periode\s+(\d{2})-(\d{2})-(\d{4})\s+t/m\s+(\d{2})-(\d{2})-(\d{4})"
    r"(?:\s+verbruik\s+(tot|boven)\s+[\d.,]+\s*(?:kWh|m3|m³|m³))?",
    re.IGNORECASE,
)

_TOTAAL_STROOM_RE = re.compile(
    r"Totaal\s+Stroom\s+per\s+kWh\s+"
    r"€\s*([\d.,]+)\s+€\s*([\d.,]+)"
    r"(?:\s+€\s*([\d.,]+)\s+€\s*([\d.,]+))?"
)

_TOTAAL_GAS_RE = re.compile(
    r"Totaal\s+Gas\s+per\s+m(?:3|³|³)\s+"
    r"€\s*([\d.,]+)\s+€\s*([\d.,]+)"
)


def _to_float(s: str) -> float:
    return float(s.replace(",", "."))


def _to_iso_date(dd: str, mm: str, yyyy: str) -> str:
    return f"{yyyy}-{mm}-{dd}"


def _find_periods(text: str) -> list[tuple[int, str, str, str | None]]:
    """Returns [(text_position, period_start_iso, period_end_iso, tier), ...]
    tier is 'tot', 'boven', or None (no threshold split that year)."""
    periods = []
    for m in _PERIODE_RE.finditer(text):
        d1, mo1, y1, d2, mo2, y2, tier = m.groups()
        periods.append(
            (m.start(), _to_iso_date(d1, mo1, y1), _to_iso_date(d2, mo2, y2), tier.lower() if tier else None)
        )
    return periods


def _nearest_preceding_period(periods: list[tuple[int, str, str, str | None]], pos: int):
    """Last period entry whose text position is <= pos, or None."""
    best = None
    for p in periods:
        if p[0] <= pos:
            best = p
        else:
            break
    return best


def parse_power_periods(text: str) -> list[RatePeriod]:
    periods = _find_periods(text)
    results: list[RatePeriod] = []
    for m in _TOTAAL_STROOM_RE.finditer(text):
        period = _nearest_preceding_period(periods, m.start())
        if period is None:
            continue
        _, start, end, tier = period
        if tier == "boven":
            continue  # keep only the "tot <threshold>" (or untiered) rate

        n_incl, _n_excl, d_incl, _d_excl = m.groups()
        if d_incl is not None:
            rate = (_to_float(n_incl) + _to_float(d_incl)) / 2.0
        else:
            rate = _to_float(n_incl)
        results.append(RatePeriod(start, end, round(rate, 6)))
    return results


def parse_gas_periods(text: str) -> list[RatePeriod]:
    periods = _find_periods(text)
    results: list[RatePeriod] = []
    for m in _TOTAAL_GAS_RE.finditer(text):
        period = _nearest_preceding_period(periods, m.start())
        if period is None:
            continue
        _, start, end, tier = period
        if tier == "boven":
            continue

        incl, _excl = m.groups()
        results.append(RatePeriod(start, end, round(_to_float(incl), 6)))
    return results


def extract_pdf_text(file_obj) -> str:
    """file_obj: a file path or a file-like object (BytesIO) opened in binary mode."""
    import pdfplumber

    parts = []
    with pdfplumber.open(file_obj) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            parts.append(page_text)
    return "\n".join(parts)


@runtime_checkable
class TariffParser(Protocol):
    """Registry mirroring meter_device.MeterDevice's existing
    precedent -- one small contract, one implementation per supplier document
    format, a registry that picks the right one so app.py never needs to know
    which supplier a PDF came from.

    `detect` must be cheap and structural (does this text contain the
    section headers this parser looks for), never a guess at a supplier name
    -- a wrong guess here silently routes a real bill through the wrong
    regex set and produces confidently-wrong rates, exactly the risk the
    original research into this design flagged."""

    name: str

    def detect(self, text: str) -> bool: ...

    def parse(self, text: str) -> dict: ...  # {"power": [RatePeriod...], "gas": [RatePeriod...]}


class VattenfallSpecificatieParser:
    """Vattenfall's personal, retrospective Tarievenspecificatie -- closed
    [period_start, period_end] ranges only. The first parser added; later
    additions add the open-ended (Tarievenblad-shaped) suppliers alongside it."""

    name = "Vattenfall Tarievenspecificatie"

    def detect(self, text: str) -> bool:
        # Reuses the exact structural markers parse_power_periods/
        # parse_gas_periods already require to extract anything -- not a
        # separate guess, the same signal this parser actually needs.
        return bool(_TOTAAL_STROOM_RE.search(text) or _TOTAAL_GAS_RE.search(text))

    def parse(self, text: str) -> dict:
        return {"power": parse_power_periods(text), "gas": parse_gas_periods(text)}


_DUTCH_MONTHS = {
    "januari": 1, "februari": 2, "maart": 3, "april": 4, "mei": 5, "juni": 6,
    "juli": 7, "augustus": 8, "september": 9, "oktober": 10, "november": 11, "december": 12,
}


# A few suppliers (Eneco, Pure Energie, Mega) label their own rate rows
# with the short tier forms ("normaal"/"dal"/"enkel", sometimes with a space
# before "tarief") rather than the "-tarief"-suffixed forms Vattenfall/
# Greenchoice/Innova/Clean Energy use. _single_or_averaged_rate() below
# expects the suffixed keys everywhere, so each short-form parser maps at
# its own call site rather than this shared helper learning every
# supplier's spelling.
SHORT_TO_SUFFIXED = {"normaal": "normaaltarief", "dal": "daltarief", "enkel": "enkeltarief"}


def _single_or_averaged_rate(rates: dict[str, float]) -> float | None:
    """Both public suppliers verified so far bill a connection one of two
    ways depending on the physical meter (Greenchoice's own PDF spells this
    out: single-register meters get "enkeltarief", dual-register meters get
    the "normaal-/daltarief" split) -- a document publishes whichever rates
    apply, sometimes all three. Since OmniMeter's own daily import doesn't
    split usage by T1/T2 either (a documented v1 limitation),
    normaal/dal is averaged into one working number when both are present,
    exactly like VattenfallSpecificatieParser already does; enkeltarief is
    the fallback when that's the only rate a document carries."""
    normaal, dal = rates.get("normaaltarief"), rates.get("daltarief")
    if normaal is not None and dal is not None:
        return round((normaal + dal) / 2.0, 6)
    if "enkeltarief" in rates:
        return rates["enkeltarief"]
    return normaal if normaal is not None else dal


class VattenfallTarievenbladParser:
    """Vattenfall's public, prospective Tarievenblad -- "Tarievenblad per
    D month YYYY", a single effective date and no end date (open-ended,
    db.OPEN_ENDED_SENTINEL). Structurally unrelated to the personal
    Tarievenspecificatie: multiple consumption tiers per rate (0-10.000,
    10.000-50.000, 50.000-10.000.000 kWh) rather than date-bounded periods.
    Only the household tier (0-10.000 kWh) is extracted -- the higher tiers
    exist for large/business connections this dashboard was never scoped
    for. Verified against a real document (2025-01-01) fetched and
    extracted with this module's own extract_pdf_text before any regex was
    written -- not guessed from memory."""

    name = "Vattenfall Tarievenblad"

    _DATE_RE = re.compile(r"Tarievenblad\s+per\s+(\d{1,2})\s+(\w+)\s+(\d{4})", re.IGNORECASE)
    _POWER_TIER_RE = re.compile(
        r"Totaal\s+variabele\s+leveringskosten\s+(enkeltarief|normaaltarief|daltarief)"
        r"\s*\(0\s*-\s*10\.000\s*kWh\)\s*€\s*([\d.,]+)",
        re.IGNORECASE,
    )
    _GAS_RE = re.compile(
        r"Totaal\s+variabele\s+leveringskosten\s*\(0\s*-\s*170\.000\s*m(?:3|³|³)\)\s*€\s*([\d.,]+)",
        re.IGNORECASE,
    )

    def detect(self, text: str) -> bool:
        return bool(self._DATE_RE.search(text))

    def _effective_date(self, text: str) -> str | None:
        m = self._DATE_RE.search(text)
        if not m:
            return None
        day, month_name, year = m.groups()
        month = _DUTCH_MONTHS.get(month_name.lower())
        if month is None:
            return None
        return f"{year}-{month:02d}-{int(day):02d}"

    def parse(self, text: str) -> dict:
        start = self._effective_date(text)
        if start is None:
            return {"power": [], "gas": []}

        rates = {tier.lower(): _to_float(v) for tier, v in self._POWER_TIER_RE.findall(text)}
        power_rate = _single_or_averaged_rate(rates)
        power = [RatePeriod(start, db.OPEN_ENDED_SENTINEL, round(power_rate, 6))] if power_rate is not None else []

        gas_m = self._GAS_RE.search(text)
        gas = [RatePeriod(start, db.OPEN_ENDED_SENTINEL, round(_to_float(gas_m.group(1)), 6))] if gas_m else []

        return {"power": power, "gas": gas}


class GreenchoiceModelcontractParser:
    """Greenchoice's public, prospective modelcontract tariff sheet --
    "Tarieven geldig per DD-MM-YYYY", numeric date (not spelled out like
    Vattenfall's Tarievenblad), single effective date, open-ended. Each rate
    row is "Stroom {tarief} per kWh <leveringstarief> <energiebelasting>
    <btw%> <totaaltarief>" / "Gas per m³ ..." -- the last € figure on the
    line is the all-in Totaaltarief column, which is what rate_schedule/
    gas_rate_schedule store. Verified against a real document (2026-05-18,
    the live PDF linked from greenchoice.nl/stroom-en-gas/modelcontract/)
    fetched and extracted before any regex was written."""

    name = "Greenchoice Modelcontract"

    _DATE_RE = re.compile(r"Tarieven\s+geldig\s+per\s+(\d{2})-(\d{2})-(\d{4})", re.IGNORECASE)
    _POWER_RE = re.compile(
        r"Stroom\s+(enkeltarief|normaaltarief|daltarief)\s+per\s+kWh\s+"
        r"€\s*[\d.,]+\s+€\s*[\d.,]+\s+\d+%\s+€\s*([\d.,]+)",
        re.IGNORECASE,
    )
    _GAS_RE = re.compile(
        r"Gas\s+per\s+m(?:3|³|³)\s+€\s*[\d.,]+\s+€\s*[\d.,]+\s+\d+%\s+€\s*([\d.,]+)",
        re.IGNORECASE,
    )

    def detect(self, text: str) -> bool:
        return bool(self._DATE_RE.search(text) and self._POWER_RE.search(text))

    def parse(self, text: str) -> dict:
        m = self._DATE_RE.search(text)
        if not m:
            return {"power": [], "gas": []}
        dd, mm, yyyy = m.groups()
        start = _to_iso_date(dd, mm, yyyy)

        rates = {tier.lower(): _to_float(v) for tier, v in self._POWER_RE.findall(text)}
        power_rate = _single_or_averaged_rate(rates)
        power = [RatePeriod(start, db.OPEN_ENDED_SENTINEL, round(power_rate, 6))] if power_rate is not None else []

        gas_m = self._GAS_RE.search(text)
        gas = [RatePeriod(start, db.OPEN_ENDED_SENTINEL, round(_to_float(gas_m.group(1)), 6))] if gas_m else []

        return {"power": power, "gas": gas}


class EnecoModelcontractParser:
    """Eneco's public Modelcontract rate table -- a live webpage,
    not a downloadable document, unlike every parser above. The intended
    input is a user's own "Print to PDF" of
    eneco.nl/duurzame-energie/modelcontract/ (no live fetch by this app --
    matches the LAN-only-by-default design; the weather feature is
    the one deliberate exception, and even that is opt-in and scheduled, not
    a scrape). Verified against a real Chromium print-to-PDF output before
    any regex was written: the table survives cleanly, one line per rate,
    no accordion/consent gating on this particular page (contrast
    BudgetThuisModelcontractParser below).

    No document-stated effective date exists to extract -- Eneco's page
    names no "geldig per"/"Tarievenblad per" date anywhere. period_start is
    therefore the date of import, not a date the source claims. A stale,
    previously-saved snapshot re-imported later would misreport its own
    age as "today" -- a known limitation of this parser class, not solved
    here, same tradeoff BudgetThuisModelcontractParser makes."""

    name = "Eneco Modelcontract"

    _TITLE_RE = re.compile(r"Tarieven\s+Eneco\s+Modelcontract", re.IGNORECASE)
    # Only the "Onbepaalde Tijd" (indefinite/variable) column is captured --
    # the first € figure after the tariff label. The second column,
    # "Bepaalde Tijd 1 jaar", is a fixed 1-year contract with a real end
    # date this app has no way to know, so treating it as open-ended would
    # be wrong in the other direction; deliberately not extracted.
    _POWER_RE = re.compile(
        r"Stroom\s+per\s+kWh\s+(normaal|dal|enkel)\s*€\s*([\d.,]+)\s*€\s*[\d.,]+",
        re.IGNORECASE,
    )
    _GAS_RE = re.compile(r"Gas\s+per\s+m(?:3|³|³)\s*€\s*([\d.,]+)\s*€\s*[\d.,]+")

    def detect(self, text: str) -> bool:
        return bool(self._TITLE_RE.search(text))

    def parse(self, text: str) -> dict:
        start = _today()

        # Eneco labels its own rows with the short forms ("normaal"/"dal"/
        # "enkel"), not the "-tarief"-suffixed forms _single_or_averaged_rate()
        # expects -- a real bug the first version of this parser had: without
        # this mapping, rates.get() silently found nothing under either key
        # and produced zero power periods. Caught by testing against the real
        # print-to-PDF output, not by inspection.
        rates = {SHORT_TO_SUFFIXED[tier.lower()]: _to_float(v) for tier, v in self._POWER_RE.findall(text)}
        power_rate = _single_or_averaged_rate(rates)
        power = [RatePeriod(start, db.OPEN_ENDED_SENTINEL, round(power_rate, 6))] if power_rate is not None else []

        gas_m = self._GAS_RE.search(text)
        gas = [RatePeriod(start, db.OPEN_ENDED_SENTINEL, round(_to_float(gas_m.group(1)), 6))] if gas_m else []

        return {"power": power, "gas": gas}


class BudgetThuisModelcontractParser:
    """Budget Thuis's public Modelcontract rate table -- also a
    live webpage. Two real steps a user must do before printing, both
    discovered the hard way (the first print-to-PDF attempt this parser was
    designed against produced unusable output without them): the rate table
    sits inside a collapsed accordion ("Tarievenblad Modelcontract voor
    onbepaalde tijd met variabele tarieven") that must be clicked open, and
    a cookie-consent overlay must be dismissed first -- left in place, it
    visually overlaps the page content and both get flattened into the same
    interleaved, unusable text stream by the PDF's text layer. With both
    done, the table extracts cleanly and was verified against a real
    Chromium print-to-PDF output before any regex was written.

    The TOTAAL column is not extracted directly: at this page's print
    column width, its digits wrap onto their own line, separated from the
    row they belong to -- reliably reconstructing that association from
    text order alone is what a wrong-parse-shaped bug looks like. Instead
    Leveringstarief + Energiebelasting are summed, which is arithmetically
    identical to TOTAAL (checked against all three real tariff rows and
    gas: e.g. 0,17545 + 0,11085 = 0,28630) and immune to where the wrap
    lands, since both addends stay on the row's own line regardless.

    Same effective-date caveat as EnecoModelcontractParser: no "geldig
    per"/"Tarievenblad per" date is stated on this page either, so
    period_start is the import date, not a source-claimed date."""

    name = "Budget Thuis Modelcontract"

    _TITLE_RE = re.compile(
        r"Tarievenblad\s+Modelcontract\s+voor\s+onbepaalde\s+tijd\s+met\s+variabele\s+tarieven",
        re.IGNORECASE,
    )
    # The page's *other* contract. Its title is present in the text even when
    # its accordion is collapsed, so it can't be used to decide whether the
    # second rate table is there -- only to bound the variable section.
    _FIXED_TITLE_RE = re.compile(
        r"Tarievenblad\s+Modelcontract\s+voor\s+bepaalde\s+tijd\s+met\s+vaste\s+tarieven",
        re.IGNORECASE,
    )
    _POWER_RE = re.compile(
        r"(Enkeltarief|Normaaltarief|Daltarief)\s*€\s*([\d.,]+)\s*€\s*([\d.,]+)",
        re.IGNORECASE,
    )
    _GAS_RE = re.compile(r"Gas\s*€\s*([\d.,]+)\s*€\s*([\d.,]+)")

    def detect(self, text: str) -> bool:
        return bool(self._TITLE_RE.search(text))

    def _variable_section(self, text: str) -> str:
        """Text belonging to the variable-tariff contract only.

        The page carries two contracts; the parser's own instructions tell
        the user to click accordions open, so both tables can legitimately
        be present in one print. Searching the whole document then takes
        power from the *last* table (findall) and gas from the *first*
        (search) -- silently mixing a fixed and a variable tariff. Scope by
        title position instead, the same fix PureEnergieModelcontractParser
        uses for its own two-section problem."""
        m = self._TITLE_RE.search(text)
        if m is None:
            return ""
        fixed = self._FIXED_TITLE_RE.search(text, m.end())
        return text[m.end() : fixed.start()] if fixed else text[m.end() :]

    def parse(self, text: str) -> dict:
        start = _today()
        section = self._variable_section(text)

        rates = {
            tier.lower(): round(_to_float(levering) + _to_float(belasting), 6)
            for tier, levering, belasting in self._POWER_RE.findall(section)
        }
        power_rate = _single_or_averaged_rate(rates)
        power = [RatePeriod(start, db.OPEN_ENDED_SENTINEL, round(power_rate, 6))] if power_rate is not None else []

        gas_m = self._GAS_RE.search(section)
        gas = (
            [RatePeriod(start, db.OPEN_ENDED_SENTINEL, round(_to_float(gas_m.group(1)) + _to_float(gas_m.group(2)), 6))]
            if gas_m
            else []
        )

        return {"power": power, "gas": gas}


class PureEnergieModelcontractParser:
    """Pure Energie's public Modelcontract tariff sheet -- a real
    downloadable PDF (pure-energie.nl/assets/Tariefblad-Gas-en-Stroom-
    Modelcontract-*.pdf), states its own effective date ("Deze tarieven zijn
    geldig vanaf D-M-YYYY"), unlike Eneco/Budget Thuis. One real structural
    difference from every parser above: this document uses no € symbol at
    all, just bare decimal numbers ("Enkel 0,24789 0,12286 0,37075").

    Electricity and gas share the exact same "Teller Leveringstarief
    Energiebelasting* Totaalprijs" row shape, and gas also happens to use
    the tier word "Enkel" -- so a bare tier-anchored regex over the whole
    document can't tell the two sections' Enkel rows apart. Scoped by text
    position instead: everything between the "Elektriciteit" and "Gas"
    section headers is searched for power, everything after "Gas" for gas."""

    name = "Pure Energie Modelcontract"

    _DATE_RE = re.compile(r"geldig\s+vanaf\s+(\d{1,2})-(\d{1,2})-(\d{4})", re.IGNORECASE)
    _ELEC_HEADER = "Elektriciteit - Modelcontract variabel"
    _GAS_HEADER = "Gas - Modelcontract variabel"
    # 3 bare numbers after the tier word: Leveringstarief, Energiebelasting,
    # Totaalprijs -- the single-column Teruglevertarieven rows further down
    # ("Enkel 0,01500") only have one number and so never match this.
    _TIER_RE = re.compile(r"(Enkel|Normaal|Dal)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)", re.IGNORECASE)

    def detect(self, text: str) -> bool:
        return bool(re.search(r"Tarieven\s+Elektriciteit\s+&\s+Gas\s+Pure\s+Energie", text, re.IGNORECASE))

    def _effective_date(self, text: str) -> str | None:
        m = self._DATE_RE.search(text)
        if not m:
            return None
        d, mo, y = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"

    def parse(self, text: str) -> dict:
        start = self._effective_date(text)
        if start is None:
            return {"power": [], "gas": []}

        elec_start = text.find(self._ELEC_HEADER)
        gas_start = text.find(self._GAS_HEADER)
        if elec_start == -1:
            elec_text = ""
        elif gas_start > elec_start:
            elec_text = text[elec_start:gas_start]
        else:
            # No gas section at all (electricity-only sheet), or gas comes
            # first. Either way the electricity section runs to the end of
            # the slice we can safely claim; slicing to a gas_start that
            # precedes it would silently yield zero power rates and produce
            # a "no rate periods found" 400 for a document we do recognise.
            elec_text = text[elec_start:]
        gas_text = text[gas_start:] if gas_start != -1 else ""

        rates = {
            SHORT_TO_SUFFIXED[tier.lower()]: _to_float(totaal)
            for tier, _levering, _belasting, totaal in self._TIER_RE.findall(elec_text)
        }
        power_rate = _single_or_averaged_rate(rates)
        power = [RatePeriod(start, db.OPEN_ENDED_SENTINEL, round(power_rate, 6))] if power_rate is not None else []

        gas_m = self._TIER_RE.search(gas_text)
        gas = [RatePeriod(start, db.OPEN_ENDED_SENTINEL, round(_to_float(gas_m.group(4)), 6))] if gas_m else []

        return {"power": power, "gas": gas}


class InnovaEnergieModelcontractParser:
    """Innova Energie's public Modelcontract tariff sheet -- a real
    downloadable PDF, "Tariefblad per DD-MM-YYYY" (numeric date, distinct
    from Vattenfall's spelled-out-month "Tarievenblad per D month YYYY" --
    similar wording, different word and different date format, verified not
    to collide). 5 columns per row (Leveringstarief, Energiebelasting, ODE,
    Btw, Totaal incl. btw) where ODE is often the literal text "n.v.t."
    (not applicable) rather than a number -- the regex matches that literal
    rather than treating it as an optional gap, so a genuinely malformed row
    can't silently be misread as one with a missing column.

    The gas table has no tier-word prefix at all (electricity only has one
    rate, no time-of-use split for gas) -- anchored on the "Leveringstarieven
    Gas" section header instead of a tier word."""

    name = "Innova Energie Modelcontract"

    _DATE_RE = re.compile(r"Tariefblad\s+per\s+(\d{2})-(\d{2})-(\d{4})")
    _BRAND_RE = re.compile(r"Innova\s+Energie")
    _POWER_RE = re.compile(
        r"(Enkeltarief|Normaaltarief|Daltarief)\s*€\s*[\d.,]+\s*€\s*[\d.,]+\s*n\.v\.t\.\s*€\s*[\d.,]+\s*€\s*([\d.,]+)",
        re.IGNORECASE,
    )
    _GAS_RE = re.compile(
        r"Leveringstarieven\s+Gas.*?€\s*[\d.,]+\s*€\s*[\d.,]+\s*€\s*[\d.,]+\s*n\.v\.t\.\s*€\s*[\d.,]+\s*€\s*([\d.,]+)",
        re.DOTALL,
    )

    def detect(self, text: str) -> bool:
        return bool(self._DATE_RE.search(text) and self._BRAND_RE.search(text))

    def parse(self, text: str) -> dict:
        m = self._DATE_RE.search(text)
        if not m:
            return {"power": [], "gas": []}
        dd, mm, yyyy = m.groups()
        start = _to_iso_date(dd, mm, yyyy)

        rates = {tier.lower(): _to_float(v) for tier, v in self._POWER_RE.findall(text)}
        power_rate = _single_or_averaged_rate(rates)
        power = [RatePeriod(start, db.OPEN_ENDED_SENTINEL, round(power_rate, 6))] if power_rate is not None else []

        gas_m = self._GAS_RE.search(text)
        gas = [RatePeriod(start, db.OPEN_ENDED_SENTINEL, round(_to_float(gas_m.group(1)), 6))] if gas_m else []

        return {"power": power, "gas": gas}


class MegaEnergieModelcontractParser:
    """Mega Energie's public Modelcontract tariff sheet -- a real
    downloadable PDF. A real trap avoided while researching this one: the
    first hit from a general web search was a stale copy still indexed
    online (filename dated June 2024); the actual current document was only
    found by checking mega.nl/modelcontract/'s own live download links.
    Always prefer the page's own current link over a search-indexed PDF URL.

    Tier words are spaced ("Enkel tarief", not "Enkeltarief") -- a real
    spelling difference from every parser above, not a typo to normalize
    away. 3 columns (Leveringstarief, Energiebelasting + ODE combined,
    Totaal); the single-€-figure Terugleveringstarieven rows further down
    ("Enkel tarief € 0,05000") don't match the 3-figure pattern."""

    name = "Mega Energie Modelcontract"

    _DATE_RE = re.compile(r"Tarieven\s+Elektriciteit\s+&\s+Gas\s+modelcontract\s+per\s+(\d{2})-(\d{2})-(\d{4})")
    _POWER_RE = re.compile(
        r"(Enkel|Normaal|Dal)\s+tarief\s*€\s*[\d.,]+\s*€\s*[\d.,]+\s*€\s*([\d.,]+)",
        re.IGNORECASE,
    )
    _GAS_RE = re.compile(r"Gas\s*€\s*[\d.,]+\s*€\s*[\d.,]+\s*€\s*([\d.,]+)")

    def detect(self, text: str) -> bool:
        return bool(self._DATE_RE.search(text))

    def parse(self, text: str) -> dict:
        m = self._DATE_RE.search(text)
        if not m:
            return {"power": [], "gas": []}
        dd, mm, yyyy = m.groups()
        start = _to_iso_date(dd, mm, yyyy)

        rates = {
            SHORT_TO_SUFFIXED[tier.lower()]: _to_float(v) for tier, v in self._POWER_RE.findall(text)
        }
        power_rate = _single_or_averaged_rate(rates)
        power = [RatePeriod(start, db.OPEN_ENDED_SENTINEL, round(power_rate, 6))] if power_rate is not None else []

        gas_m = self._GAS_RE.search(text)
        gas = [RatePeriod(start, db.OPEN_ENDED_SENTINEL, round(_to_float(gas_m.group(1)), 6))] if gas_m else []

        return {"power": power, "gas": gas}


class CleanEnergyModelcontractParser:
    """Clean Energy's public Modelcontract tariff sheet -- a real
    downloadable PDF, "De tarieven zijn geldig vanaf D month YYYY" (spelled-
    out Dutch month, same date shape as Vattenfall Tarievenblad -- reuses
    _DUTCH_MONTHS). Lowercase labels throughout ("enkeltarief totaal", not
    "Enkeltarief"). Each tier appears twice -- once as "{tier} totaal"
    (the all-in figure, incl. btw is the second column and what's wanted)
    and again as "{tier} (exclusief energiebelasting)" a few lines down;
    the literal word "totaal" in the regex is what keeps those apart, not
    position or order."""

    name = "Clean Energy Modelcontract"

    _DATE_RE = re.compile(r"geldig\s+vanaf\s+(\d{1,2})\s+(\w+)\s+(\d{4})", re.IGNORECASE)
    _POWER_RE = re.compile(
        r"(enkeltarief|normaaltarief|daltarief)\s+totaal\s*€\s*[\d.,]+\s*€\s*([\d.,]+)",
        re.IGNORECASE,
    )
    _GAS_RE = re.compile(r"levering\s+gas\s+totaal\s*€\s*[\d.,]+\s*€\s*([\d.,]+)", re.IGNORECASE)

    def detect(self, text: str) -> bool:
        return bool(self._DATE_RE.search(text) and self._POWER_RE.search(text))

    def _effective_date(self, text: str) -> str | None:
        m = self._DATE_RE.search(text)
        if not m:
            return None
        day, month_name, year = m.groups()
        month = _DUTCH_MONTHS.get(month_name.lower())
        if month is None:
            return None
        return f"{year}-{month:02d}-{int(day):02d}"

    def parse(self, text: str) -> dict:
        start = self._effective_date(text)
        if start is None:
            return {"power": [], "gas": []}

        rates = {tier.lower(): _to_float(v) for tier, v in self._POWER_RE.findall(text)}
        power_rate = _single_or_averaged_rate(rates)
        power = [RatePeriod(start, db.OPEN_ENDED_SENTINEL, round(power_rate, 6))] if power_rate is not None else []

        gas_m = self._GAS_RE.search(text)
        gas = [RatePeriod(start, db.OPEN_ENDED_SENTINEL, round(_to_float(gas_m.group(1)), 6))] if gas_m else []

        return {"power": power, "gas": gas}


# Registered in the order they should be tried. A cheap-label-sniffing
# auto-detect over this list -- the winner is named
# back to the caller (parse_tariff_pdf's "parser" key) so an import result
# is never silently attributed to the wrong supplier.
REGISTRY: list[TariffParser] = [
    VattenfallSpecificatieParser(),
    VattenfallTarievenbladParser(),
    GreenchoiceModelcontractParser(),
    EnecoModelcontractParser(),
    BudgetThuisModelcontractParser(),
    PureEnergieModelcontractParser(),
    InnovaEnergieModelcontractParser(),
    MegaEnergieModelcontractParser(),
    CleanEnergyModelcontractParser(),
]


def detect_parser(text: str) -> TariffParser | None:
    for parser in REGISTRY:
        if parser.detect(text):
            return parser
    return None


def parse_tariff_pdf(file_obj) -> dict:
    text = extract_pdf_text(file_obj)
    parser = detect_parser(text)
    if parser is None:
        return {"power": [], "gas": [], "parser": None}
    result = parser.parse(text)
    result["parser"] = parser.name
    return result


class TariffCsvError(ValueError):
    """A row in a user-supplied tariff CSV is malformed. Carries a 1-based
    line number so the error the UI shows points at the actual bad row
    rather than a generic parse failure."""


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Deliberately loose: the point is to catch a unit mistake (ct/kWh instead of
# EUR/kWh, i.e. 100x), not to police plausible tariffs. Real NL rates sit
# around 0.25 EUR/kWh and 1.35 EUR/m3, so 10.0 leaves room for an extreme
# market spike while still rejecting anything that looks like cents.
_MAX_PLAUSIBLE_RATE = 10.0


def parse_tariff_csv(csv_text: str) -> dict:
    """The generic fallback for any supplier with no
    registered PDF parser -- api/import/tariff-csv/template's own download
    documents the exact format. Deliberately fails on the first bad row
    (reject the whole file) rather than best-effort skipping rows, matching
    the PDF path's "reject outright rather than partially misread" rule --
    a hand-typed CSV is far more error-prone than a supplier's own PDF, so
    partial acceptance here would more often hide a typo than tolerate one.

    Columns: category (power|gas), period_start (YYYY-MM-DD),
    period_end (YYYY-MM-DD or blank for open-ended), rate (EUR per kWh or
    per m3, matching the PDF parsers' unit -- not ct/kWh). Fully blank lines
    and lines starting with '#' are ignored, so the template's own comment
    rows and its commented-out example rows don't need to be stripped before
    uploading if a user forgets to. A row that has content but a blank
    category cell is NOT treated as blank -- it raises, per the
    reject-the-whole-file rule above. A literal 'category' first column is
    treated as the header row and skipped, so the template can be uploaded
    unmodified as a (genuinely empty) smoke test."""
    power: list[RatePeriod] = []
    gas: list[RatePeriod] = []

    # Materialised up front so a csv-level fault (a NUL byte, an unterminated
    # quote) is raised as TariffCsvError here rather than mid-loop. That keeps
    # the module's contract exact: TariffCsvError means bad input, and anything
    # else escaping this function is our own defect. The files are a handful of
    # rate periods, so the list costs nothing.
    try:
        rows = list(csv.reader(io.StringIO(csv_text)))
    except csv.Error as e:
        raise TariffCsvError(f"malformed CSV: {e}") from None

    for lineno, row in enumerate(rows, start=1):
        cells = [c.strip() for c in row]
        # Genuinely blank rows and comments only. A row with content but an
        # empty *first* cell (",2026-01-01,2026-06-30,0.245" -- category
        # accidentally cleared, or an Excel export that shifted a column)
        # must NOT be silently dropped: that is exactly the partial
        # acceptance this parser's reject-the-whole-file rule exists to
        # prevent. It falls through to the category check below and raises.
        if not cells or not any(cells):
            continue
        if cells[0].startswith("#"):
            continue
        if cells[0].lower() == "category":
            continue

        if len(cells) != 4:
            raise TariffCsvError(
                f"line {lineno}: expected 4 columns (category,period_start,period_end,rate), got {len(cells)}"
            )
        category, start, end, rate_s = cells

        if category not in ("power", "gas"):
            raise TariffCsvError(f"line {lineno}: category must be 'power' or 'gas', got {category!r}")
        if not _ISO_DATE_RE.match(start):
            raise TariffCsvError(f"line {lineno}: period_start must be YYYY-MM-DD, got {start!r}")
        if end and not _ISO_DATE_RE.match(end):
            raise TariffCsvError(f"line {lineno}: period_end must be YYYY-MM-DD or blank, got {end!r}")
        end_iso = end or db.OPEN_ENDED_SENTINEL
        if end_iso < start:
            raise TariffCsvError(f"line {lineno}: period_end ({end_iso}) is before period_start ({start})")
        try:
            rate = float(rate_s)
        except ValueError:
            raise TariffCsvError(f"line {lineno}: rate must be a number, got {rate_s!r}") from None
        # float() happily accepts "nan"/"inf", and `nan < 0` is False, so a
        # bare negativity check lets both through. A stored NaN silently
        # poisons every downstream average and estimate -- same reasoning as
        # app.py's _parse_finite on the manual-entry route.
        if not math.isfinite(rate):
            raise TariffCsvError(f"line {lineno}: rate must be a finite number, got {rate_s!r}")
        if rate < 0:
            raise TariffCsvError(f"line {lineno}: rate must not be negative, got {rate}")
        # Upper bound in the CSV's own unit (EUR/kWh, EUR/m3). The mistake
        # this actually catches is entering ct/kWh -- a bill's "24,5 ct/kWh"
        # typed as 24.5 is a 100x cost error that no other check would see.
        if rate > _MAX_PLAUSIBLE_RATE:
            raise TariffCsvError(
                f"line {lineno}: rate {rate} is implausibly high -- expected EUR per kWh/m3 "
                f"(e.g. 0.245), not ct/kWh (24.5)"
            )

        period = RatePeriod(start, end_iso, rate)
        (power if category == "power" else gas).append(period)

    return {"power": power, "gas": gas}
