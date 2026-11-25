from datetime import UTC, datetime

from fake_arena import sample_arena
from rich.console import Console

from arenaclub_optimizer.config import Config, Override, Overrides
from arenaclub_optimizer.fetch import fetch_card_packs
from arenaclub_optimizer.output import note_lines, results_table
from arenaclub_optimizer.ranking import RankingReport, rank

# Gold Pokemon carries a High-variance Note; Gold Football has none.
OVERRIDES = Overrides(
    by_cert={"11111111": Override(20000)},
    by_card_id={"card_gp_base": Override(60), "card_gf_1": Override(120)},
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


def report() -> RankingReport:
    packs = fetch_card_packs(sample_arena()).packs
    return rank(
        packs, Config(), OVERRIDES, generated_at=datetime(2026, 1, 1, tzinfo=UTC)
    )


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
    assert note_lines(rank((), Config(), OVERRIDES)) == ()
