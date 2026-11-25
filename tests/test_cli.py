import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from importlib.metadata import version
from pathlib import Path

import pytest
from fake_arena import sample_arena
from fake_values import NOW, Clock, FakeProvider, quote
from typer.testing import CliRunner

from arenaclub_optimizer import cli
from arenaclub_optimizer.arena_client import (
    ArenaApiError,
    ArenaBlockedError,
    ArenaClient,
)
from arenaclub_optimizer.cli import app
from arenaclub_optimizer.model import ValueConfidence
from arenaclub_optimizer.output import overrides_snippet
from arenaclub_optimizer.valuation import (
    ReviewEntry,
    ValueSources,
    open_value_sources,
)
from arenaclub_optimizer.values import CreditBudget, ProviderUnavailable, ValueCache

runner = CliRunner()

# Gold Pokemon: EV % about +43.7%, P(profit) 0.5%, so it carries a High-variance
# Note. Gold Football: EV % +8%, no note. Silver Pokemon: Value Coverage 50%.
SAMPLE_OVERRIDES = """\
["11111111"]
market_value = 20000

[card_gp_base]
market_value = 60

[card_sp_1]
market_value = 60

[card_gf_1]
market_value = 120
"""

WIDE = {"COLUMNS": "250"}
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture
def config_dir(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    directory = tmp_path / "xdg" / "arenaclub-optimizer"
    directory.mkdir(parents=True)
    (directory / "overrides.toml").write_text(SAMPLE_OVERRIDES, encoding="utf-8")
    return directory


class FakeValueSources:
    """Stands in for `open_value_sources`: a value cache in a temp dir, a
    budget on a fixed clock, and the fake providers a test puts in
    `providers` (none by default). The cache outlives each run, as the real
    one does."""

    def __init__(self, directory: Path) -> None:
        self.cache = ValueCache(directory)
        self.budget = CreditBudget(self.cache, clock=Clock(NOW))
        self.providers: list = []
        self.runs = 0

    @contextmanager
    def __call__(self):
        self.runs += 1
        yield ValueSources(tuple(self.providers), self.cache, self.budget)


@pytest.fixture
def values(tmp_path, monkeypatch):
    fake = FakeValueSources(tmp_path / "values")
    monkeypatch.setattr(cli, "make_value_sources", fake)
    yield fake
    fake.cache.close()


@pytest.fixture
def arena(config_dir, values, monkeypatch):
    fake = sample_arena()
    monkeypatch.setattr(cli, "make_client", lambda: fake)
    return fake


def write_config(config_dir: Path, text: str) -> None:
    (config_dir / "config.toml").write_text(text, encoding="utf-8")


def invoke(*args: str):
    return runner.invoke(app, ["rank", *args], env=WIDE)


def rank_json(*args: str) -> dict:
    result = invoke("--json", *args)
    assert result.exit_code == 0, result.stderr
    return json.loads(result.stdout)


def result_ids(document: dict) -> list[str]:
    return [r["pack_id"] for r in document["results"]]


# --- general ----------------------------------------------------------------------


def test_version_prints_package_version():
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout == version("arenaclub-optimizer") + "\n"


def test_watch_is_still_a_stub():
    result = runner.invoke(app, ["watch"])

    assert result.exit_code != 0
    assert result.stderr == "arena watch: not implemented yet\n"


def test_the_default_client_is_the_real_arena_client():
    assert cli.make_client is ArenaClient


# --- table output -------------------------------------------------------------------


def test_rank_prints_a_table_and_the_summary_line(arena):
    result = invoke()

    assert result.exit_code == 0, result.stderr
    assert "Gold Pokemon" in result.stdout
    assert "Gold Football" in result.stdout
    assert result.stdout.rstrip().endswith(
        "2 ranked, 1 unrankable (low Value Coverage), 0 skipped; "
        "values: 4 overridden, 0 from cache, 0 fetched, 1 need review"
    )


def plain(stdout: str) -> str:
    return ANSI_ESCAPE.sub("", stdout)


def header_text(stdout: str) -> str:
    """The table's header rows (between the title and the rule under them),
    where a header may be wrapped onto two lines, e.g. "Buyback" over "EV %"."""
    lines = plain(stdout).splitlines()
    title = next(i for i, line in enumerate(lines) if "Top Results" in line)
    rule = next(i for i, line in enumerate(lines) if "━━━" in line and i > title)
    return "\n".join(lines[title + 1 : rule])


# One word or line of each header; full header names are checked in test_output.
COMPACT_HEADER_WORDS = ["Slab", "Price", "Buyback", "P(profit)", "median×", "heaviness"]
WIDE_ONLY_HEADER_WORDS = ["Market", "Slab Safe", "Coverage"]


def test_default_table_has_the_compact_columns(arena):
    headers = header_text(invoke().stdout)

    for word in COMPACT_HEADER_WORDS:
        assert word in headers
    for word in [*WIDE_ONLY_HEADER_WORDS, "Notes"]:
        assert word not in headers


def test_wide_table_adds_market_ev_slab_safe_ev_and_coverage(arena):
    headers = header_text(invoke("--wide").stdout)

    for word in [*COMPACT_HEADER_WORDS, *WIDE_ONLY_HEADER_WORDS]:
        assert word in headers
    assert "Notes" not in headers


def test_table_rows_are_in_buyback_ev_pct_order(arena):
    stdout = invoke().stdout

    assert stdout.index("Gold Pokemon") < stdout.index("Gold Football")


def test_table_shows_the_high_variance_note(arena):
    stdout = invoke().stdout

    assert "Only" in stdout


# --- layout at 80 columns ------------------------------------------------------

NARROW = {"COLUMNS": "80"}


def invoke_at(env: dict[str, str], *args: str):
    result = runner.invoke(app, ["rank", *args], env=env)
    assert result.exit_code == 0, result.stderr
    return plain(result.stdout)


def assert_fits(stdout: str, width: int) -> None:
    assert "…" not in stdout
    for line in stdout.splitlines():
        assert len(line) <= width, line


def test_default_table_fits_80_columns_and_truncates_nothing(arena):
    stdout = invoke_at(NARROW)

    assert_fits(stdout, 80)
    for word in COMPACT_HEADER_WORDS:
        assert word in header_text(stdout)
    # Gold Pokemon, then Gold Football, every figure in full.
    for cell in ["$100.00", "+43.7%", "+$43.73", "0.5%", "+8.0%", "+$8.00", "100.0%"]:
        assert cell in stdout


def test_default_table_fits_80_columns_with_long_names_and_big_numbers(
    arena, config_dir
):
    # A $10,000 Slab Pack with a long name, and a large Buyback EV $.
    (config_dir / "overrides.toml").write_text(
        '["11111111"]\nmarket_value = 9999999\n[card_gp_base]\nmarket_value = 60\n'
        "[card_gf_1]\nmarket_value = 120\n",
        encoding="utf-8",
    )
    arena.levels[0]["priceCents"] = 1_000_000
    pokemon = arena.packs["gold-pokemon"]
    pokemon["category"].update(name="Black Diamond", priceCents=1_000_000)
    pokemon["name"] = "One Piece Japanese Edition"

    stdout = invoke_at(NARROW)

    assert_fits(stdout, 80)
    # 0.9 x (0.005 x 9,999,999 + 0.995 x 60) - 10,000 = 35,053.7255
    for cell in ["$10,000.00", "+350.5%", "+$35,053.73", "One", "Edition"]:
        assert cell in stdout


def test_wide_table_truncates_nothing_on_a_wide_terminal(arena):
    stdout = invoke_at(WIDE, "--wide")

    assert "…" not in stdout
    for word in [*COMPACT_HEADER_WORDS, *WIDE_ONLY_HEADER_WORDS]:
        assert word in header_text(stdout)
    # Market EV % and Slab Safe EV $ for Gold Pokemon and Gold Football.
    for cell in ["+59.7%", "+$59.60", "+20.0%", "-$2.00", "100%"]:
        assert cell in stdout


def test_default_table_truncates_nothing_on_a_wide_terminal(arena):
    stdout = invoke_at(WIDE)

    assert "…" not in stdout
    assert "+$43.73" in stdout


def test_notes_print_on_their_own_lines_under_the_table(arena):
    stdout = invoke_at(NARROW)

    lines = stdout.splitlines()
    caption = next(i for i, line in enumerate(lines) if "k = 1." in line)
    notes = lines.index("Notes:")
    [note] = [i for i, line in enumerate(lines) if line.startswith("- Gold Pokemon: ")]
    assert caption < notes < note
    assert "Only" in lines[note]
    assert not any(line.startswith("- Gold Football") for line in lines)


def test_no_notes_heading_without_notes(arena):
    stdout = invoke_at(NARROW, "--category", "football")

    assert "Notes:" not in stdout


def test_wide_table_also_prints_notes_under_the_table(arena):
    stdout = invoke_at(WIDE, "--wide")

    assert "Notes:" in stdout.splitlines()
    assert any(line.startswith("- Gold Pokemon: Only") for line in stdout.splitlines())


def test_notes_and_skipped_lines_wrap_instead_of_truncating(arena):
    arena.packs["gold-football"]["isOfferEnabled"] = False

    stdout = invoke_at(NARROW)

    assert_fits(stdout, 80)
    assert "Skipped Gold Football:" in stdout


def test_wide_table_at_80_columns_runs_past_the_edge_rather_than_truncate(arena):
    stdout = invoke_at(NARROW, "--wide")

    assert "…" not in stdout
    for cell in ["$100.00", "+43.7%", "+$43.73", "+59.7%", "+$59.60", "-$2.00"]:
        assert cell in stdout
    # The last column, Coverage, is not cropped at the edge either.
    assert "Coverage" in header_text(stdout)
    rows = [line for line in stdout.splitlines() if line.startswith("Gold ")]
    assert len(rows) == 2
    assert all(row.rstrip().endswith(" 100%") for row in rows)
    # Pack names wrap between words, never inside one.
    for word in ["Gold", "Pokemon", "Football"]:
        assert word in stdout
    # The lines after the table still wrap at the terminal's width.
    assert_fits(stdout[stdout.index("Notes:") :], 80)


def test_default_table_on_a_very_narrow_terminal_truncates_nothing(arena):
    stdout = invoke_at({"COLUMNS": "50"})

    assert "…" not in stdout
    for cell in ["$100.00", "+43.7%", "+$43.73", "100.0%", "Pokemon", "Football"]:
        assert cell in stdout


def test_wide_option_leaves_json_unchanged(arena):
    narrow = rank_json()
    wide = rank_json("--wide")

    for document in (narrow, wide):
        del document["generated_at"]
    assert narrow == wide


def test_summary_counts_skipped_packs(arena):
    arena.packs["gold-football"]["isOfferEnabled"] = False

    result = invoke()

    assert result.exit_code == 0
    assert "1 ranked, 1 unrankable (low Value Coverage), 1 skipped" in result.stdout


def test_skipped_packs_are_listed_with_their_reason(arena):
    arena.packs["gold-football"]["isOfferEnabled"] = False

    stdout = invoke().stdout

    assert "Gold Football" in stdout
    assert "Buyback" in stdout


def test_no_results_still_prints_the_summary(arena, config_dir):
    (config_dir / "overrides.toml").write_text("", encoding="utf-8")

    result = invoke()

    assert result.exit_code == 0
    assert "0 ranked, 3 unrankable (low Value Coverage), 0 skipped" in result.stdout


# --- JSON output ----------------------------------------------------------------------


def test_json_document_has_a_stable_shape(arena):
    document = rank_json()

    assert set(document) == {
        "version",
        "generated_at",
        "settings",
        "summary",
        "results",
        "unrankable",
        "skipped",
        "unavailable_sources",
    }
    assert document["version"] == 2
    assert datetime.fromisoformat(document["generated_at"]).tzinfo is not None
    assert document["summary"] == {
        "ranked": 2,
        "unrankable": 1,
        "skipped": 0,
        "values": {
            "override": 4,
            "cache": 0,
            "stale_cache": 0,
            "fetched": 0,
            "needs_review": 1,
        },
    }
    assert result_ids(document) == ["gold-pokemon", "gold-football"]


def test_json_result_fields(arena):
    pokemon = rank_json()["results"][0]

    assert set(pokemon) == {
        "rank",
        "pack_id",
        "slab_pack",
        "price_level",
        "category",
        "price",
        "cost",
        "buyback_ev",
        "buyback_ev_pct",
        "market_ev",
        "market_ev_pct",
        "slab_safe_ev",
        "value_coverage",
        "lower_bound",
        "missing_top_tiers",
        "p_profit",
        "p_at_least",
        "std_ratio",
        "median_ratio",
        "top_heaviness",
        "high_variance",
        "notes",
        "cards",
    }
    assert pokemon["rank"] == 1
    assert pokemon["slab_pack"] == "Gold Pokemon"
    assert pokemon["price"] == 100.0
    assert pokemon["cost"] == 100.0
    # 0.9 x (0.005 x 20000 + 0.995 x 60) - 100
    assert pokemon["buyback_ev"] == pytest.approx(43.73)
    assert pokemon["buyback_ev_pct"] == pytest.approx(0.4373)
    assert pokemon["value_coverage"] == pytest.approx(1.0)
    assert pokemon["p_profit"] == pytest.approx(0.005)
    assert set(pokemon["p_at_least"]) == {"1x", "2x", "10x"}
    assert pokemon["p_at_least"]["10x"] == pytest.approx(0.005)
    assert pokemon["high_variance"] is True
    assert len(pokemon["notes"]) == 1


def test_json_lists_unrankable_and_skipped_packs(arena):
    arena.packs["gold-football"]["isOfferEnabled"] = False

    document = rank_json()

    [unrankable] = document["unrankable"]
    assert unrankable["pack_id"] == "silver-pokemon"
    assert unrankable["slab_pack"] == "Silver Pokemon"
    assert unrankable["value_coverage"] == pytest.approx(0.5)
    [skipped] = document["skipped"]
    assert skipped["pack_id"] == "gold-football"
    assert skipped["slab_pack"] == "Gold Football"
    assert "Buyback" in skipped["reason"]


def test_json_slab_safe_ev_is_null_without_slab_safe(arena):
    arena.packs["gold-football"]["isInsuranceAvailable"] = False

    football = rank_json()["results"][1]

    assert football["slab_safe_ev"] is None


def test_json_settings_default_to_the_config_defaults(arena):
    assert rank_json()["settings"] == {
        "k": 1.0,
        "payment_method": "wallet",
        "tax_rate": 0.0,
        "coverage_threshold": 0.95,
        "p_profit_threshold": 0.25,
        "top_heaviness_threshold": 0.5,
        "filters": {
            "categories": [],
            "price_min": None,
            "price_max": None,
            "min_ev_pct": None,
            "min_p_profit": None,
            "exclude_high_variance": False,
            "top": 10,
        },
    }


def test_json_settings_show_the_effective_values_with_fractions(arena, config_dir):
    write_config(
        config_dir,
        'payment_method = "ach"\ntax_rate = 0.07\ncoverage_threshold = 0.9\n'
        "p_profit_threshold = 0.3\ntop_heaviness_threshold = 0.4\n"
        "[filters]\nprice_min = 20\ntop = 4\n",
    )

    settings = rank_json(
        "--k",
        "0.8",
        "--category",
        "pokemon",
        "--price-max",
        "150",
        "--min-ev",
        "5",
        "--min-p-profit",
        "20",
        "--exclude-high-variance",
    )["settings"]

    assert settings == {
        "k": 0.8,
        "payment_method": "ach",
        "tax_rate": 0.07,
        "coverage_threshold": 0.9,
        "p_profit_threshold": 0.3,
        "top_heaviness_threshold": 0.4,
        "filters": {
            "categories": ["pokemon"],
            "price_min": 20.0,
            "price_max": 150.0,
            "min_ev_pct": pytest.approx(0.05),
            "min_p_profit": pytest.approx(0.2),
            "exclude_high_variance": True,
            "top": 4,
        },
    }


# --- data is never read as rich markup ----------------------------------------------

MARKUP_OVERRIDES = """\
[card_gp_base]
market_value = 60

[card_gf_1]
market_value = 120
"""


def test_markup_in_arena_data_is_printed_literally(arena, config_dir):
    # Gold Pokemon's Grail card is left unvalued, so the lower-bound note names
    # its Tier; Gold Football has Buyback disabled, so it is skipped by name.
    (config_dir / "overrides.toml").write_text(MARKUP_OVERRIDES, encoding="utf-8")
    pokemon = arena.packs["gold-pokemon"]
    pokemon["name"] = "Pokemon [Japanese] [/]"
    pokemon["category"]["name"] = "Gold [/]"  # an unmatched close tag
    pokemon["tiers"][0]["name"] = "[bold]Grail[/]"
    football = arena.packs["gold-football"]
    football["name"] = "Football [Japanese] [/]"
    football["isOfferEnabled"] = False

    result = runner.invoke(app, ["rank"], env={"COLUMNS": "400"})

    assert result.exit_code == 0, result.stderr
    assert result.exception is None
    text = ANSI_ESCAPE.sub("", result.stdout)
    for token in ["[bold]", "[Japanese]", "[/]", "[bold]Grail[/]"]:
        assert token in text
    assert "Skipped Gold Football [Japanese] [/]:" in text
    assert "1 ranked, 1 unrankable (low Value Coverage), 1 skipped" in text


def test_markup_in_arena_data_survives_json_output(arena):
    arena.packs["gold-football"]["name"] = "Football [Japanese] [/]"

    document = rank_json("--category", "football [japanese] [/]")

    assert document["results"][0]["category"] == "Football [Japanese] [/]"


# --- options and config -----------------------------------------------------------


def test_category_option_limits_fetching(arena):
    document = rank_json("--category", "football")

    assert result_ids(document) == ["gold-football"]
    assert document["unrankable"] == []
    assert arena.requested("card") == ["card_gf_1"]


def test_category_option_is_repeatable(arena):
    document = rank_json("--category", "Football", "--category", "POKEMON")

    assert result_ids(document) == ["gold-pokemon", "gold-football"]


def test_config_categories_apply_and_the_option_overrides_them(arena, config_dir):
    write_config(config_dir, '[filters]\ncategories = ["Pokemon"]\n')

    assert result_ids(rank_json()) == ["gold-pokemon"]
    assert result_ids(rank_json("--category", "football")) == ["gold-football"]


def test_top_option(arena):
    assert result_ids(rank_json("--top", "1")) == ["gold-pokemon"]


def test_top_option_overrides_config(arena, config_dir):
    write_config(config_dir, "[filters]\ntop = 1\n")

    assert len(rank_json()["results"]) == 1
    assert len(rank_json("--top", "2")["results"]) == 2


def test_min_ev_is_a_percentage(arena):
    # Gold Football's EV % is +8%: 10 means 10%, so it drops out ...
    assert result_ids(rank_json("--min-ev", "10")) == ["gold-pokemon"]
    # ... while 0.1 means 0.1%, so both stay.
    assert len(rank_json("--min-ev", "0.1")["results"]) == 2
    assert len(rank_json("--min-ev", "-50")["results"]) == 2


def test_min_p_profit_is_a_percentage(arena):
    assert result_ids(rank_json("--min-p-profit", "50")) == ["gold-football"]
    assert len(rank_json("--min-p-profit", "0.4")["results"]) == 2


def test_exclude_high_variance_option(arena):
    assert result_ids(rank_json("--exclude-high-variance")) == ["gold-football"]


def test_include_high_variance_overrides_config(arena, config_dir):
    write_config(config_dir, "[filters]\nexclude_high_variance = true\n")

    assert result_ids(rank_json()) == ["gold-football"]
    assert len(rank_json("--include-high-variance")["results"]) == 2


def test_price_options(arena):
    document = rank_json("--price-min", "60", "--price-max", "100")

    assert len(document["results"]) == 2
    assert document["unrankable"] == []


def test_price_option_overrides_config(arena, config_dir):
    write_config(config_dir, "[filters]\nprice_max = 75\n")

    assert rank_json()["results"] == []
    assert len(rank_json("--price-max", "100")["results"]) == 2


def test_price_options_skip_price_levels_before_fetching(arena):
    document = rank_json("--price-min", "60")

    assert result_ids(document) == ["gold-pokemon", "gold-football"]
    assert "silver-pokemon" not in arena.requested("pack")
    # 1 Price Level listing + 2 Slab Packs + 3 cards, instead of 9.
    assert len(arena.calls) == 6


def test_config_price_filter_skips_price_levels_before_fetching(arena, config_dir):
    write_config(config_dir, "[filters]\nprice_max = 75\n")

    rank_json()

    assert arena.requested("pack") == ["silver-pokemon"]
    assert len(arena.calls) == 4


def test_rank_keeps_its_own_price_check_after_the_pre_filter(arena):
    # The Price Level says $100, but the pack's own JSON says $150: the
    # pre-filter lets it through and `rank` drops it on its own price.
    arena.packs["gold-football"]["category"]["priceCents"] = 15000

    document = rank_json("--price-max", "100")

    assert "gold-football" in arena.requested("pack")
    assert result_ids(document) == ["gold-pokemon"]


def test_k_option_changes_buyback_ev(arena):
    football = rank_json("--k", "0.5", "--category", "football")["results"][0]

    # 0.9 x 0.5 x 120 - 100
    assert football["buyback_ev"] == pytest.approx(-46.0)


def test_help_documents_percent_units(arena):
    result = runner.invoke(app, ["rank", "--help"], env=WIDE)

    # Rich styles help text when it detects CI (e.g. GitHub Actions).
    text = ANSI_ESCAPE.sub("", result.stdout)
    assert result.exit_code == 0
    assert "--min-ev" in text
    assert "percent" in text.lower()


def help_text() -> str:
    """`rank --help` as plain text: no styling, no panel borders, one space
    between words, so that wrapped help reads as one string."""
    result = runner.invoke(app, ["rank", "--help"], env=WIDE)
    assert result.exit_code == 0
    text = re.sub(r"[│╭╮╰╯─]", " ", ANSI_ESCAPE.sub("", result.stdout))
    return " ".join(text.split())


def test_help_says_defaults_come_from_the_config_file(arena):
    text = help_text()

    assert "config file" in text
    assert "config.toml" in text
    # The built-in defaults are only the fallback, and are named as such.
    assert "Default: 10." not in text
    assert "Default: 1." not in text
    assert "Default: config file, else 10." in text
    assert "Default: config file, else 1." in text
    assert "Default: config file, else all." in text


def test_help_documents_the_wide_option(arena):
    text = help_text()

    assert "--wide" in text
    assert "Market EV %" in text


@pytest.mark.parametrize(
    ("args", "option"),
    [
        (["--k", "0"], "--k"),
        (["--k", "-1"], "--k"),
        (["--k", "nan"], "--k"),
        (["--k", "inf"], "--k"),
        (["--top", "0"], "--top"),
        (["--top", "-3"], "--top"),
        (["--min-p-profit", "150"], "--min-p-profit"),
        (["--min-p-profit", "-1"], "--min-p-profit"),
        (["--min-ev", "nan"], "--min-ev"),
        (["--min-ev", "inf"], "--min-ev"),
        (["--price-min", "-5"], "--price-min"),
        (["--price-max", "-5"], "--price-max"),
        (["--price-min", "nan"], "--price-min"),
        (["--price-min", "50", "--price-max", "10"], "--price-min"),
    ],
)
def test_invalid_option_values_exit_2_before_any_request(arena, args, option):
    result = invoke(*args)

    assert result.exit_code == 2
    assert option in result.stderr
    assert "Traceback" not in result.stderr
    assert arena.calls == []


def test_price_min_above_config_price_max_exits_2(arena, config_dir):
    write_config(config_dir, "[filters]\nprice_max = 40\n")

    result = invoke("--price-min", "50")

    assert result.exit_code == 2
    assert "--price-min" in result.stderr


def test_invalid_config_file_exits_2(arena, config_dir):
    write_config(config_dir, "k = -1\n")

    result = invoke()

    assert result.exit_code == 2
    assert "config.toml" in result.stderr
    assert arena.calls == []


def test_invalid_overrides_file_exits_2(arena, config_dir):
    (config_dir / "overrides.toml").write_text("[x]\nmarket_value = -1\n")

    result = invoke()

    assert result.exit_code == 2
    assert "overrides.toml" in result.stderr


# --- errors -----------------------------------------------------------------------


@pytest.mark.parametrize("status", [403, 429])
def test_a_block_prints_a_clear_message_and_exits_2(arena, status):
    arena.errors[("pack", "gold-football")] = ArenaBlockedError(status, None)

    result = invoke()

    assert result.exit_code == 2
    assert "blocked" in result.stderr.lower()
    assert str(status) in result.stderr
    assert "Traceback" not in result.stderr
    assert arena.closed is True
    assert arena.calls[-1] == ("pack", "gold-football")


def test_a_failed_price_level_listing_exits_1(arena):
    arena.errors[("levels", "")] = ArenaApiError("GET /v4/repacks: timed out")

    result = invoke()

    assert result.exit_code == 1
    assert "timed out" in result.stderr


def test_the_client_is_closed_after_a_run(arena):
    invoke()

    assert arena.closed is True


def test_json_output_keeps_stdout_pure_json(arena):
    result = invoke("--json")

    json.loads(result.stdout)


# --- values -----------------------------------------------------------------------

CITATION = (
    "Player card_sp_2 215/203 (Evolving Skies). CardVolume, "
    "https://cardvolume.com/pokemon/evolving-skies/player-card-sp-2/ "
    "(accessed 2026-11-24)."
)


def player(card_id: str) -> str:
    """The card name `fake_arena.card_json` gives a card."""
    return f"Player {card_id}"


def unavailable(reason: str, *, lasting: bool = False) -> FakeProvider:
    error = ProviderUnavailable(reason=reason, lasting=lasting)
    return FakeProvider("pokeprice", lambda _: error)


def no_key() -> FakeProvider:
    """PokéPrice without a key: unavailable until the user sets one."""
    return unavailable("PokéPrice key not set", lasting=True)


def cards_by_id(document: dict) -> dict[str, dict]:
    return {c["card_id"]: c for r in document["results"] for c in r["cards"]}


def no_overrides(config_dir: Path) -> None:
    (config_dir / "overrides.toml").write_text("", encoding="utf-8")


def test_the_default_value_sources_are_the_real_ones():
    assert cli.make_value_sources is open_value_sources


def test_overrides_win_then_pokeprice_then_cardvolume(arena, values):
    pokeprice = FakeProvider(
        "pokeprice",
        {
            player("card_gp_hit"): quote(1.0, "pokeprice"),
            player("card_sp_2"): quote(70.0, "pokeprice"),
        },
    )
    cardvolume = FakeProvider(
        "cardvolume", {player("card_sp_2"): quote(80.0, "cardvolume")}
    )
    values.providers = [pokeprice, cardvolume]

    cards = cards_by_id(rank_json())

    assert cards["card_gp_hit"]["value"]["source"] == "override"
    assert cards["card_gp_hit"]["market_value"] == 20000
    assert cards["card_sp_2"]["value"]["source"] == "pokeprice"
    assert cards["card_sp_2"]["market_value"] == 70.0
    assert player("card_gp_hit") not in pokeprice.called_names
    assert cardvolume.calls == []


def test_cardvolume_values_the_cards_pokeprice_cannot(arena, values):
    cardvolume = FakeProvider(
        "cardvolume",
        {player("card_sp_2"): quote(80.0, "cardvolume", citation=CITATION)},
    )
    values.providers = [no_key(), cardvolume]

    document = rank_json()

    value = cards_by_id(document)["card_sp_2"]["value"]
    assert value["source"] == "cardvolume"
    assert value["citation"] == CITATION
    assert document["unavailable_sources"] == {"pokeprice": "PokéPrice key not set"}


def test_values_are_resolved_once_per_run(arena, values):
    down = unavailable("down")
    values.providers = [down]

    invoke()

    assert len(down.calls) == 1
    assert values.runs == 1


def test_only_cards_of_packs_matching_the_filters_are_valued(arena, values, config_dir):
    no_overrides(config_dir)
    provider = FakeProvider("pokeprice")
    values.providers = [provider]

    invoke("--category", "football")

    assert provider.called_names == [player("card_gf_1")]


def test_a_limited_budget_values_grail_and_chase_cards_first(arena, values, config_dir):
    no_overrides(config_dir)
    values.budget = CreditBudget(values.cache, {"pokeprice": 2}, clock=Clock(NOW))
    values.providers = [
        FakeProvider(
            "pokeprice",
            lambda _: quote(10.0, "pokeprice"),
            credit_cost=1,
            budget=values.budget,
        )
    ]

    document = review_document()

    # Priority: the Grail card first, then by Hit Rate: Gold Football's only
    # card (100%), Gold Pokemon's Lineup card (99.5%), Silver's two (50%).
    waiting = [entry["card_id"] for entry in document["waiting_on_budget"]]
    assert waiting == ["card_gp_base", "card_sp_1", "card_sp_2"]
    assert all(
        entry["reason"].startswith("budget exhausted")
        for entry in document["waiting_on_budget"]
    )
    assert document["needs_review"] == []


def test_the_summary_line_counts_values_from_cache_fetched_and_to_review(arena, values):
    values.providers = [
        FakeProvider("pokeprice", {player("card_sp_2"): quote(70.0, "pokeprice")})
    ]

    first = invoke().stdout
    second = invoke().stdout

    assert "; values: 4 overridden, 0 from cache, 1 fetched, 0 need review" in first
    assert "; values: 4 overridden, 1 from cache, 0 fetched, 0 need review" in second


def test_the_summary_line_without_overrides_or_providers(arena, config_dir):
    no_overrides(config_dir)

    stdout = invoke().stdout

    assert stdout.rstrip().endswith(
        "0 ranked, 3 unrankable (low Value Coverage), 0 skipped; "
        "values: 0 from cache, 0 fetched, 5 need review"
    )


def test_json_output_prints_the_summary_line_with_values_to_stderr(arena):
    result = invoke("--json")

    assert "values: 4 overridden, 0 from cache, 0 fetched, 1 need review" in (
        result.stderr
    )


def test_a_low_confidence_value_gets_a_note(arena, values):
    low = quote(70.0, "pokeprice", confidence=ValueConfidence.LOW)
    values.providers = [FakeProvider("pokeprice", {player("card_sp_2"): low})]

    stdout = plain(invoke().stdout)
    document = rank_json()

    assert "Silver Pokemon: 1 card valued at low confidence." in stdout
    [silver] = [r for r in document["results"] if r["pack_id"] == "silver-pokemon"]
    assert "1 card valued at low confidence." in silver["notes"]
    assert cards_by_id(document)["card_sp_2"]["value"]["confidence"] == "low"


def test_cardvolume_citations_appear_in_a_sources_footer(arena, values):
    values.providers = [
        FakeProvider(
            "cardvolume",
            {player("card_sp_2"): quote(70.0, "cardvolume", citation=CITATION)},
        )
    ]

    lines = plain(invoke().stdout).splitlines()

    start = lines.index("Sources:")
    assert lines[start + 1] == f"- {CITATION}"
    assert lines[-1].startswith("3 ranked")


def test_no_sources_footer_without_citations(arena):
    assert "Sources:" not in invoke().stdout


def test_an_unavailable_value_source_is_reported(arena, values):
    values.providers = [no_key()]

    stdout = plain(invoke().stdout)

    assert "Value source unavailable: pokeprice (PokéPrice key not set)" in stdout


def test_a_value_cache_that_cannot_open_exits_1(arena, monkeypatch):
    def broken():
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(cli, "make_value_sources", broken)

    result = invoke()

    assert result.exit_code == 1
    assert "value cache" in result.stderr
    assert "unable to open database file" in result.stderr
    assert "Traceback" not in result.stderr


# --- arena review -------------------------------------------------------------------


def review(*args: str, env: dict[str, str] | None = None):
    return runner.invoke(app, ["review", *args], env=env or WIDE)


def review_document(*args: str) -> dict:
    result = review("--json", *args)
    assert result.exit_code == 0, result.stderr
    return json.loads(result.stdout)


def reviewed_ids(*args: str) -> list[str]:
    return [entry["card_id"] for entry in review_document(*args)["needs_review"]]


def review_entries() -> list[ReviewEntry]:
    return [
        ReviewEntry(**{**entry, "slab_packs": tuple(entry["slab_packs"])})
        for entry in review_document()["needs_review"]
    ]


def test_review_lists_each_card_without_a_value(arena):
    result = review()

    assert result.exit_code == 0, result.stderr
    stdout = plain(result.stdout)
    for cell in ["card_sp_2", "Player card_sp_2", "no value providers"]:
        assert cell in stdout
    assert stdout.rstrip().endswith("1 card needs review")


def test_review_shows_the_cert_number(arena, config_dir):
    no_overrides(config_dir)

    stdout = plain(review().stdout)

    assert "11111111" in stdout
    assert stdout.rstrip().endswith("5 cards need review")


@pytest.mark.parametrize("columns", ["250", "40"])
def test_review_prints_the_overrides_snippet_unwrapped(arena, config_dir, columns):
    no_overrides(config_dir)

    stdout = plain(review(env={"COLUMNS": columns}).stdout)

    assert overrides_snippet(review_entries()) in stdout
    assert str(config_dir / "overrides.toml") in stdout.replace("\n", "")


def test_pasting_the_filled_in_snippet_empties_the_list(arena, config_dir):
    no_overrides(config_dir)
    snippet = overrides_snippet(review_entries())
    filled = snippet.replace("# market_value =", "market_value = 10")
    (config_dir / "overrides.toml").write_text(filled, encoding="utf-8")

    result = review()

    assert review_document()["needs_review"] == []
    assert "No cards need review." in result.stdout
    assert result.stdout.rstrip().endswith("0 cards need review")


def test_review_json(arena):
    document = review_document()

    assert document["version"] == 1
    assert document["summary"] == {"needs_review": 1}
    assert document["needs_review"] == [
        {
            "card_id": "card_sp_2",
            "cert_number": None,
            "label": "2021 Pokemon SWSH Evolving Skies Player card_sp_2 #215 PSA 10",
            "reason": "no value providers",
            "slab_packs": ["Silver Pokemon"],
        }
    ]


def test_review_json_keeps_stdout_pure_json(arena):
    result = review("--json")

    json.loads(result.stdout)
    assert "1 card needs review" in result.stderr


def test_review_reports_unavailable_value_sources(arena, values):
    values.providers = [no_key()]

    stdout = plain(review().stdout)
    document = review_document()

    assert "Value source unavailable: pokeprice (PokéPrice key not set)" in stdout
    assert document["unavailable_sources"] == {"pokeprice": "PokéPrice key not set"}
    assert document["needs_review"][0]["reason"] == "PokéPrice key not set"


def test_review_takes_the_slab_pack_filters_of_rank(arena, config_dir):
    no_overrides(config_dir)

    assert reviewed_ids("--category", "football") == ["card_gf_1"]
    assert reviewed_ids("--price-max", "60") == ["card_sp_1", "card_sp_2"]
    assert reviewed_ids("--price-min", "60", "--category", "Pokémon") == [
        "card_gp_hit",
        "card_gp_base",
    ]


def test_review_uses_the_config_file_filters(arena, config_dir):
    no_overrides(config_dir)
    write_config(config_dir, '[filters]\ncategories = ["Football"]\n')

    assert reviewed_ids() == ["card_gf_1"]


def test_review_fetches_only_the_cards_of_matching_packs(arena):
    review("--category", "football")

    assert arena.requested("card") == ["card_gf_1"]


def test_review_rejects_invalid_options_before_any_request(arena):
    result = review("--price-min", "50", "--price-max", "10")

    assert result.exit_code == 2
    assert "--price-min" in result.stderr
    assert arena.calls == []


@pytest.mark.parametrize("status", [403, 429])
def test_review_stops_at_a_block(arena, status):
    arena.errors[("pack", "gold-football")] = ArenaBlockedError(status, None)

    result = review()

    assert result.exit_code == 2
    assert "blocked" in result.stderr.lower()


def test_review_help_lists_its_options():
    result = runner.invoke(app, ["review", "--help"], env=WIDE)

    text = plain(result.stdout)
    for option in ["--category", "--price-min", "--price-max", "--json"]:
        assert option in text


# --- arena review: cards waiting on budget -------------------------------------------


def budget_for_two(values) -> None:
    """PokéPrice can quote two cards, then its budget runs out: the Grail
    card and Gold Football's card are valued, the other three wait."""
    values.budget = CreditBudget(values.cache, {"pokeprice": 2}, clock=Clock(NOW))
    values.providers = [
        FakeProvider(
            "pokeprice",
            lambda _: quote(10.0, "pokeprice"),
            credit_cost=1,
            budget=values.budget,
        )
    ]


def test_review_lists_cards_waiting_on_budget_in_their_own_section(
    arena, values, config_dir
):
    no_overrides(config_dir)
    budget_for_two(values)

    stdout = plain(review().stdout)

    assert "Waiting on budget" in stdout
    for card_id in ["card_gp_base", "card_sp_1", "card_sp_2"]:
        assert card_id in stdout
    assert "budget exhausted" in stdout
    assert stdout.rstrip().endswith("0 cards need review, 3 waiting on budget")


def test_cards_waiting_on_budget_are_left_out_of_the_snippet(arena, values, config_dir):
    no_overrides(config_dir)
    budget_for_two(values)

    stdout = plain(review().stdout)

    assert "market_value" not in stdout
    assert '["22222222"]' not in stdout
    assert '["card_sp_1"]' not in stdout


def test_a_temporarily_unavailable_source_puts_cards_in_waiting(arena, values):
    values.providers = [unavailable("PokéPrice HTTP 503 on /sets")]

    document = review_document()

    assert document["needs_review"] == []
    assert [e["card_id"] for e in document["waiting_on_budget"]] == ["card_sp_2"]


def test_a_missing_key_still_puts_cards_up_for_review(arena, values):
    values.providers = [no_key()]

    document = review_document()

    assert [e["card_id"] for e in document["needs_review"]] == ["card_sp_2"]
    assert document["waiting_on_budget"] == []


def test_the_snippet_header_says_to_fill_in_or_delete_each_entry(arena):
    stdout = plain(review().stdout).replace("\n", " ")

    assert "fill in each market_value or delete the entry" in stdout
