"""Fetch every card Slab Pack from Arena Club and map it to the domain model.

The only module that drives the client for `rank`. It asks for as little as it
can: Category filtering happens before any card is requested, and packs the
ranking would leave out anyway (inactive, or with Buyback disabled) cost no
card requests.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from arenaclub_optimizer.arena_client import ArenaApiError, ArenaBlockedError
from arenaclub_optimizer.arena_mapping import (
    ArenaMappingError,
    PackRates,
    is_card_product,
    map_slab_pack,
    pack_rates,
)
from arenaclub_optimizer.model import SkippedPack, SlabPack, category_key

__all__ = ["ArenaSource", "FetchResult", "fetch_card_packs"]

type Json = dict[str, Any]


class ArenaSource(Protocol):
    """What fetching needs from the client; ArenaClient provides it."""

    def list_price_levels(self) -> list[Json]: ...

    def get_slab_pack(self, pack_id: str) -> Json: ...

    def get_card(self, card_id: str) -> Json: ...


@dataclass(frozen=True)
class FetchResult:
    """The fetched `(SlabPack, PackRates)` pairs, and the packs that failed."""

    packs: tuple[tuple[SlabPack, PackRates], ...]
    skipped: tuple[SkippedPack, ...]


def fetch_card_packs(
    client: ArenaSource,
    categories: Iterable[str] | None = None,
    *,
    progress: Callable[[int, int], None] | None = None,
) -> FetchResult:
    """Fetch every Slab Pack whose Product Type is card, with its cards.

    Lists the Price Levels, keeps the card ones, fetches each of their Slab
    Packs once, and drops any outside `categories` (compared like
    `model.category_key`; empty or None means all) before requesting cards.
    Cards go through the client, whose cache makes repeats free. An inactive
    pack, or one with Buyback disabled, comes back with an empty Checklist and
    costs no card requests, since `ranking.rank` leaves it out either way.

    A pack whose request or JSON fails goes under `skipped` and the rest carry
    on. `progress(done, total)` is called after each Slab Pack.

    Raises ArenaBlockedError at once if Arena Club blocks a request, and
    ArenaApiError if the Price Levels can't be listed.
    """
    wanted = {category_key(c) for c in categories or ()}
    queue = _card_pack_ids(client.list_price_levels())
    packs: list[tuple[SlabPack, PackRates]] = []
    skipped: list[SkippedPack] = []

    for done, (pack_id, level_name) in enumerate(queue, start=1):
        pack_json: Json | None = None
        try:
            pack_json = client.get_slab_pack(pack_id)
            if not wanted or category_key(str(pack_json.get("name"))) in wanted:
                packs.append(_fetch_one(client, pack_json))
        except ArenaBlockedError:
            raise
        except (ArenaApiError, ArenaMappingError, ValueError) as error:
            name = _name(pack_json, level_name, pack_id)
            skipped.append(SkippedPack(pack_id, name, str(error)))
        if progress is not None:
            progress(done, len(queue))

    return FetchResult(tuple(packs), tuple(skipped))


def _card_pack_ids(levels: list[Json]) -> list[tuple[str, str]]:
    """(pack id, Price Level name) for each card Slab Pack, each pack once."""
    seen: dict[str, str] = {}
    for level in levels:
        if not is_card_product(level):
            continue
        for pack_id in level.get("repackSeriesIds") or ():
            seen.setdefault(str(pack_id), str(level.get("name", "")))
    return list(seen.items())


def _fetch_one(client: ArenaSource, pack_json: Json) -> tuple[SlabPack, PackRates]:
    rates = pack_rates(pack_json)
    # Mapping without tiers needs no cards, and settles whether the pack is active.
    shell = map_slab_pack({**pack_json, "tiers": []}, {})
    if not shell.active or rates.buyback_rate is None:
        return shell, rates
    # Malformed tiers are left for map_slab_pack to report.
    tiers = [t for t in pack_json.get("tiers") or () if isinstance(t, dict)]
    card_ids = [card_id for tier in tiers for card_id in tier.get("itemIds") or ()]
    cards = {card_id: client.get_card(card_id) for card_id in card_ids}
    return map_slab_pack(pack_json, cards), rates


def _name(pack_json: Json | None, level_name: str, pack_id: str) -> str:
    """The pack as the user knows it, e.g. "Gold Pokemon", falling back to its
    Price Level and id when the pack JSON is missing."""
    category = pack_json.get("name") if pack_json else None
    if category:
        return f"{level_name} {category}".strip()
    return f"{level_name} ({pack_id})".strip()
