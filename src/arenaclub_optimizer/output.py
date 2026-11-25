"""Render a RankingReport (the `rich` table, the summary line, the Sources
footer and the JSON) and the Needs-review List (a table, an overrides.toml
snippet and the JSON)."""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from rich import box
from rich.cells import cell_len
from rich.table import Table
from rich.text import Text

from arenaclub_optimizer.config import Config
from arenaclub_optimizer.model import ChecklistCard
from arenaclub_optimizer.ranking import (
    RankedPack,
    RankingReport,
    SkippedPack,
    UnrankablePack,
    pack_name,
)
from arenaclub_optimizer.valuation import ReviewEntry
from arenaclub_optimizer.values import ValueOrigin, ValueQuote, ValueResolution

__all__ = [
    "JSON_VERSION",
    "REVIEW_JSON_VERSION",
    "note_lines",
    "overrides_snippet",
    "report_json",
    "results_table",
    "review_json",
    "review_summary",
    "review_table",
    "source_lines",
    "summary_line",
    "unavailable_lines",
]

# Bump when a key is renamed or removed, or when a change needs readers to
# know (see README, "JSON output"); adding keys alone keeps the version.
JSON_VERSION = 2
REVIEW_JSON_VERSION = 1

_DASH = "—"


@dataclass(frozen=True)
class _Column:
    """A results-table column. `header` may break onto two lines, so a narrow
    terminal never truncates it; `wide` columns appear only with `--wide`."""

    header: str
    cell: Callable[[RankedPack], str]
    wide: bool = False


def _slab_safe(result: RankedPack) -> str:
    ev = result.ev.slab_safe_ev
    return _DASH if ev is None else _signed_money(ev)


_COLUMNS = (
    _Column("Price", lambda r: f"${r.pack.price:,.2f}"),
    _Column("Buyback\nEV %", lambda r: f"{r.ev.buyback_ev_pct:+.1%}"),
    _Column("Buyback\nEV $", lambda r: _signed_money(r.ev.buyback_ev)),
    _Column("Market\nEV %", lambda r: f"{r.ev.market_ev_pct:+.1%}", wide=True),
    _Column("Slab Safe\nEV $", _slab_safe, wide=True),
    _Column("P(profit)", lambda r: _probability(r.risk.p_profit)),
    _Column("median×", lambda r: f"{r.risk.median_ratio:.2f}×"),
    _Column("Top-\nheaviness", lambda r: f"{r.risk.top_heaviness:.0%}"),
    _Column("Coverage", lambda r: f"{r.ev.value_coverage:.0%}", wide=True),
)


def summary_line(report: RankingReport) -> str:
    """E.g. "2 ranked, 1 unrankable (low Value Coverage), 0 skipped; values:
    5 from cache, 3 fetched, 1 need review". Stale cached values count as
    from cache. Manual Overrides are counted first, when there are any."""
    counts = _value_counts(report.values)
    overridden = counts[ValueOrigin.OVERRIDE]
    from_cache = counts[ValueOrigin.CACHE] + counts[ValueOrigin.STALE_CACHE]
    values = [
        f"{from_cache} from cache",
        f"{counts[ValueOrigin.FETCHED]} fetched",
        f"{counts[_NEEDS_REVIEW]} need review",
    ]
    if overridden:
        values.insert(0, f"{overridden} overridden")
    return (
        f"{len(report.results)} ranked, "
        f"{len(report.unrankable)} unrankable (low Value Coverage), "
        f"{len(report.skipped)} skipped; values: {', '.join(values)}"
    )


def source_lines(report: RankingReport) -> tuple[str, ...]:
    """The citation of each Value Quote behind the Top Results, once each, in
    Top Results and Checklist order. CardVolume's terms ask for one wherever
    its figures are used. A quote without a citation adds nothing."""
    quotes = (
        report.values.values.get(card.card_id)
        for result in report.results
        for card in result.pack.checklist
    )
    citations = (q.citation for q in quotes if q is not None and q.citation)
    return tuple(dict.fromkeys(citations))


def unavailable_lines(values: ValueResolution) -> tuple[str, ...]:
    """One line per value source that stopped answering during the run."""
    return tuple(
        f"Value source unavailable: {name} ({reason})"
        for name, reason in values.unavailable.items()
    )


def results_table(report: RankingReport, k: float, *, wide: bool = False) -> Table:
    """The Top Results as a table, best first.

    The default columns fit 80 columns, rich's width when output is piped;
    `wide` adds Market EV %, Slab Safe EV $ and Coverage. Figures never wrap
    and the borderless layout leaves the room to the Slab Pack names, which
    wrap instead. Notes are not a column: see `note_lines`. `k` goes in the
    caption, since Buyback EV rests on it. Every cell is a `Text`, so Arena
    Club data such as "Football [Japanese]" is never parsed as rich markup.
    """
    table = Table(
        title="Top Results",
        caption=f"Buyback EV is an estimate: AC Value = k × Market Value, k = {k:g}.",
        box=box.SIMPLE_HEAVY,
        show_edge=False,
        pad_edge=False,
    )
    columns = [c for c in _COLUMNS if wide or not c.wide]
    names = [pack_name(result.pack) for result in report.results]
    # Wide enough for the longest word, so a name wraps between words; only
    # a word longer than the cap is split.
    longest = max((len(word) for name in names for word in name.split()), default=0)
    table.add_column("Slab Pack", overflow="fold", min_width=min(longest, 16))
    rows = [[c.cell(result) for c in columns] for result in report.results]
    for i, column in enumerate(columns):
        # rich measures a no-wrap column by its longest word, which would let
        # a narrow console crop it; an explicit width keeps every figure whole.
        lines = [*column.header.split("\n"), *(row[i] for row in rows)]
        width = max(cell_len(line) for line in lines)
        table.add_column(column.header, justify="right", no_wrap=True, min_width=width)
    for name, row in zip(names, rows, strict=True):
        table.add_row(*(Text(cell) for cell in [name, *row]))
    return table


def note_lines(report: RankingReport) -> tuple[str, ...]:
    """One "<Slab Pack>: <note>" line per Note, in Top Results order. They
    print under the table, where a sentence has the full width to wrap in."""
    return tuple(
        f"{pack_name(result.pack)}: {note}"
        for result in report.results
        for note in result.notes
    )


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
            "values": _value_counts(report.values),
        },
        "results": [
            _result_json(position, result, report.values)
            for position, result in enumerate(report.results, start=1)
        ],
        "unrankable": [_unrankable_json(entry) for entry in report.unrankable],
        "skipped": [_skipped_json(entry) for entry in report.skipped],
        "unavailable_sources": dict(report.values.unavailable),
    }


# --- the Needs-review List ---------------------------------------------------------


def review_summary(entries: Sequence[ReviewEntry]) -> str:
    """E.g. "3 cards need review"."""
    n = len(entries)
    return f"{n} card needs review" if n == 1 else f"{n} cards need review"


def review_table(entries: Iterable[ReviewEntry]) -> Table:
    """The Needs-review List as a table. Every cell is a `Text`, so Arena Club
    data is never parsed as rich markup."""
    table = Table(
        title="Needs review", box=box.SIMPLE_HEAVY, show_edge=False, pad_edge=False
    )
    table.add_column("Card id", overflow="fold")
    table.add_column("Cert", overflow="fold")
    table.add_column("Label", overflow="fold", ratio=2)
    table.add_column("Reason", overflow="fold", ratio=1)
    for entry in entries:
        cells = [entry.card_id, entry.cert_number or _DASH, entry.label, entry.reason]
        table.add_row(*(Text(cell) for cell in cells))
    return table


def overrides_snippet(entries: Iterable[ReviewEntry]) -> str:
    """An overrides.toml snippet with one entry per card, ready to paste.

    Each entry is keyed by the card's cert number when it has one (a cert
    match wins over a card id), else by its card id. It notes the label and
    has its `market_value` line commented out: the user fills it in, and an
    entry pasted without one fails to load rather than counting as $0. A
    comment above it gives the Slab Packs and the reason. "" for no entries.
    """
    blocks = []
    for entry in entries:
        where = f"{', '.join(entry.slab_packs)}: " if entry.slab_packs else ""
        key = entry.cert_number or entry.card_id
        lines = [
            f"# {_comment(where + entry.reason)}",
            f"[{_toml_string(key)}]",
            "# market_value =",
            f"note = {_toml_string(entry.label)}",
        ]
        blocks.append("".join(f"{line}\n" for line in lines))
    return "\n".join(blocks)


def review_json(
    entries: Iterable[ReviewEntry],
    config: Config,
    *,
    unavailable: Mapping[str, str],
    generated_at: datetime,
) -> dict[str, Any]:
    """The Needs-review List as a JSON-ready dict, with the `config` whose
    filters chose the Slab Packs."""
    rows = [
        {
            "card_id": entry.card_id,
            "cert_number": entry.cert_number,
            "label": entry.label,
            "reason": entry.reason,
            "slab_packs": list(entry.slab_packs),
        }
        for entry in entries
    ]
    return {
        "version": REVIEW_JSON_VERSION,
        "generated_at": generated_at.isoformat(),
        "settings": _settings_json(config),
        "summary": {"needs_review": len(rows)},
        "needs_review": rows,
        "unavailable_sources": dict(unavailable),
    }


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


def _result_json(
    position: int, result: RankedPack, values: ValueResolution
) -> dict[str, Any]:
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
        "cards": [_card_json(card, values) for card in pack.checklist],
    }


def _card_json(card: ChecklistCard, values: ValueResolution) -> dict[str, Any]:
    quote = values.values.get(card.card_id)
    return {
        "card_id": card.card_id,
        "cert_number": card.cert_number,
        "label": card.label,
        "tier": card.tier_name,
        "tier_type": card.tier_type.value,
        "hit_rate": card.hit_rate,
        "market_value": card.market_value,
        "value": None if quote is None else _value_json(quote),
    }


def _value_json(quote: ValueQuote) -> dict[str, Any]:
    return {
        "source": quote.source,
        "as_of": quote.as_of.isoformat(),
        "sales_count": quote.sales_count,
        "confidence": quote.confidence.value,
        "citation": quote.citation,
    }


_NEEDS_REVIEW = "needs_review"


def _value_counts(values: ValueResolution) -> dict[str, int]:
    """How many cards took their value from each origin, keyed by its value
    ("override", "cache", ...), and how many need review."""
    counts = {origin.value: 0 for origin in ValueOrigin}
    for origin in values.origins.values():
        counts[origin.value] += 1
    counts[_NEEDS_REVIEW] = len(values.needs_review)
    return counts


def _toml_string(text: str) -> str:
    """`text` as a TOML basic string: quotes, backslashes and control
    characters escaped, everything else (accents, emoji) kept."""
    escaped = []
    for char in text:
        if char in '"\\':
            escaped.append("\\" + char)
        elif _is_control(char) and char != "\t":
            escaped.append(f"\\u{ord(char):04X}")
        else:
            escaped.append(char)
    return '"' + "".join(escaped) + '"'


def _comment(text: str) -> str:
    """`text` fit for a TOML comment: control characters become spaces."""
    return "".join(" " if _is_control(char) else char for char in text)


def _is_control(char: str) -> bool:
    return char < " " or char == "\x7f"


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
