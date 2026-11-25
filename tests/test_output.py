import dataclasses
import tomllib
from datetime import UTC, date, datetime

import pytest
from fake_arena import sample_arena
from fake_values import NO_VALUES, quote, resolution
from rich.console import Console

from arenaclub_optimizer.config import Config, load_overrides
from arenaclub_optimizer.fetch import fetch_card_packs
from arenaclub_optimizer.model import ValueConfidence
from arenaclub_optimizer.output import (
    JSON_VERSION,
    note_lines,
    overrides_snippet,
    report_json,
    results_table,
    review_json,
    review_summary,
    review_table,
    source_lines,
    summary_line,
    unavailable_lines,
)
from arenaclub_optimizer.ranking import RankingReport, rank
from arenaclub_optimizer.valuation import ReviewEntry
from arenaclub_optimizer.values import NeedsReview, ValueOrigin, ValueResolution

WHEN = datetime(2026, 1, 1, tzinfo=UTC)
OVERRIDE = ValueOrigin.OVERRIDE
# Gold Pokemon carries a High-variance Note; Gold Football has none.
VALUES = resolution(
    {
        "card_gp_hit": quote(20000, "override", sales_count=None),
        "card_gp_base": quote(60, "override", sales_count=None),
        "card_gf_1": quote(120, "override", sales_count=None),
    },
    origins={"card_gp_hit": OVERRIDE, "card_gp_base": OVERRIDE, "card_gf_1": OVERRIDE},
)

COMPACT = [
    "Slab Pack",
    "Price",
    "Buyback EV %",
    "Buyback EV $",
    "P(profit)",
    "median×",
    "Top-heaviness",
]
WIDE = [
    "Slab Pack",
    "Price",
    "Buyback EV %",
    "Buyback EV $",
    "Market EV %",
    "Slab Safe EV $",
    "P(profit)",
    "median×",
    "Top-heaviness",
    "Coverage",
]


def report(values=VALUES, config: Config | None = None) -> RankingReport:
    packs = fetch_card_packs(sample_arena()).packs
    return rank(packs, config or Config(), values, generated_at=WHEN)


def headers(table) -> list[str]:
    """Each column's header, with a two-line header joined back into one:
    "Buyback" over "EV %" reads "Buyback EV %", "Top-" over "heaviness"
    reads "Top-heaviness"."""
    return [
        str(column.header).replace("-\n", "-").replace("\n", " ")
        for column in table.columns
    ]


def render(table, width: int) -> str:
    console = Console(width=width, no_color=True, record=True)
    console.print(table)
    return console.export_text()


def test_default_columns_are_the_compact_set():
    assert headers(results_table(report(), 1.0)) == COMPACT


def test_wide_columns_are_every_metric_column():
    assert headers(results_table(report(), 1.0, wide=True)) == WIDE


def test_neither_layout_has_a_notes_column():
    for wide in (False, True):
        assert "Notes" not in headers(results_table(report(), 1.0, wide=wide))


def test_numeric_columns_never_wrap():
    for wide in (False, True):
        table = results_table(report(), 1.0, wide=wide)
        for column in table.columns[1:]:
            assert column.no_wrap
            assert column.justify == "right"


def test_compact_table_fits_80_columns():
    text = render(results_table(report(), 1.0), 80)

    assert "…" not in text
    assert max(len(line) for line in text.splitlines()) <= 80


def test_wide_table_fits_120_columns():
    text = render(results_table(report(), 1.0, wide=True), 120)

    assert "…" not in text
    assert max(len(line) for line in text.splitlines()) <= 120


def test_caption_still_gives_k():
    text = render(results_table(report(), 0.9), 80)

    assert "k = 0.9" in text


def test_note_lines_name_the_pack_for_each_note():
    lines = note_lines(report())

    assert len(lines) == 1
    assert lines[0].startswith("Gold Pokemon: Only 0.5% of pulls")


def test_note_lines_are_empty_without_notes():
    assert note_lines(rank((), Config(), NO_VALUES)) == ()


# --- the summary line -----------------------------------------------------------


def counted(**origins: int) -> ValueResolution:
    """A resolution with `n` values of each origin, plus `needs_review`."""
    review = origins.pop("needs_review", 0)
    values, by_card = {}, {}
    for origin, n in origins.items():
        for i in range(n):
            card_id = f"card_{origin}_{i}"
            values[card_id] = quote(1.0, "pokeprice")
            by_card[card_id] = ValueOrigin(origin)
    reviews = [
        NeedsReview(f"card_r{i}", None, "label", "unmatched") for i in range(review)
    ]
    return resolution(values, origins=by_card, needs_review=reviews)


def test_the_summary_line_adds_the_value_counts():
    values = counted(cache=1, stale_cache=1, fetched=3, needs_review=2)

    assert summary_line(rank((), Config(), values)) == (
        "0 ranked, 0 unrankable (low Value Coverage), 0 skipped; "
        "values: 2 from cache, 3 fetched, 2 need review"
    )


def test_the_summary_line_counts_overrides_when_there_are_any():
    values = counted(override=2, cache=1, fetched=0, needs_review=1)

    assert summary_line(rank((), Config(), values)).endswith(
        "; values: 2 overridden, 1 from cache, 0 fetched, 1 need review"
    )


# --- Sources ----------------------------------------------------------------------

CV_SILVER = "Player card_sp_2 (Evolving Skies). CardVolume, https://cardvolume.com/a/"
CV_FOOTBALL = "Player card_gf_1. CardVolume, https://cardvolume.com/b/"


def cited_values(**extra) -> ValueResolution:
    values = dict(VALUES.values)
    values["card_sp_1"] = quote(70, "cardvolume", citation=CV_SILVER)
    values["card_sp_2"] = quote(70, "cardvolume", citation=CV_SILVER)
    values["card_gf_1"] = quote(120, "cardvolume", citation=CV_FOOTBALL)
    values.update(extra)
    return resolution(values)


def test_source_lines_cite_each_quote_of_the_shown_results_once():
    lines = source_lines(report(cited_values()))

    # Gold Pokemon (no citations), then Silver Pokemon, then Gold Football.
    assert lines == (CV_SILVER, CV_FOOTBALL)


def test_source_lines_leave_out_packs_that_are_not_shown():
    config = dataclasses.replace(
        Config(), filters=dataclasses.replace(Config().filters, top=1)
    )
    assert source_lines(report(cited_values(), config)) == ()


def test_source_lines_are_empty_without_citations():
    assert source_lines(report()) == ()


# --- value sources that were unavailable -------------------------------------------


def test_unavailable_lines_name_each_value_source_and_why():
    values = resolution(unavailable={"pokeprice": "PokéPrice key not set"})

    assert unavailable_lines(values) == (
        "Value source unavailable: pokeprice (PokéPrice key not set)",
    )


def test_unavailable_lines_are_empty_when_every_source_answered():
    assert unavailable_lines(VALUES) == ()


# --- JSON v2 ------------------------------------------------------------------------


def test_the_json_version_is_2():
    assert JSON_VERSION == 2
    assert report_json(report(), Config())["version"] == 2


def test_json_results_list_each_card_with_its_value():
    values = cited_values(
        card_gf_1=quote(
            120,
            "cardvolume",
            as_of=date(2026, 11, 20),
            sales_count=47,
            confidence=ValueConfidence.LOW,
            citation=CV_FOOTBALL,
        )
    )

    document = report_json(report(values), Config())

    [football] = [r for r in document["results"] if r["pack_id"] == "gold-football"]
    assert football["slab_pack"] == "Gold Football"
    assert football["cards"] == [
        {
            "card_id": "card_gf_1",
            "cert_number": None,
            "label": "2021 Pokemon SWSH Evolving Skies Player card_gf_1 #215 PSA 10",
            "tier": "Lineup",
            "tier_type": "line_up",
            "hit_rate": 1.0,
            "market_value": 120.0,
            "value": {
                "source": "cardvolume",
                "as_of": "2026-11-20",
                "sales_count": 47,
                "confidence": "low",
                "citation": CV_FOOTBALL,
            },
        }
    ]


def test_json_value_is_null_for_a_card_without_one():
    config = dataclasses.replace(Config(), coverage_threshold=0.4)
    values = resolution({**VALUES.values, "card_sp_1": quote(60, "pokeprice")})

    document = report_json(report(values, config), config)

    [silver] = [r for r in document["results"] if r["slab_pack"] == "Silver Pokemon"]
    by_id = {c["card_id"]: c for c in silver["cards"]}
    assert by_id["card_sp_2"]["value"] is None
    assert by_id["card_sp_2"]["market_value"] is None
    assert by_id["card_sp_1"]["value"]["source"] == "pokeprice"


def test_json_summary_counts_values_by_origin():
    values = counted(override=1, cache=2, stale_cache=3, fetched=4, needs_review=5)

    document = report_json(rank((), Config(), values), Config())

    assert document["summary"]["values"] == {
        "override": 1,
        "cache": 2,
        "stale_cache": 3,
        "fetched": 4,
        "needs_review": 5,
    }


def test_json_lists_unavailable_value_sources():
    values = resolution(unavailable={"pokeprice": "PokéPrice key not set"})

    document = report_json(rank((), Config(), values), Config())

    assert document["unavailable_sources"] == {"pokeprice": "PokéPrice key not set"}


# --- the Needs-review List -----------------------------------------------------------

UMBREON = ReviewEntry(
    card_id="card_umbreon",
    cert_number="12345678",
    label="2021 Pokemon SWSH Evolving Skies Umbreon VMAX #215 PSA 10",
    reason="unmatched by all providers",
    slab_packs=("Gold Pokemon",),
)
NO_CERT = ReviewEntry(
    card_id="card_ac",
    cert_number=None,
    label="Darkrai GX #88a Arena Club 9.5",
    reason="no raw price",
    slab_packs=("Gold Pokemon", "Silver Pokemon"),
)


def filled_in(snippet: str, value: str = "12.5") -> str:
    return snippet.replace("# market_value =", f"market_value = {value}")


def test_the_snippet_keys_on_the_cert_else_the_card_id():
    snippet = overrides_snippet([UMBREON, NO_CERT])

    data = tomllib.loads(snippet)

    assert data == {
        "12345678": {"note": UMBREON.label},
        "card_ac": {"note": NO_CERT.label},
    }


def test_the_snippet_names_the_slab_packs_and_reason_in_a_comment():
    snippet = overrides_snippet([NO_CERT])

    assert "# Gold Pokemon, Silver Pokemon: no raw price" in snippet.splitlines()


def test_a_filled_in_snippet_loads_as_manual_overrides(tmp_path):
    path = tmp_path / "overrides.toml"
    path.write_text(filled_in(overrides_snippet([UMBREON, NO_CERT])), encoding="utf-8")

    overrides = load_overrides(path)

    assert overrides.lookup("card_umbreon", "12345678") == 12.5
    assert overrides.lookup("card_ac", None) == 12.5


def test_a_snippet_pasted_without_a_value_fails_loudly(tmp_path):
    path = tmp_path / "overrides.toml"
    path.write_text(overrides_snippet([UMBREON]), encoding="utf-8")

    with pytest.raises(ValueError, match="market_value is required"):
        load_overrides(path)


def test_the_snippet_escapes_awkward_labels():
    label = 'Pokémon "Special" \\ path 🎉 \x7f\tend'
    entry = dataclasses.replace(
        UMBREON, label=label, reason="odd\nreason\x00", cert_number='8"AC\\'
    )

    data = tomllib.loads(filled_in(overrides_snippet([entry])))

    assert data == {'8"AC\\': {"note": label, "market_value": 12.5}}


def test_the_snippet_is_empty_without_entries():
    assert overrides_snippet([]) == ""


def test_review_summary_counts_the_cards():
    assert review_summary([]) == "0 cards need review"
    assert review_summary([UMBREON]) == "1 card needs review"
    assert review_summary([UMBREON, NO_CERT]) == "2 cards need review"


def test_the_review_table_shows_card_id_cert_label_and_reason():
    table = review_table([UMBREON, NO_CERT])

    assert headers(table) == ["Card id", "Cert", "Label", "Reason"]
    text = render(table, 200)
    for cell in [UMBREON.card_id, "12345678", "Umbreon VMAX", "unmatched by all"]:
        assert cell in text


def test_review_json_lists_each_card():
    document = review_json(
        [UMBREON, NO_CERT],
        Config(),
        unavailable={"pokeprice": "PokéPrice key not set"},
        generated_at=WHEN,
    )

    assert document["version"] == 1
    assert document["generated_at"] == WHEN.isoformat()
    assert document["summary"] == {"needs_review": 2, "waiting_on_budget": 0}
    assert document["settings"] == report_json(report(), Config())["settings"]
    assert document["unavailable_sources"] == {"pokeprice": "PokéPrice key not set"}
    assert document["needs_review"][1] == {
        "card_id": "card_ac",
        "cert_number": None,
        "label": NO_CERT.label,
        "reason": "no raw price",
        "slab_packs": ["Gold Pokemon", "Silver Pokemon"],
    }


# --- cards waiting on budget -----------------------------------------------------

WAITING = ReviewEntry(
    card_id="card_wait",
    cert_number="99999999",
    label="2021 Pokemon SWSH Evolving Skies Leafeon VMAX #205 PSA 10",
    reason="budget exhausted, retry after 2026-11-25 00:00 UTC",
    slab_packs=("Gold Pokemon",),
    waiting=True,
)


def test_review_entries_are_not_waiting_by_default():
    assert UMBREON.waiting is False


def test_the_snippet_leaves_out_cards_waiting_on_budget():
    data = tomllib.loads(overrides_snippet([UMBREON, WAITING, NO_CERT]))

    assert set(data) == {"12345678", "card_ac"}


def test_the_snippet_is_empty_when_every_card_is_waiting():
    assert overrides_snippet([WAITING]) == ""


def test_review_summary_counts_cards_waiting_on_budget_separately():
    assert review_summary([UMBREON, WAITING, NO_CERT]) == (
        "2 cards need review, 1 waiting on budget"
    )
    assert review_summary([WAITING]) == "0 cards need review, 1 waiting on budget"


def test_the_review_table_takes_a_title():
    table = review_table([WAITING], title="Waiting on budget")

    assert "Waiting on budget" in render(table, 200)


def test_review_json_lists_waiting_cards_on_their_own():
    document = review_json(
        [UMBREON, WAITING],
        Config(),
        unavailable={},
        generated_at=WHEN,
    )

    assert document["summary"] == {"needs_review": 1, "waiting_on_budget": 1}
    assert [e["card_id"] for e in document["needs_review"]] == ["card_umbreon"]
    assert document["waiting_on_budget"] == [
        {
            "card_id": "card_wait",
            "cert_number": "99999999",
            "label": WAITING.label,
            "reason": WAITING.reason,
            "slab_packs": ["Gold Pokemon"],
        }
    ]


# --- CardVolume attribution ---------------------------------------------------------


def test_a_cardvolume_value_without_a_citation_is_still_attributed():
    values = cited_values(card_gf_1=quote(120, "cardvolume", citation=None))

    lines = source_lines(report(values))

    assert lines == (CV_SILVER, "CardVolume, https://cardvolume.com/")
