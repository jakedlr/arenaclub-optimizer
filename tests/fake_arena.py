"""An in-memory stand-in for ArenaClient, and builders for Arena Club JSON.

Tests use it so that nothing ever calls the live API.
"""

import copy
from typing import Any

from arenaclub_optimizer.arena_client import ArenaApiError

type Json = dict[str, Any]


def level_json(
    level_id: str,
    name: str,
    price_cents: int,
    pack_ids: list[str],
    product_type: str = "card",
) -> Json:
    return {
        "id": level_id,
        "name": name,
        "numItems": 1,
        "category": product_type,
        "priceCents": price_cents,
        "position": 1,
        "repackSeriesIds": list(pack_ids),
    }


def tier_json(name: str, tier_type: str, percent: str, card_ids: list[str]) -> Json:
    return {
        "name": name,
        "type": tier_type,
        "position": 1,
        "formattedHitRate": percent,
        "itemIds": list(card_ids),
    }


def pack_json(
    pack_id: str,
    category: str,
    level: str,
    price_cents: int,
    tiers: list[Json],
    *,
    status: str = "active",
    offer_enabled: bool = True,
    offer: float = 0.9,
    insured: float = 0.8,
    insurance: bool = True,
) -> Json:
    return {
        "id": pack_id,
        "name": category,
        "status": status,
        "isInsuranceAvailable": insurance,
        "isOfferEnabled": offer_enabled,
        "offerPercentage": offer,
        "offerInsuredPercentage": insured,
        "category": {
            "id": f"level-{level}",
            "name": level,
            "numItems": 1,
            "priceCents": price_cents,
            "category": "card",
        },
        "tiers": tiers,
    }


def card_json(card_id: str, cert: str | None = None, **fields: Any) -> Json:
    body = {
        "id": card_id,
        "category": "card",
        "year": 2021,
        "setName": "2021 Pokemon SWSH",
        "insert": "Evolving Skies",
        "playerName": f"Player {card_id}",
        "setNumber": "215",
        "gradingCompany": "psa",
        "overall": 10,
        "status": "available",
        "priceBadge": "none",
        "clPriceBadge": "good",
    }
    if cert is not None:
        body["cardCertNumber"] = cert
    body.update(fields)
    return body


class FakeArena:
    """Serves canned JSON like ArenaClient and records every call.

    `errors` maps ("pack" | "card", id) to an exception to raise for that call.
    Unknown ids raise ArenaApiError 404, as the real client would.
    """

    def __init__(
        self,
        levels: list[Json],
        packs: dict[str, Json],
        cards: dict[str, Json],
    ) -> None:
        self.levels = levels
        self.packs = packs
        self.cards = cards
        self.errors: dict[tuple[str, str], Exception] = {}
        self.calls: list[tuple[str, str | None]] = []
        self.closed = False

    def __enter__(self) -> FakeArena:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.closed = True

    def list_price_levels(self) -> list[Json]:
        self.calls.append(("levels", None))
        error = self.errors.get(("levels", ""))
        if error is not None:
            raise error
        return copy.deepcopy(self.levels)

    def get_slab_pack(self, pack_id: str) -> Json:
        return self._serve("pack", pack_id, self.packs)

    def get_card(self, card_id: str) -> Json:
        return self._serve("card", card_id, self.cards)

    def _serve(self, kind: str, key: str, store: dict[str, Json]) -> Json:
        self.calls.append((kind, key))
        error = self.errors.get((kind, key))
        if error is not None:
            raise error
        if key not in store:
            raise ArenaApiError(f"GET {kind} {key}: HTTP 404", 404)
        return copy.deepcopy(store[key])

    def requested(self, kind: str) -> list[str]:
        return [key for k, key in self.calls if k == kind and key is not None]


def sample_arena() -> FakeArena:
    """Gold and Silver Pokemon, Gold Football, and a watch Price Level.

    Gold Pokemon: card_gp_hit (0.5%, cert 11111111) and card_gp_base (99.5%).
    Silver Pokemon: card_sp_1 and card_sp_2 at 50% each.
    Gold Football: card_gf_1 at 100%.
    """
    levels = [
        level_json("lvl-gold", "Gold", 10000, ["gold-pokemon", "gold-football"]),
        level_json("lvl-silver", "Silver", 5000, ["silver-pokemon"]),
        level_json("lvl-watch", "Dusk", 25000, ["dusk-watch"], product_type="watch"),
    ]
    packs = {
        "gold-pokemon": pack_json(
            "gold-pokemon",
            "Pokemon",
            "Gold",
            10000,
            [
                tier_json("Grail", "grail", "0.5", ["card_gp_hit"]),
                tier_json("Lineup", "line_up", "99.5", ["card_gp_base"]),
            ],
        ),
        "silver-pokemon": pack_json(
            "silver-pokemon",
            "Pokemon",
            "Silver",
            5000,
            [tier_json("Lineup", "line_up", "50", ["card_sp_1", "card_sp_2"])],
        ),
        "gold-football": pack_json(
            "gold-football",
            "Football",
            "Gold",
            10000,
            [tier_json("Lineup", "line_up", "100", ["card_gf_1"])],
        ),
        "dusk-watch": pack_json("dusk-watch", "Watches", "Dusk", 25000, []),
    }
    cards = {
        "card_gp_hit": card_json("card_gp_hit", cert="11111111"),
        "card_gp_base": card_json("card_gp_base", cert="22222222"),
        "card_sp_1": card_json("card_sp_1"),
        "card_sp_2": card_json("card_sp_2"),
        "card_gf_1": card_json("card_gf_1"),
    }
    return FakeArena(levels, packs, cards)
