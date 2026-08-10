"""Fixtures below are transcribed verbatim (whitespace normalized to what a
PDF text layer typically yields) from real Vattenfall Tarievenspecificatie
PDFs already read this session — used as ground truth to check against the
same rates recorded by hand in scripts/load_vattenfall_rates.py."""

import pytest

from src.tariff_parser import (
    REGISTRY,
    BudgetThuisModelcontractParser,
    CleanEnergyModelcontractParser,
    EnecoModelcontractParser,
    GreenchoiceModelcontractParser,
    InnovaEnergieModelcontractParser,
    MegaEnergieModelcontractParser,
    PureEnergieModelcontractParser,
    RatePeriod,
    TariffCsvError,
    VattenfallSpecificatieParser,
    VattenfallTarievenbladParser,
    detect_parser,
    parse_gas_periods,
    parse_power_periods,
    parse_tariff_csv,
)

# Verbatim (whitespace as pdfplumber's own extract_pdf_text
# actually yields it -- verified via this module's own extraction, not
# retyped from a viewer) text from the real, current, publicly-downloadable
# rate sheets both suppliers publish. Fetched 2026-08-07 from
# vattenfall.nl/media/.../modelcontract-tarieven-per-01-1-2025.pdf and
# media.greenchoice.nl/media/5bmlwlh3/tarieven-modelcontract-20260518.pdf.
VATTENFALL_TARIEVENBLAD_TEXT = """
Tarievenblad per 1 januari 2025
Tarief
(incl. btw)
Variabele kosten per kWh
Variabele leveringskosten enkeltarief € 0,141812
Overheidsheffingen € 0,122863
Totaal variabele leveringskosten enkeltarief (0 - 10.000 kWh) € 0,264675
Variabele leveringskosten normaaltarief € 0,148225
Overheidsheffingen € 0,122863
Totaal variabele leveringskosten normaaltarief (0 - 10.000 kWh) €0,271088
Variabele leveringskosten daltarief € 0,135157
Overheidsheffingen € 0,122863
Totaal variabele leveringskosten daltarief (0 - 10.000 kWh) € 0,258020
Variabele leveringskosten enkeltarief € 0,141812
Overheidsheffingen € 0,083938
Totaal variabele leveringskosten enkeltarief (10.000 - 50.000 kWh) € 0,225750
Variabele leveringskosten normaaltarief € 0,148225
Overheidsheffingen € 0,083938
Totaal variabele leveringskosten normaaltarief (10.000 - 50.000 kWh) € 0,232163
Variabele leveringskosten daltarief € 0,135157
Overheidsheffingen € 0,083938
Totaal variabele leveringskosten daltarief (10.000 - 50.000 kWh) € 0,219095
Vaste kosten per dag per maand per jaar
Vaste leveringskosten € 1,315154 € 40,00 €480,03
Terugleververgoeding per kWh
Terugleververgoeding Stroom € 0,030000
Tarief
(incl. btw)
Variabele kosten per m³
Variabele leveringskosten € 0,687522
Overheidsheffingen € 0,699622
Totaal variabele leveringskosten (0 - 170.000 m3) € 1,387144
Vaste kosten per dag per maand per jaar
Vaste leveringskosten € 0,196915 € 5,99 € 71,87
"""

# Dal-only variant used to check the fallback path: no normaaltarief line at
# all in the household tier, so the parser must fall back to whatever single
# rate is present rather than averaging with a missing value.
VATTENFALL_TARIEVENBLAD_ENKELTARIEF_ONLY_TEXT = """
Tarievenblad per 1 juli 2023
Totaal variabele leveringskosten enkeltarief (0 - 10.000 kWh) € 0,300000
Totaal variabele leveringskosten (0 - 170.000 m3) € 1,500000
"""

GREENCHOICE_MODELCONTRACT_TEXT = """
Modelcontract variabel (onbepaalde tijd)
Tarieven geldig per 18-05-2026
Leveringstarief Energiebelasting Btw Totaaltarief
Stroom enkeltarief per kWh € 0,12845 € 0,09161 21% € 0,26627
Stroom normaaltarief per kWh € 0,12345 € 0,09161 21% € 0,26022
Stroom daltarief per kWh € 0,13345 € 0,09161 21% € 0,27232
Terugleverkosten per kWh € 0,10826 - 21% € 0,13099
Terugleververgoeding per kWh € 0,14100 € 0,14100
Vaste leveringskosten per dag € 0,28767 - 21% € 0,34808
Gas per m³ € 0,61425 € 0,60066 21% € 1,47004
Vaste leveringskosten per dag € 0,26027 - 21% € 0,31493
"""

# Verbatim text from a real Chromium print-to-PDF of each supplier's
# live Modelcontract page (2026-08-08), run through this module's own
# extract_pdf_text() before any regex was written -- these are user-facing
# webpages, not downloadable documents, so the intended input is the user's
# own "Print to PDF" of the page, not a fetch by this app (see the classes'
# docstrings for why). eneco.nl/duurzame-energie/modelcontract/ needed no
# extra steps; budgetthuis.nl/energie/modelcontract needed cookies accepted
# and its rate accordion expanded first -- without those, printing captures
# a cookie-consent overlay interleaved with the page underneath instead.
ENECO_MODELCONTRACT_TEXT = """
Modelcontract Tarieven
Tarieven Eneco Modelcontract
Product Tarief Onbepaalde Tijd Tarief Bepaalde Tijd 1 jaar
Stroom per kWh normaal € 0,28913 € 0,27703
Stroom per kWh dal € 0,27788 € 0,26578
Stroom per kWh enkel € 0,28360 € 0,27150
Terugleverkosten per kWh tot 1
€ 0,04505 € 0,14088
januari 2027
Terugleverkosten per kWh vanaf 1
€ 0,04505 € 0,04505
januari 2027
Terugleververgoeding per kWh tot 1
€ 0,07367 € 0,15088
januari 2027
Terugleververgoeding per kWh vanaf
€ 0,07367 € 0,06867
1 januari 2027
Vaste leveringskosten voor stroom
€ 10,99 € 10,99
per maand
Gas per m3 € 1,54023 € 1,49183
Vaste leveringskosten voor gas per
€ 8,99 € 8,99
maand
"""

# Only enkel present (no normaal/dal lines) -- checks the fallback path with
# Eneco's own short tier-name spelling, not the "-tarief" suffixed form.
ENECO_MODELCONTRACT_ENKEL_ONLY_TEXT = """
Tarieven Eneco Modelcontract
Stroom per kWh enkel € 0,30000 € 0,29000
Gas per m3 € 1,60000 € 1,55000
"""

BUDGET_THUIS_MODELCONTRACT_TEXT = """
Tarievenblad Modelcontract voor bepaalde tijd met vaste tarieven (1 jaar)
Tarievenblad Modelcontract voor onbepaalde tijd met variabele tarieven
Met het Energie van Budget Thuis Modelcontract Variabel kies je voor een modelcontract
Leveringstarieven elektriciteit
Aansluiting Leveringstarief Energiebelasting TOTAAL Terugleververgoeding* Terugleverkosten
per kWh t/m 10.000 kWh per kWh, excl. btw per kWh
Enkeltarief € 0,17545 € 0,11085 € € 0,07250 € 0,07000
0,28630
Normaaltarief € 0,17720 € 0,11085 € € 0,07323 € 0,07072
0,28805
Daltarief € 0,17369 € 0,11085 € € 0,07178 € 0,06928
0,28454
Vaste € 9,99
leveringskosten
per aansluiting
per maand
Leveringstarieven gas
Aansluiting Leveringstarief Energiebelasting TOTAAL
per m³ t/m 170.000 m³
Gas € 0,76230 € 0,72680 € 1,48910
Vaste leveringskosten € 9,99
per aansluiting per maand
"""

# The same page with BOTH accordions expanded -- a plausible print,
# since the parser's own instructions tell the user to click accordions open.
# The fixed-tariff table is deliberately given clearly different numbers so a
# section-scoping failure shows up as a wrong value rather than a near-miss.
# Layout mirrors the collapsed capture above: fixed section first, variable
# second (so the variable section runs to the end of the document).
BUDGET_THUIS_BOTH_SECTIONS_TEXT = """
Tarievenblad Modelcontract voor bepaalde tijd met vaste tarieven (1 jaar)
Leveringstarieven elektriciteit
Enkeltarief € 0,20000 € 0,11085 € 0,31085
Normaaltarief € 0,21000 € 0,11085 € 0,32085
Daltarief € 0,19000 € 0,11085 € 0,30085
Leveringstarieven gas
Gas € 0,90000 € 0,72680 € 1,62680
Tarievenblad Modelcontract voor onbepaalde tijd met variabele tarieven
Leveringstarieven elektriciteit
Enkeltarief € 0,17545 € 0,11085 € 0,28630
Normaaltarief € 0,17720 € 0,11085 € 0,28805
Daltarief € 0,17369 € 0,11085 € 0,28454
Leveringstarieven gas
Gas € 0,76230 € 0,72680 € 1,48910
"""

# Verbatim text from 4 more real, current, publicly-downloadable
# supplier PDFs (fetched 2026-08-08, extracted with this module's own
# extract_pdf_text() before any regex was written -- same discipline as
# every fixture above).
PURE_ENERGIE_TEXT = """
Tarieven Elektriciteit & Gas Pure Energie
Stroom en gas worden in één pakket (dual fuel) en apart aangeboden.
Deze tarieven zijn geldig vanaf 1-1-2026.
Elektriciteit - Modelcontract variabel
Teller Leveringstarief Energiebelasting* Totaalprijs per kWh
Enkel 0,24789 0,12286 0,37075
Normaal 0,27379 0,12286 0,39665
Dal 0,21624 0,12286 0,33910
Teruglevertarieven
Teller Terugleveringstarief
Enkel 0,01500
Normaal 0,01500
Dal 0,01500
Gas - Modelcontract variabel
Teller Leveringstarief Energiebelasting* Totaalprijs per m3
Enkel 0,56393 0,69957 1,26350
"""

INNOVA_TEXT = """
Tariefblad per 01-07-2025
Tarieven Elektriciteit Variabel
Uw variabele leveringskosten, energiebelasting, ODE en btw (per kWh) zijn:
Leveringstarief Energiebelasting* ODE Btw Totaal incl. btw
Enkeltarief € 0,14546 € 0,10154 n.v.t. € 0,05187 € 0,29887
Normaaltarief € 0,15449 € 0,10154 n.v.t. € 0,05377 € 0,30980
Daltarief € 0,14066 € 0,10154 n.v.t. € 0,05086 € 0,29306
Innova Energie • Oude Middenweg 85 • 2491 AC Den Haag
Tarieven Gas Variabel
Uw variabele leveringskosten, regiotoeslag, energiebelasting, ODE en btw (per m3) zijn:
Leveringstarieven Gas
Leveringstarief Leveringstarief Energiebelasting* ODE Btw Totaal incl. btw
incl. regiotoeslag
€ 0,59423 € 0,59423 € 0,57816 n.v.t. € 0,24620 € 1,41859
"""

MEGA_TEXT = """
Tarieven Elektriciteit & Gas modelcontract
per 01-01-2026
Elektriciteit Leveringstarieven
Soort aansluiting Leveringstarief Energiebelasting + ODE Totaal
per kWh t/m verbruik 10.000 kWh
Enkel tarief € 0,26301 € 0,11084 € 0,37385
Normaal tarief € 0,35098 € 0,11084 € 0,46182
Dal tarief € 0,18581 € 0,11084 € 0,29665
Elektriciteit Terugleveringstarieven (Niet gesaldeerd)
Soort aansluiting Leveringstarief
per kWh
Enkel tarief € 0,05000
Normaal tarief € 0,05000
Dal tarief € 0,05000
Gas Leveringstarieven
Soort aansluiting Leveringstarief Energiebelasting + ODE Totaal
per m³3 t/m verbruik 170.000 m³3
Gas € 0,60490 € 0,72673 € 1,33163
"""

CLEAN_ENERGY_TEXT = """
Leveringstarieven Modelcontract
De tarieven zijn geldig vanaf 1 januari 2026.
Leveringstarieven groene stroom excl. btw incl. btw (21%)
enkeltarief totaal € 0,23161 € 0,28025 /kWh
normaaltarief totaal € 0,23161 € 0,28025
daltarief totaal € 0,23161 € 0,28025
terugleververgoeding € 0,12397 € 0,15000 /kWh
terugleverkosten € 0,11570 € 0,14000 /kWh
vaste leveringskosten € 0,32712 € 0,39582 /dag
enkeltarief (exclusief energiebelasting) € 0,14000 € 0,16940 /kWh
normaaltarief (exclusief energiebelasting) € 0,14000 € 0,16940 /kWh
daltarief (exclusief energiebelasting) € 0,14000 € 0,16940 /kWh
energiebelasting € 0,09161 € 0,11085 /kWh
Leveringstarieven aardgas excl. btw incl. btw (21%)
levering gas totaal € 1,08066 € 1,30760 /m3
vaste leveringskosten € 0,32712 € 0,39582 /dag
levering gas (exclusief energiebelasting) € 0,48000 € 0,58080 /m3
energiebelasting € 0,60066 € 0,72680 /m3
"""

# 2025-2026 contract: tot/boven 2.900 kWh threshold split, Normaal == Dal.
POWER_TEXT_2026 = """
Normaaltarief Daltarief
Periode 28-04-2025 t/m 30-04-2025 verbruik tot 2.900 kWh Incl. 21% BTW Excl.BTW Incl. 21% BTW Excl.BTW
Stroom (basisprijs) € 0,162745 € 0,134500 € 0,162745 € 0,134500
Blijven Loont-korting 25,00 % € 0,040686- € 0,033625- € 0,040686- € 0,033625-
Stroom € 0,122059 € 0,100875 € 0,122059 € 0,100875
Energiebelasting tot 2.900 kWh € 0,122863 € 0,101540 € 0,122863 € 0,101540
Energiebelasting € 0,122863 € 0,101540 € 0,122863 € 0,101540
Totaal Stroom per kWh € 0,244922 € 0,202415 € 0,244922 € 0,202415
Normaaltarief Daltarief
Periode 28-04-2025 t/m 30-04-2025 verbruik boven 2.900 kWh Incl. 21% BTW Excl.BTW Incl. 21% BTW Excl.BTW
Stroom (basisprijs) € 0,162745 € 0,134500 € 0,162745 € 0,134500
Blijven Loont-korting 25,00 % € 0,040686- € 0,033625- € 0,040686- € 0,033625-
Stroom € 0,122059 € 0,100875 € 0,122059 € 0,100875
Totaal Stroom per kWh € 0,122059 € 0,100875 € 0,122059 € 0,100875
Normaaltarief Daltarief
Periode 01-02-2026 t/m 27-04-2026 verbruik tot 2.900 kWh Incl. 21% BTW Excl.BTW Incl. 21% BTW Excl.BTW
Stroom (basisprijs) € 0,143264 € 0,118400 € 0,143264 € 0,118400
Totaal Stroom per kWh € 0,218296 € 0,180410 € 0,218296 € 0,180410
"""

GAS_TEXT_2026 = """
Kosten
Periode 28-04-2025 t/m 30-04-2025 Incl. 21% BTW Excl.BTW
Gas (basisprijs) € 0,726363 € 0,600300
Gas € 0,726363 € 0,600300
Energiebelasting tot 1.000 m3 € 0,699574 € 0,578160
Energiebelasting € 0,699574 € 0,578160
Totaal Gas per m3 € 1,425937 € 1,178460
Kosten
Periode 01-02-2026 t/m 27-04-2026 Incl. 21% BTW Excl.BTW
Gas (basisprijs) € 0,565675 € 0,467500
Totaal Gas per m3 € 1,292474 € 1,068160
"""

# 2019-2020 contract: dal-only (no Normaaltarief column at all that year).
POWER_TEXT_2019 = """
Daltarief
Periode 30-04-2019 t/m 30-06-2019
Incl. 21% BTW Excl.BTW
Stroom (basisprijs) € 0,079618 € 0,065800
Blijven Loont-korting 25,00 % € 0,019905- € 0,016450-
Stroom € 0,059713 € 0,049350
Energiebelasting tot 10.000 kWh € 0,119342 € 0,098630
Opslag duurzame energie tot 10.000 kWh € 0,022869 € 0,018900
Overheidsheffingen € 0,142211 € 0,117530
Totaal Stroom per kWh € 0,201924 € 0,166880
"""


class TestParsePowerPeriods:
    def test_keeps_tot_tier_skips_boven_tier(self):
        periods = parse_power_periods(POWER_TEXT_2026)
        starts = [(p.period_start, p.period_end) for p in periods]
        # only 2 periods: the "boven" duplicate of 28-04..30-04 must be dropped
        assert starts == [("2025-04-28", "2025-04-30"), ("2026-02-01", "2026-04-27")]

    def test_averages_normaal_and_dal_when_equal(self):
        periods = parse_power_periods(POWER_TEXT_2026)
        assert periods[0].rate == 0.244922
        assert periods[1].rate == 0.218296

    def test_single_tariff_period_no_normaal_column(self):
        periods = parse_power_periods(POWER_TEXT_2019)
        assert len(periods) == 1
        assert periods[0].period_start == "2019-04-30"
        assert periods[0].period_end == "2019-06-30"
        assert periods[0].rate == 0.201924

    def test_averages_differing_normaal_and_dal(self):
        text = (
            "Normaaltarief Daltarief\n"
            "Periode 01-07-2021 t/m 31-12-2021 Incl. 21% BTW Excl.BTW Incl. 21% BTW Excl.BTW\n"
            "Totaal Stroom per kWh € 0,220438 € 0,182180 € 0,208459 € 0,172280\n"
        )
        periods = parse_power_periods(text)
        assert len(periods) == 1
        assert periods[0].rate == round((0.220438 + 0.208459) / 2, 6)


class TestParseGasPeriods:
    def test_extracts_both_periods(self):
        periods = parse_gas_periods(GAS_TEXT_2026)
        assert len(periods) == 2
        assert periods[0].period_start == "2025-04-28"
        assert periods[0].rate == 1.425937
        assert periods[1].period_start == "2026-02-01"
        assert periods[1].rate == 1.292474

    def test_no_gas_text_returns_empty(self):
        assert parse_gas_periods(POWER_TEXT_2026) == []


class TestRegistry:
    """TariffParser protocol + registry, mirroring
    meter_device.MeterDevice's precedent."""

    def test_detect_returns_the_matching_parser(self):
        parser = detect_parser(POWER_TEXT_2026)
        assert parser is not None
        assert parser.name == "Vattenfall Tarievenspecificatie"

    def test_detect_returns_none_for_unrecognized_text(self):
        assert detect_parser("this is not a rate sheet of any kind") is None

    def test_registry_contains_the_vattenfall_parser(self):
        assert any(isinstance(p, VattenfallSpecificatieParser) for p in REGISTRY)

    def test_parser_parse_matches_the_free_functions(self):
        parser = VattenfallSpecificatieParser()
        result = parser.parse(POWER_TEXT_2026 + GAS_TEXT_2026)
        assert result["power"] == parse_power_periods(POWER_TEXT_2026 + GAS_TEXT_2026)
        assert result["gas"] == parse_gas_periods(POWER_TEXT_2026 + GAS_TEXT_2026)

    def test_full_registry_picks_the_right_parser_for_each_real_document(self):
        # The nine real document shapes verified so far must not be
        # confused with one another by auto-detection.
        assert detect_parser(POWER_TEXT_2026).name == "Vattenfall Tarievenspecificatie"
        assert detect_parser(VATTENFALL_TARIEVENBLAD_TEXT).name == "Vattenfall Tarievenblad"
        assert detect_parser(GREENCHOICE_MODELCONTRACT_TEXT).name == "Greenchoice Modelcontract"
        assert detect_parser(ENECO_MODELCONTRACT_TEXT).name == "Eneco Modelcontract"
        assert detect_parser(BUDGET_THUIS_MODELCONTRACT_TEXT).name == "Budget Thuis Modelcontract"
        assert detect_parser(PURE_ENERGIE_TEXT).name == "Pure Energie Modelcontract"
        assert detect_parser(INNOVA_TEXT).name == "Innova Energie Modelcontract"
        assert detect_parser(MEGA_TEXT).name == "Mega Energie Modelcontract"
        assert detect_parser(CLEAN_ENERGY_TEXT).name == "Clean Energy Modelcontract"


class TestVattenfallTarievenbladParser:
    """Fixture is verbatim real text -- see the module
    docstring above for provenance."""

    def test_detects_by_title(self):
        assert VattenfallTarievenbladParser().detect(VATTENFALL_TARIEVENBLAD_TEXT) is True

    def test_does_not_detect_the_specificatie_document(self):
        assert VattenfallTarievenbladParser().detect(POWER_TEXT_2026) is False

    def test_effective_date_is_open_ended(self):
        from src.db import OPEN_ENDED_SENTINEL

        result = VattenfallTarievenbladParser().parse(VATTENFALL_TARIEVENBLAD_TEXT)
        assert result["power"][0].period_start == "2025-01-01"
        assert result["power"][0].period_end == OPEN_ENDED_SENTINEL
        assert result["gas"][0].period_start == "2025-01-01"
        assert result["gas"][0].period_end == OPEN_ENDED_SENTINEL

    def test_power_rate_averages_normaal_and_dal_of_the_household_tier(self):
        # (0,271088 + 0,258020) / 2 -- the higher consumption tiers present
        # in the same document must not leak into this figure.
        result = VattenfallTarievenbladParser().parse(VATTENFALL_TARIEVENBLAD_TEXT)
        assert result["power"][0].rate == pytest.approx(round((0.271088 + 0.258020) / 2, 6))

    def test_gas_rate_from_the_single_household_tier(self):
        result = VattenfallTarievenbladParser().parse(VATTENFALL_TARIEVENBLAD_TEXT)
        assert result["gas"][0].rate == pytest.approx(1.387144)

    def test_falls_back_to_enkeltarief_when_normaal_dal_absent(self):
        result = VattenfallTarievenbladParser().parse(VATTENFALL_TARIEVENBLAD_ENKELTARIEF_ONLY_TEXT)
        assert result["power"][0].rate == pytest.approx(0.300000)
        assert result["power"][0].period_start == "2023-07-01"


class TestGreenchoiceModelcontractParser:
    """Fixture is verbatim real text -- see the module
    docstring above for provenance."""

    def test_detects_by_title_and_rate_rows(self):
        assert GreenchoiceModelcontractParser().detect(GREENCHOICE_MODELCONTRACT_TEXT) is True

    def test_does_not_detect_the_vattenfall_documents(self):
        assert GreenchoiceModelcontractParser().detect(POWER_TEXT_2026) is False
        assert GreenchoiceModelcontractParser().detect(VATTENFALL_TARIEVENBLAD_TEXT) is False

    def test_effective_date_is_open_ended_from_numeric_date(self):
        from src.db import OPEN_ENDED_SENTINEL

        result = GreenchoiceModelcontractParser().parse(GREENCHOICE_MODELCONTRACT_TEXT)
        assert result["power"][0].period_start == "2026-05-18"
        assert result["power"][0].period_end == OPEN_ENDED_SENTINEL

    def test_power_rate_is_totaaltarief_column_averaged_normaal_dal(self):
        # (0,26022 + 0,27232) / 2 -- the Leveringstarief/Energiebelasting
        # columns earlier on the same line must not be picked up instead of
        # the final Totaaltarief figure.
        result = GreenchoiceModelcontractParser().parse(GREENCHOICE_MODELCONTRACT_TEXT)
        assert result["power"][0].rate == pytest.approx(round((0.26022 + 0.27232) / 2, 6))

    def test_gas_rate_is_totaaltarief_column_not_leveringstarief(self):
        result = GreenchoiceModelcontractParser().parse(GREENCHOICE_MODELCONTRACT_TEXT)
        assert result["gas"][0].rate == pytest.approx(1.47004)

    def test_terugleverkosten_and_vaste_kosten_rows_not_mistaken_for_stroom_or_gas(self):
        # Both rows share the "€ ... € ... 21% € ..." shape with the real
        # Stroom/Gas rows and sit directly adjacent to them in the document
        # -- only one power period and one gas period may come out.
        result = GreenchoiceModelcontractParser().parse(GREENCHOICE_MODELCONTRACT_TEXT)
        assert len(result["power"]) == 1
        assert len(result["gas"]) == 1


class TestEnecoModelcontractParser:
    """Fixture is verbatim real text from a Chromium print-to-PDF of
    the live page -- see the module docstring above for provenance. This
    supplier's rates are a live webpage, not a downloadable document; the
    intended input is a user's own saved PDF of the page."""

    def test_detects_by_title(self):
        assert EnecoModelcontractParser().detect(ENECO_MODELCONTRACT_TEXT) is True

    def test_does_not_detect_other_documents(self):
        assert EnecoModelcontractParser().detect(POWER_TEXT_2026) is False
        assert EnecoModelcontractParser().detect(VATTENFALL_TARIEVENBLAD_TEXT) is False
        assert EnecoModelcontractParser().detect(GREENCHOICE_MODELCONTRACT_TEXT) is False
        assert EnecoModelcontractParser().detect(BUDGET_THUIS_MODELCONTRACT_TEXT) is False

    def test_effective_date_is_todays_date_not_extracted(self, monkeypatch):
        # No "geldig per"/"Tarievenblad per" date exists on this page --
        # period_start must come from the import-time seam, not the text.
        from src import tariff_parser
        from src.db import OPEN_ENDED_SENTINEL

        monkeypatch.setattr(tariff_parser, "_today", lambda: "2026-08-08")
        result = EnecoModelcontractParser().parse(ENECO_MODELCONTRACT_TEXT)
        assert result["power"][0].period_start == "2026-08-08"
        assert result["power"][0].period_end == OPEN_ENDED_SENTINEL
        assert result["gas"][0].period_start == "2026-08-08"

    def test_power_rate_averages_normaal_and_dal_only_the_onbepaalde_tijd_column(self, monkeypatch):
        # (0,28913 + 0,27788) / 2 -- the second "Bepaalde Tijd 1 jaar"
        # column (a real fixed-term end date this app can't know) must not
        # be picked up instead of "Onbepaalde Tijd".
        from src import tariff_parser

        monkeypatch.setattr(tariff_parser, "_today", lambda: "2026-08-08")
        result = EnecoModelcontractParser().parse(ENECO_MODELCONTRACT_TEXT)
        assert result["power"][0].rate == pytest.approx(round((0.28913 + 0.27788) / 2, 6))

    def test_gas_rate_from_onbepaalde_tijd_column(self, monkeypatch):
        from src import tariff_parser

        monkeypatch.setattr(tariff_parser, "_today", lambda: "2026-08-08")
        result = EnecoModelcontractParser().parse(ENECO_MODELCONTRACT_TEXT)
        assert result["gas"][0].rate == pytest.approx(1.54023)

    def test_terugleverkosten_and_vaste_kosten_rows_not_mistaken_for_stroom_or_gas(self, monkeypatch):
        from src import tariff_parser

        monkeypatch.setattr(tariff_parser, "_today", lambda: "2026-08-08")
        result = EnecoModelcontractParser().parse(ENECO_MODELCONTRACT_TEXT)
        assert len(result["power"]) == 1
        assert len(result["gas"]) == 1

    def test_falls_back_to_enkel_when_normaal_dal_absent(self, monkeypatch):
        # Regression test for a real bug: the first version of this parser
        # collected rates under Eneco's own short tier names ("normaal"/
        # "dal"/"enkel") but _single_or_averaged_rate() expects the
        # "-tarief"-suffixed keys, so rates.get() silently found nothing and
        # produced zero power periods. Caught by running against the real
        # print-to-PDF output, not by inspection.
        from src import tariff_parser

        monkeypatch.setattr(tariff_parser, "_today", lambda: "2026-08-08")
        result = EnecoModelcontractParser().parse(ENECO_MODELCONTRACT_ENKEL_ONLY_TEXT)
        assert len(result["power"]) == 1
        assert result["power"][0].rate == pytest.approx(0.30000)


class TestBudgetThuisModelcontractParser:
    """Fixture is verbatim real text from a Chromium print-to-PDF of
    the live page, captured only after accepting cookies and expanding the
    rate accordion -- see the module docstring above and
    BudgetThuisModelcontractParser's own docstring for why both are
    required."""

    def test_detects_by_title(self):
        assert BudgetThuisModelcontractParser().detect(BUDGET_THUIS_MODELCONTRACT_TEXT) is True

    def test_does_not_detect_other_documents(self):
        assert BudgetThuisModelcontractParser().detect(POWER_TEXT_2026) is False
        assert BudgetThuisModelcontractParser().detect(ENECO_MODELCONTRACT_TEXT) is False
        assert BudgetThuisModelcontractParser().detect(GREENCHOICE_MODELCONTRACT_TEXT) is False

    def test_does_not_detect_the_bepaalde_tijd_variant_title_alone(self):
        # The fixed-term title line sits directly above the one this parser
        # must match -- confirms detect() keys on "onbepaalde tijd"
        # specifically, not just "Tarievenblad Modelcontract" generically.
        assert BudgetThuisModelcontractParser().detect(
            "Tarievenblad Modelcontract voor bepaalde tijd met vaste tarieven (1 jaar)\n"
            "Enkeltarief € 0,17545 € 0,11085"
        ) is False

    def test_both_accordions_expanded_uses_only_the_variable_section(self, monkeypatch):
        # Without section scoping the power rate came from the
        # LAST matching table (findall) and the gas rate from the FIRST
        # (search) -- silently mixing a fixed and a variable tariff into one
        # stored open-ended rate. Both must come from the variable section.
        from src import tariff_parser

        monkeypatch.setattr(tariff_parser, "_today", lambda: "2026-08-08")
        result = BudgetThuisModelcontractParser().parse(BUDGET_THUIS_BOTH_SECTIONS_TEXT)

        # Normaal+dal averaged (the documented rule), from the VARIABLE
        # table: (0,28805 + 0,28454) / 2. The fixed table's equivalent is
        # (0,32085 + 0,30085) / 2 = 0,31085 -- far enough apart that a
        # scoping failure can't pass as a rounding difference.
        assert result["power"][0].rate == pytest.approx((0.28805 + 0.28454) / 2, abs=1e-6)
        # Variable gas (0.76230 + 0.72680), not the fixed 0.90000 + 0.72680.
        assert result["gas"][0].rate == pytest.approx(1.48910, abs=1e-6)

    def test_single_expanded_accordion_still_parses(self, monkeypatch):
        # The section-scoping fix must not break the ordinary capture, where
        # only the variable accordion was expanded.
        from src import tariff_parser

        monkeypatch.setattr(tariff_parser, "_today", lambda: "2026-08-08")
        result = BudgetThuisModelcontractParser().parse(BUDGET_THUIS_MODELCONTRACT_TEXT)
        assert result["gas"][0].rate == pytest.approx(1.48910, abs=1e-6)
        # Assert the VALUE, not just that a period exists -- a count-only
        # assertion passes even when the wrong section was selected, which is
        # the failure mode this whole test exists to catch.
        assert len(result["power"]) == 1
        assert result["power"][0].rate == pytest.approx((0.28805 + 0.28454) / 2, abs=1e-6)

    def test_variable_section_first_is_bounded_by_the_fixed_title(self, monkeypatch):
        # Both real fixtures put the fixed-tariff title *before* the variable
        # one, so _variable_section() always took its "run to end of document"
        # path and the bounded slice was never exercised by any test. This
        # covers the other ordering: variable first, fixed second, where the
        # slice must stop at the fixed title or the fixed table's rates win.
        from src import tariff_parser

        monkeypatch.setattr(tariff_parser, "_today", lambda: "2026-08-08")
        reordered = (
            "Tarievenblad Modelcontract voor onbepaalde tijd met variabele tarieven\n"
            "Enkeltarief € 0,17545 € 0,11085\n"
            "Normaaltarief € 0,17720 € 0,11085\n"
            "Daltarief € 0,17369 € 0,11085\n"
            "Gas € 0,76230 € 0,72680\n"
            "Tarievenblad Modelcontract voor bepaalde tijd met vaste tarieven (1 jaar)\n"
            "Enkeltarief € 0,20000 € 0,11085\n"
            "Normaaltarief € 0,21000 € 0,11085\n"
            "Daltarief € 0,19000 € 0,11085\n"
            "Gas € 0,90000 € 0,72680\n"
        )
        result = BudgetThuisModelcontractParser().parse(reordered)
        assert result["power"][0].rate == pytest.approx((0.28805 + 0.28454) / 2, abs=1e-6)
        assert result["gas"][0].rate == pytest.approx(1.48910, abs=1e-6)

    def test_effective_date_is_todays_date_not_extracted(self, monkeypatch):
        from src import tariff_parser
        from src.db import OPEN_ENDED_SENTINEL

        monkeypatch.setattr(tariff_parser, "_today", lambda: "2026-08-08")
        result = BudgetThuisModelcontractParser().parse(BUDGET_THUIS_MODELCONTRACT_TEXT)
        assert result["power"][0].period_start == "2026-08-08"
        assert result["power"][0].period_end == OPEN_ENDED_SENTINEL

    def test_power_rate_is_leveringstarief_plus_energiebelasting_averaged(self, monkeypatch):
        # TOTAAL's own digits wrap onto a separate line at this page's print
        # width (see the class docstring), so the rate is computed from the
        # two reliably-single-line columns instead: (0,17720+0,11085 +
        # 0,17369+0,11085) / 2 for normaal/dal -- confirmed arithmetically
        # identical to the printed TOTAAL figures (0,28805 and 0,28454).
        from src import tariff_parser

        monkeypatch.setattr(tariff_parser, "_today", lambda: "2026-08-08")
        result = BudgetThuisModelcontractParser().parse(BUDGET_THUIS_MODELCONTRACT_TEXT)
        normaal_totaal = round(0.17720 + 0.11085, 6)
        dal_totaal = round(0.17369 + 0.11085, 6)
        assert normaal_totaal == pytest.approx(0.28805)  # sanity: matches the real printed TOTAAL
        assert dal_totaal == pytest.approx(0.28454)
        assert result["power"][0].rate == pytest.approx(round((normaal_totaal + dal_totaal) / 2, 6))

    def test_gas_rate_is_leveringstarief_plus_energiebelasting(self, monkeypatch):
        from src import tariff_parser

        monkeypatch.setattr(tariff_parser, "_today", lambda: "2026-08-08")
        result = BudgetThuisModelcontractParser().parse(BUDGET_THUIS_MODELCONTRACT_TEXT)
        assert result["gas"][0].rate == pytest.approx(round(0.76230 + 0.72680, 6))
        assert result["gas"][0].rate == pytest.approx(1.48910)  # matches the real printed TOTAAL

    def test_only_one_power_and_gas_period_each(self, monkeypatch):
        from src import tariff_parser

        monkeypatch.setattr(tariff_parser, "_today", lambda: "2026-08-08")
        result = BudgetThuisModelcontractParser().parse(BUDGET_THUIS_MODELCONTRACT_TEXT)
        assert len(result["power"]) == 1
        assert len(result["gas"]) == 1


class TestPureEnergieModelcontractParser:
    """Fixture is verbatim real text from the real downloadable PDF
    -- see the module docstring above for provenance. The one parser so far
    with no € symbol at all in its source document."""

    def test_detects_by_title(self):
        assert PureEnergieModelcontractParser().detect(PURE_ENERGIE_TEXT) is True

    def test_does_not_detect_other_documents(self):
        assert PureEnergieModelcontractParser().detect(POWER_TEXT_2026) is False
        assert PureEnergieModelcontractParser().detect(MEGA_TEXT) is False

    def test_effective_date_is_stated_by_the_document(self):
        from src.db import OPEN_ENDED_SENTINEL

        result = PureEnergieModelcontractParser().parse(PURE_ENERGIE_TEXT)
        assert result["power"][0].period_start == "2026-01-01"
        assert result["power"][0].period_end == OPEN_ENDED_SENTINEL

    def test_power_rate_averages_normaal_and_dal_totaalprijs(self):
        # (0,39665 + 0,33910) / 2 -- the Leveringstarief/Energiebelasting
        # columns before Totaalprijs must not be picked up instead.
        result = PureEnergieModelcontractParser().parse(PURE_ENERGIE_TEXT)
        assert result["power"][0].rate == pytest.approx(round((0.39665 + 0.33910) / 2, 6))

    def test_gas_rate_is_totaalprijs(self):
        result = PureEnergieModelcontractParser().parse(PURE_ENERGIE_TEXT)
        assert result["gas"][0].rate == pytest.approx(1.26350)

    def test_electricity_only_sheet_still_yields_power_rates(self):
        # The electricity slice used to be text[elec:gas] guarded on
        # BOTH headers being found, so an electricity-only sheet produced an
        # empty slice and therefore ZERO power periods -- while detect()
        # still passed on the title. The user got "no rate periods found"
        # for a document the app does recognise.
        elec_only = "\n".join(
            line for line in PURE_ENERGIE_TEXT.splitlines() if not line.startswith("Gas - Modelcontract")
        )
        # Sanity: the gas section header really is gone from the input.
        assert "Gas - Modelcontract variabel" not in elec_only

        result = PureEnergieModelcontractParser().parse(elec_only)
        assert result["power"][0].rate == pytest.approx(round((0.39665 + 0.33910) / 2, 6))

    def test_gas_section_before_electricity_still_scopes_correctly(self):
        # The other half of that fix: `gas_start > elec_start` is false
        # when gas comes first too, not only when gas is absent. Untested until
        # now -- the real PDF always puts electricity first.
        lines = PURE_ENERGIE_TEXT.splitlines()
        gas_at = next(i for i, ln in enumerate(lines) if ln.startswith("Gas - Modelcontract"))
        elec_at = next(i for i, ln in enumerate(lines) if ln.startswith("Elektriciteit - Modelcontract"))
        # Move the whole gas block (to end of document) above the electricity
        # header, preserving the header line that carries the effective date.
        reordered = "\n".join(lines[:elec_at] + lines[gas_at:] + lines[elec_at:gas_at])

        result = PureEnergieModelcontractParser().parse(reordered)
        # Electricity rates must still come from the electricity section...
        assert result["power"][0].rate == pytest.approx(round((0.39665 + 0.33910) / 2, 6))
        # ...and gas from the gas section, not from electricity's Enkel row.
        assert result["gas"][0].rate == pytest.approx(1.26350)

    def test_single_column_teruglevertarieven_rows_not_mistaken_for_the_real_table(self):
        # "Enkel 0,01500" (Teruglevertarieven) has only one number, unlike
        # the real "Enkel 0,24789 0,12286 0,37075" row -- only one power
        # period and one gas period may come out despite "Enkel" appearing
        # 6 times total across both tables in the fixture.
        result = PureEnergieModelcontractParser().parse(PURE_ENERGIE_TEXT)
        assert len(result["power"]) == 1
        assert len(result["gas"]) == 1

    def test_gas_section_scoping_excludes_the_electricity_enkel_row(self):
        # Both sections use the tier word "Enkel" with the same 3-number
        # shape -- gas must come from *after* the Gas header, not from
        # Elektriciteit's Enkel row (0,37075) reused by mistake.
        result = PureEnergieModelcontractParser().parse(PURE_ENERGIE_TEXT)
        assert result["gas"][0].rate != pytest.approx(0.37075)


class TestInnovaEnergieModelcontractParser:
    """Fixture is verbatim real text from the real downloadable PDF
    -- see the module docstring above for provenance."""

    def test_detects_by_title_and_brand(self):
        assert InnovaEnergieModelcontractParser().detect(INNOVA_TEXT) is True

    def test_does_not_detect_other_documents(self):
        assert InnovaEnergieModelcontractParser().detect(POWER_TEXT_2026) is False
        assert InnovaEnergieModelcontractParser().detect(VATTENFALL_TARIEVENBLAD_TEXT) is False

    def test_date_regex_alone_is_not_enough_to_detect(self):
        # "Tariefblad per DD-MM-YYYY" without the Innova Energie brand text
        # must not be mistaken for a real Innova document.
        assert InnovaEnergieModelcontractParser().detect("Tariefblad per 01-07-2025\nsome other supplier entirely") is False

    def test_effective_date_is_stated_by_the_document(self):
        from src.db import OPEN_ENDED_SENTINEL

        result = InnovaEnergieModelcontractParser().parse(INNOVA_TEXT)
        assert result["power"][0].period_start == "2025-07-01"
        assert result["power"][0].period_end == OPEN_ENDED_SENTINEL

    def test_power_rate_averages_normaal_and_dal_totaal_incl_btw(self):
        # (0,30980 + 0,29306) / 2 -- the literal "n.v.t." ODE column between
        # Energiebelasting and Btw must not break the match.
        result = InnovaEnergieModelcontractParser().parse(INNOVA_TEXT)
        assert result["power"][0].rate == pytest.approx(round((0.30980 + 0.29306) / 2, 6))

    def test_gas_rate_from_the_unlabeled_totaal_row(self):
        # The gas table's data row has no tier-word prefix at all --
        # anchored on the "Leveringstarieven Gas" section header instead.
        result = InnovaEnergieModelcontractParser().parse(INNOVA_TEXT)
        assert result["gas"][0].rate == pytest.approx(1.41859)


class TestMegaEnergieModelcontractParser:
    """Fixture is verbatim real text from the real downloadable PDF
    -- see the module docstring above for provenance, including the stale-
    search-result trap this document's own live link avoided."""

    def test_detects_by_title_and_date(self):
        assert MegaEnergieModelcontractParser().detect(MEGA_TEXT) is True

    def test_does_not_detect_other_documents(self):
        assert MegaEnergieModelcontractParser().detect(POWER_TEXT_2026) is False
        assert MegaEnergieModelcontractParser().detect(PURE_ENERGIE_TEXT) is False

    def test_effective_date_is_stated_by_the_document(self):
        from src.db import OPEN_ENDED_SENTINEL

        result = MegaEnergieModelcontractParser().parse(MEGA_TEXT)
        assert result["power"][0].period_start == "2026-01-01"
        assert result["power"][0].period_end == OPEN_ENDED_SENTINEL

    def test_power_rate_averages_normaal_and_dal(self):
        # (0,46182 + 0,29665) / 2 -- tier words are spaced ("Enkel tarief"),
        # a real spelling difference from every other parser, not a typo.
        result = MegaEnergieModelcontractParser().parse(MEGA_TEXT)
        assert result["power"][0].rate == pytest.approx(round((0.46182 + 0.29665) / 2, 6))

    def test_gas_rate_is_totaal(self):
        result = MegaEnergieModelcontractParser().parse(MEGA_TEXT)
        assert result["gas"][0].rate == pytest.approx(1.33163)

    def test_single_euro_figure_terugleveringstarieven_rows_not_mistaken_for_the_real_table(self):
        # "Enkel tarief € 0,05000" (Terugleveringstarieven) has only one
        # €-figure, unlike the real 3-figure row -- only one power period
        # and one gas period may come out.
        result = MegaEnergieModelcontractParser().parse(MEGA_TEXT)
        assert len(result["power"]) == 1
        assert len(result["gas"]) == 1


class TestCleanEnergyModelcontractParser:
    """Fixture is verbatim real text from the real downloadable PDF
    -- see the module docstring above for provenance."""

    def test_detects_by_date_and_rate_rows(self):
        assert CleanEnergyModelcontractParser().detect(CLEAN_ENERGY_TEXT) is True

    def test_does_not_detect_other_documents(self):
        assert CleanEnergyModelcontractParser().detect(POWER_TEXT_2026) is False
        assert CleanEnergyModelcontractParser().detect(MEGA_TEXT) is False

    def test_effective_date_is_stated_by_the_document(self):
        from src.db import OPEN_ENDED_SENTINEL

        result = CleanEnergyModelcontractParser().parse(CLEAN_ENERGY_TEXT)
        assert result["power"][0].period_start == "2026-01-01"
        assert result["power"][0].period_end == OPEN_ENDED_SENTINEL

    def test_power_rate_is_the_totaal_row_not_the_exclusief_energiebelasting_row(self):
        # Each tier appears twice in this document -- "{tier} totaal"
        # (wanted, incl. btw = 0,28025) and "{tier} (exclusief
        # energiebelasting)" (0,16940) a few lines down. The literal word
        # "totaal" in the regex is what keeps these apart.
        result = CleanEnergyModelcontractParser().parse(CLEAN_ENERGY_TEXT)
        assert result["power"][0].rate == pytest.approx(0.28025)
        assert result["power"][0].rate != pytest.approx(0.16940)

    def test_gas_rate_is_levering_gas_totaal(self):
        result = CleanEnergyModelcontractParser().parse(CLEAN_ENERGY_TEXT)
        assert result["gas"][0].rate == pytest.approx(1.30760)

    def test_only_one_power_and_gas_period_each(self):
        # enkeltarief/normaaltarief/daltarief all resolve to the same
        # 0,28025 in this document -- confirms averaging normaal+dal still
        # produces exactly one period, not three.
        result = CleanEnergyModelcontractParser().parse(CLEAN_ENERGY_TEXT)
        assert len(result["power"]) == 1
        assert len(result["gas"]) == 1


class TestTodaySeam:
    """_today() is the injection seam every live-webpage parser uses for
    its import-date period_start -- a plain wrapper is trivial, but every
    other parser's date-determinism test depends on monkeypatching it
    correctly, so it's worth locking in that it does what it says."""

    def test_is_the_local_date_not_the_hosts_utc_date(self):
        # The reference deployment runs Etc/UTC while the rest of the app works in
        # Europe/Amsterdam (app.py's _LOCAL_TZ). A bare date.today() therefore
        # returned *yesterday* between 00:00 and 02:00 local time, dating a
        # rate period a day early and shrinking the preceding open-ended row a
        # day too far back. Verified live on the reference deployment inside that window:
        #     date.today()         -> 2026-08-08
        #     Amsterdam local date -> 2026-08-09
        # No mocking: this compares against the same wall clock the app must
        # agree with, so it fails on any host whose timezone is not Amsterdam
        # if the local-time conversion is ever removed again.
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from src import tariff_parser

        assert tariff_parser._today() == datetime.now(ZoneInfo("Europe/Amsterdam")).date().isoformat()

    def test_uses_the_same_timezone_as_the_rest_of_the_app(self):
        # The actual invariant behind the earlier bug: tariff_parser was the one
        # module not sharing the app's timezone. Asserting the two agree
        # catches a future divergence (either module's default changed, or
        # one stops honoring OMNIMETER_TIMEZONE) regardless of which tz the
        # host happens to run.
        #
        # Deliberately NOT done by reloading the module under a patched env
        # var: importlib.reload() rebinds the module's class objects, so
        # TariffCsvError imported by this test file stops matching the one
        # raised by the reloaded code, and every CSV test after it fails.
        from src import app as appmod
        from src import tariff_parser

        assert tariff_parser._LOCAL_TZ == appmod._LOCAL_TZ

    def test_returns_an_iso_date(self):
        from datetime import date

        from src.tariff_parser import _today

        assert _today() == date.today().isoformat()


class TestParseTariffCsv:
    """The generic fallback for suppliers with no PDF
    parser. Fails on the first bad row rather than best-effort skipping --
    a hand-typed CSV is far more error-prone than a supplier's own PDF."""

    def test_valid_rows_parsed(self):
        csv_text = "category,period_start,period_end,rate\npower,2026-01-01,2026-06-30,0.245\ngas,2026-01-01,,1.35\n"
        result = parse_tariff_csv(csv_text)
        assert result["power"] == [RatePeriod("2026-01-01", "2026-06-30", 0.245)]
        assert result["gas"] == [RatePeriod("2026-01-01", "9999-12-31", 1.35)]

    def test_blank_period_end_becomes_open_ended_sentinel(self):
        from src.db import OPEN_ENDED_SENTINEL

        result = parse_tariff_csv("power,2026-01-01,,0.25\n")
        assert result["power"][0].period_end == OPEN_ENDED_SENTINEL

    def test_comment_and_blank_lines_ignored(self):
        csv_text = "# a comment\n\npower,2026-01-01,,0.25\n# trailing comment\n"
        result = parse_tariff_csv(csv_text)
        assert len(result["power"]) == 1

    def test_header_row_skipped_even_if_not_first(self):
        csv_text = "# comment first\ncategory,period_start,period_end,rate\npower,2026-01-01,,0.25\n"
        result = parse_tariff_csv(csv_text)
        assert len(result["power"]) == 1

    def test_the_downloadable_template_itself_imports_nothing(self, tmp_path, monkeypatch):
        # Reads the REAL template off the route rather than a copy of its
        # text. The previous version of this test pasted the template inline
        # and asserted 2 power + 1 gas -- it tracked the template happily
        # while missing that those "example" rows were live data.
        # A copy can only ever confirm the copy.
        monkeypatch.setenv("OMNIMETER_DB_PATH", str(tmp_path / "t.db"))
        monkeypatch.setenv("OMNIMETER_WRITE_API_TOKEN", "t")
        from src.app import create_app

        client = create_app().test_client()
        template = client.get("/api/import/tariff-csv/template").get_data(as_text=True)

        result = parse_tariff_csv(template)
        assert result["power"] == []
        assert result["gas"] == []
        # ...and the examples are present, just inert -- so the template is
        # still a usable starting point, not an empty file.
        assert "#power,2026-01-01,2026-06-30,0.245" in template

    def test_wrong_column_count_rejected_with_line_number(self):
        with pytest.raises(TariffCsvError, match="line 1"):
            parse_tariff_csv("power,2026-01-01,0.25\n")

    @pytest.mark.parametrize("bad", ["nan", "NaN", "inf", "-inf", "Infinity"])
    def test_non_finite_rate_rejected(self, bad):
        # float() accepts all of these, and `nan < 0` is False, so
        # the old bare negativity check let NaN straight through to the DB
        # where it silently poisons every downstream average and estimate.
        with pytest.raises(TariffCsvError, match="line 1"):
            parse_tariff_csv(f"power,2026-01-01,,{bad}\n")

    def test_ct_per_kwh_unit_mistake_rejected(self):
        # The template's own comment warns "Not ct/kWh", but
        # nothing enforced it: a bill's "24,5 ct/kWh" typed as 24.5 was
        # stored as 24.5 EUR/kWh -- a silent 100x cost error.
        with pytest.raises(TariffCsvError, match="implausibly high"):
            parse_tariff_csv("power,2026-01-01,,24.5\n")

    def test_plausible_rates_still_accepted(self):
        # The guard must not reject real tariffs, including an extreme
        # market spike -- it is a unit check, not a price policy.
        result = parse_tariff_csv("power,2026-01-01,,0.245\ngas,2026-01-01,,9.99\n")
        assert result["power"][0].rate == 0.245
        assert result["gas"][0].rate == 9.99

    def test_row_with_blank_category_cell_raises_rather_than_being_skipped(self):
        # ",2026-01-01,2026-06-30,0.245" -- category cleared by
        # accident, or an Excel export that shifted a column. This used to
        # be treated as a blank line and silently dropped, returning 200
        # with fewer periods than the user supplied. That is exactly the
        # partial acceptance this parser's reject-the-whole-file rule
        # exists to prevent.
        with pytest.raises(TariffCsvError, match="line 1"):
            parse_tariff_csv(",2026-01-01,2026-06-30,0.245\n")

    def test_csv_level_fault_raises_tariff_csv_error_not_a_raw_csv_error(self):
        # The route now lets non-TariffCsvError exceptions become a
        # 500, so a genuine bad-input fault at the csv layer must be converted
        # here or it would wrongly read as a server defect.
        #
        # An oversized field is the trigger that actually reaches csv.Error on
        # this path (default limit 131072). NUL bytes and unterminated quotes
        # were both checked and do NOT raise when the source is a StringIO --
        # that behaviour belongs to the bytes/file reader.
        oversized = "power,2026-01-01,," + ("x" * 200_000) + "\n"
        with pytest.raises(TariffCsvError, match="malformed CSV"):
            parse_tariff_csv(oversized)

    def test_genuinely_blank_rows_still_ignored(self):
        # This fix must not turn whitespace-only or empty rows into
        # errors -- Excel exports routinely end with a few of those.
        result = parse_tariff_csv("power,2026-01-01,,0.25\n\n   \n,,,\n")
        assert len(result["power"]) == 1

    def test_unknown_category_rejected(self):
        with pytest.raises(TariffCsvError, match="category"):
            parse_tariff_csv("water,2026-01-01,,0.25\n")

    def test_malformed_start_date_rejected(self):
        with pytest.raises(TariffCsvError, match="period_start"):
            parse_tariff_csv("power,01-01-2026,,0.25\n")

    def test_malformed_end_date_rejected(self):
        with pytest.raises(TariffCsvError, match="period_end"):
            parse_tariff_csv("power,2026-01-01,not-a-date,0.25\n")

    def test_end_before_start_rejected(self):
        with pytest.raises(TariffCsvError, match="before"):
            parse_tariff_csv("power,2026-06-30,2026-01-01,0.25\n")

    def test_non_numeric_rate_rejected(self):
        with pytest.raises(TariffCsvError, match="rate"):
            parse_tariff_csv("power,2026-01-01,,not-a-number\n")

    def test_negative_rate_rejected(self):
        with pytest.raises(TariffCsvError, match="negative"):
            parse_tariff_csv("power,2026-01-01,,-0.25\n")

    def test_second_bad_row_still_reported_with_correct_line_number(self):
        csv_text = "power,2026-01-01,2026-06-30,0.245\npower,2026-07-01,,not-a-number\n"
        with pytest.raises(TariffCsvError, match="line 2"):
            parse_tariff_csv(csv_text)
