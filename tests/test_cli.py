import json
from datetime import datetime
from importlib.metadata import version
from pathlib import Path

import pytest
from fake_arena import sample_arena
from typer.testing import CliRunner

from arenaclub_optimizer import cli
from arenaclub_optimizer.arena_client import (
    ArenaApiError,
    ArenaBlockedError,
    ArenaClient,
)
from arenaclub_optimizer.cli import app

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


@pytest.fixture
def config_dir(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    directory = tmp_path / "xdg" / "arenaclub-optimizer"
    directory.mkdir(parents=True)
    (directory / "overrides.toml").write_text(SAMPLE_OVERRIDES, encoding="utf-8")
    return directory


@pytest.fixture
def arena(config_dir, monkeypatch):
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
        "2 ranked, 1 unrankable (low Value Coverage), 0 skipped"
    )


def test_table_has_the_agreed_columns(arena):
    result = invoke()

    for header in [
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
        "Notes",
    ]:
        assert header in result.stdout


def test_table_rows_are_in_buyback_ev_pct_order(arena):
    stdout = invoke().stdout

    assert stdout.index("Gold Pokemon") < stdout.index("Gold Football")


def test_table_shows_the_high_variance_note(arena):
    stdout = invoke().stdout

    assert "Only" in stdout


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
        "summary",
        "results",
        "unrankable",
        "skipped",
    }
    assert document["version"] == 1
    assert datetime.fromisoformat(document["generated_at"]).tzinfo is not None
    assert document["summary"] == {"ranked": 2, "unrankable": 1, "skipped": 0}
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


def test_k_option_changes_buyback_ev(arena):
    football = rank_json("--k", "0.5", "--category", "football")["results"][0]

    # 0.9 x 0.5 x 120 - 100
    assert football["buyback_ev"] == pytest.approx(-46.0)


def test_help_documents_percent_units(arena):
    result = runner.invoke(app, ["rank", "--help"], env=WIDE)

    assert result.exit_code == 0
    assert "--min-ev" in result.stdout
    assert "percent" in result.stdout.lower()


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
