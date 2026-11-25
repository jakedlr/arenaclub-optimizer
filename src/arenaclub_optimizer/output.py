"""Render a RankingReport: the `rich` table, the summary line and the JSON."""

from typing import Any

from rich.table import Table
from rich.text import Text

from arenaclub_optimizer.config import Config
from arenaclub_optimizer.ranking import (
    RankedPack,
    RankingReport,
    SkippedPack,
    UnrankablePack,
    pack_name,
)

__all__ = ["JSON_VERSION", "report_json", "results_table", "summary_line"]

# Bump when a key is renamed or removed; adding keys keeps the version.
JSON_VERSION = 1

_DASH = "—"
_COLUMNS = (
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
)


def summary_line(report: RankingReport) -> str:
    return (
        f"{len(report.results)} ranked, "
        f"{len(report.unrankable)} unrankable (low Value Coverage), "
        f"{len(report.skipped)} skipped"
    )


def results_table(report: RankingReport, k: float) -> Table:
    """The Top Results as a table, best first. `k` goes in the caption, since
    Buyback EV rests on it. Every cell is a `Text`, so Arena Club data such as
    "Football [Japanese]" is never parsed as rich markup."""
    table = Table(
        title="Top Results",
        caption=f"Buyback EV is an estimate: AC Value = k × Market Value, k = {k:g}.",
    )
    for header in _COLUMNS:
        text = header in ("Slab Pack", "Notes")
        table.add_column(header, justify="left" if text else "right")
    for result in report.results:
        table.add_row(*_row(result))
    return table


def report_json(report: RankingReport, config: Config) -> dict[str, Any]:
    """The report as a JSON-ready dict, with the `config` it was ranked under.
    Fractions stay fractions (0.05 = 5%) and amounts are in dollars."""
    return {
        "version": JSON_VERSION,
        "generated_at": report.generated_at.isoformat(),
        "settings": _settings_json(config),
        "summary": {
            "ranked": len(report.results),
            "unrankable": len(report.unrankable),
            "skipped": len(report.skipped),
        },
        "results": [
            _result_json(position, result)
            for position, result in enumerate(report.results, start=1)
        ],
        "unrankable": [_unrankable_json(entry) for entry in report.unrankable],
        "skipped": [_skipped_json(entry) for entry in report.skipped],
    }


def _row(result: RankedPack) -> list[Text]:
    ev, risk = result.ev, result.risk
    slab_safe = _DASH if ev.slab_safe_ev is None else _signed_money(ev.slab_safe_ev)
    cells = [
        pack_name(result.pack),
        f"${result.pack.price:,.2f}",
        f"{ev.buyback_ev_pct:+.1%}",
        _signed_money(ev.buyback_ev),
        f"{ev.market_ev_pct:+.1%}",
        slab_safe,
        _probability(risk.p_profit),
        f"{risk.median_ratio:.2f}×",
        f"{risk.top_heaviness:.0%}",
        f"{ev.value_coverage:.0%}",
        "\n".join(result.notes),
    ]
    return [Text(cell) for cell in cells]


def _signed_money(amount: float) -> str:
    sign = "-" if amount < 0 else "+"
    return f"{sign}${abs(amount):,.2f}"


def _probability(p: float) -> str:
    """A probability as a percentage, without rounding a small one to 0%."""
    if 0 < p < 0.001:
        return "<0.1%"
    return f"{p:.1%}"


def _settings_json(config: Config) -> dict[str, Any]:
    filters = config.filters
    return {
        "k": config.k,
        "payment_method": config.payment_method.value,
        "tax_rate": config.tax_rate,
        "coverage_threshold": config.coverage_threshold,
        "p_profit_threshold": config.p_profit_threshold,
        "top_heaviness_threshold": config.top_heaviness_threshold,
        "filters": {
            "categories": list(filters.categories),
            "price_min": filters.price_min,
            "price_max": filters.price_max,
            "min_ev_pct": filters.min_ev_pct,
            "min_p_profit": filters.min_p_profit,
            "exclude_high_variance": filters.exclude_high_variance,
            "top": filters.top,
        },
    }


def _result_json(position: int, result: RankedPack) -> dict[str, Any]:
    pack, ev, risk = result.pack, result.ev, result.risk
    return {
        "rank": position,
        "pack_id": pack.pack_id,
        "slab_pack": pack_name(pack),
        "price_level": pack.price_level,
        "category": pack.category,
        "price": pack.price,
        "cost": ev.cost,
        "buyback_ev": ev.buyback_ev,
        "buyback_ev_pct": ev.buyback_ev_pct,
        "market_ev": ev.market_ev,
        "market_ev_pct": ev.market_ev_pct,
        "slab_safe_ev": ev.slab_safe_ev,
        "value_coverage": ev.value_coverage,
        "lower_bound": ev.lower_bound,
        "missing_top_tiers": list(ev.missing_top_tiers),
        "p_profit": risk.p_profit,
        "p_at_least": {"1x": result.p_1x, "2x": result.p_2x, "10x": result.p_10x},
        "std_ratio": risk.std_ratio,
        "median_ratio": risk.median_ratio,
        "top_heaviness": risk.top_heaviness,
        "high_variance": risk.high_variance,
        "notes": list(result.notes),
    }


def _unrankable_json(entry: UnrankablePack) -> dict[str, Any]:
    pack = entry.pack
    return {
        "pack_id": pack.pack_id,
        "slab_pack": pack_name(pack),
        "price_level": pack.price_level,
        "category": pack.category,
        "price": pack.price,
        "value_coverage": entry.value_coverage,
    }


def _skipped_json(entry: SkippedPack) -> dict[str, Any]:
    return {"pack_id": entry.pack_id, "slab_pack": entry.name, "reason": entry.reason}
