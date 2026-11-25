"""Map raw Arena Club JSON to the domain model in `model`.

Pure mapping: no I/O and no HTTP. Input shapes are documented in
`docs/research/arenaclub-api.md`; vocabulary follows GLOSSARY.md. Arena Club's
own field names differ from ours: a pack's `name` is its Category, its
`category.name` is its Price Level, and a Price Level's `category` is its
Product Type.
"""

from collections.abc import Mapping
from typing import Any, NamedTuple

from arenaclub_optimizer._pulls import DEFAULT_BUYBACK_RATE
from arenaclub_optimizer.ev import DEFAULT_SLAB_SAFE_FLOOR
from arenaclub_optimizer.model import (
    ChecklistCard,
    SlabPack,
    TierType,
    percent_to_probability,
)

__all__ = [
    "ARENA_CLUB_GRADER",
    "DEFAULT_BUYBACK_RATE",
    "DEFAULT_SLAB_SAFE_FLOOR",
    "ArenaMappingError",
    "PackRates",
    "build_label",
    "is_card_product",
    "map_slab_pack",
    "pack_rates",
]

CARD_PRODUCT_TYPE = "card"
ACTIVE_STATUS = "active"
BASE_NAME = "Base"
# `gradingCompany` for cards Arena Club graded itself.
ARENA_CLUB_GRADER = "arena_club"

# How grading companies read in a label. Others fall back to `_display_grader`.
_GRADER_NAMES = {
    "psa": "PSA",
    "bgs": "BGS",
    "cgc": "CGC",
    "sgc": "SGC",
    ARENA_CLUB_GRADER: "Arena Club",
}

type Json = Mapping[str, Any]


class ArenaMappingError(ValueError):
    """Arena Club JSON that can't be mapped to the domain model."""


class PackRates(NamedTuple):
    """A Slab Pack's Buyback rate and Slab Safe floor, as fractions.

    `buyback_rate` is None when Arena Club has the Buyback offer disabled.
    """

    buyback_rate: float | None
    slab_safe_floor: float


def map_slab_pack(pack_json: Json, cards_json: Mapping[str, Json]) -> SlabPack:
    """Build a SlabPack from a `/v4/repacks/{id}` response.

    `cards_json` maps each card id to its `/v3/items/{id}` (or `/v4/items`)
    response. Checklist cards come in tier order, then `itemIds` order, with no
    Market Value yet.

    Raises ArenaMappingError, naming the pack, for an unknown tier type, a card
    id missing from `cards_json`, a malformed Hit Rate or a missing field.
    """
    pack_id = str(pack_json.get("id", "<no id>"))
    try:
        price_level = pack_json["category"]
        checklist = tuple(
            card
            for tier in pack_json["tiers"]
            for card in _map_tier(pack_id, tier, cards_json)
        )
        return SlabPack(
            pack_id=pack_json["id"],
            price_level=price_level["name"],
            category=pack_json["name"],
            price=price_level["priceCents"] / 100,
            active=pack_json["status"] == ACTIVE_STATUS,
            checklist=checklist,
            insurance_available=bool(pack_json["isInsuranceAvailable"]),
        )
    except (KeyError, TypeError) as error:
        raise ArenaMappingError(
            f"Slab Pack {pack_id!r}: missing or malformed field {error}"
        ) from error


def pack_rates(pack_json: Json) -> PackRates:
    """A Slab Pack's rates from `offerPercentage` and `offerInsuredPercentage`.

    Missing or null rates default to DEFAULT_BUYBACK_RATE and
    DEFAULT_SLAB_SAFE_FLOOR. The Buyback rate is None when `isOfferEnabled` is
    false; a missing `isOfferEnabled` counts as enabled.

    Raises ArenaMappingError, naming the pack, for a rate that isn't a number
    or is outside (0, 1].
    """
    buyback_rate = _rate_or_default(pack_json, "offerPercentage", DEFAULT_BUYBACK_RATE)
    floor = _rate_or_default(
        pack_json, "offerInsuredPercentage", DEFAULT_SLAB_SAFE_FLOOR
    )
    if pack_json.get("isOfferEnabled") is False:
        buyback_rate = None
    return PackRates(buyback_rate, floor)


def is_card_product(price_level_json: Json) -> bool:
    """Whether a `/v4/repacks` Price Level's Product Type is card."""
    return price_level_json.get("category") == CARD_PRODUCT_TYPE


def build_label(card_json: Json) -> str:
    """A readable card label, e.g. "2021 Pokemon SWSH Evolving Skies Umbreon
    VMAX #215 PSA 10".

    Built from year, set name, insert, player name, parallel, set number,
    grader and grade, skipping empty parts. The year is left out when the set
    name already starts with it. Without an `insert`, the `subsetName` (v4
    items) stands in. A subset or parallel named "Base" is left out, and a
    parallel that starts with the player name loses that prefix. Falls back
    to the card id when every part is empty.
    """
    year = _text(card_json.get("year"))
    set_name = _text(card_json.get("setName"))
    insert = _text(card_json.get("insert")) or _not_base(card_json.get("subsetName"))
    player = _text(card_json.get("playerName"))
    parallel = _without_prefix(_not_base(card_json.get("parallelName")), player)
    set_number = _text(card_json.get("setNumber"))
    grader = _text(card_json.get("gradingCompany"))
    parts = [
        "" if set_name.split()[:1] == [year] else year,
        set_name,
        insert,
        player,
        "" if parallel == insert else parallel,
        f"#{set_number}" if set_number else "",
        _display_grader(grader) if grader else "",
        _grade(card_json.get("overall")),
    ]
    label = " ".join(part for part in parts if part)
    return label or _text(card_json.get("id"))


def _map_tier(
    pack_id: str, tier: Json, cards_json: Mapping[str, Json]
) -> list[ChecklistCard]:
    tier_name = tier["name"]

    def error(reason: str) -> ArenaMappingError:
        return ArenaMappingError(f"Slab Pack {pack_id!r}, tier {tier_name!r}: {reason}")

    raw_type = tier["type"]
    try:
        tier_type = TierType(raw_type)
    except ValueError as cause:
        raise error(f"unknown tier type {raw_type!r}") from cause
    try:
        hit_rate = percent_to_probability(str(tier["formattedHitRate"]))
    except ValueError as cause:
        raise error(str(cause)) from cause

    missing = [card_id for card_id in tier["itemIds"] if card_id not in cards_json]
    if missing:
        raise error(f"no card details for {', '.join(map(repr, missing))}")
    return [
        _map_card(card_id, cards_json[card_id], tier_name, tier_type, hit_rate)
        for card_id in tier["itemIds"]
    ]


def _map_card(
    card_id: str, card_json: Json, tier_name: str, tier_type: TierType, hit_rate: float
) -> ChecklistCard:
    return ChecklistCard(
        card_id=card_id,
        label=build_label({"id": card_id, **card_json}),
        tier_name=tier_name,
        tier_type=tier_type,
        hit_rate=hit_rate,
        grader=_text(card_json.get("gradingCompany")),
        grade=_grade(card_json.get("overall")),
        market_value=None,
        value_confidence=None,
    )


def _rate_or_default(pack_json: Json, key: str, default: float) -> float:
    value = pack_json.get(key)
    if value is None:
        return default
    is_number = isinstance(value, int | float) and not isinstance(value, bool)
    if not (is_number and 0 < value <= 1):
        pack_id = pack_json.get("id", "<no id>")
        raise ArenaMappingError(
            f"Slab Pack {pack_id!r}: {key} must be a number in (0, 1], got {value!r}"
        )
    return float(value)


def _text(value: object) -> str:
    """`value` as a string with whitespace collapsed; "" for None."""
    if value is None:
        return ""
    return " ".join(str(value).split())


def _not_base(value: object) -> str:
    """`value` as text, or "" when it is the default "Base" subset or parallel."""
    text = _text(value)
    return "" if text.casefold() == BASE_NAME.casefold() else text


def _without_prefix(text: str, prefix: str) -> str:
    """`text` minus a leading whole-word `prefix`, compared case-insensitively."""
    if not prefix:
        return text
    words, prefix_words = text.split(), prefix.split()
    head = words[: len(prefix_words)]
    if [w.casefold() for w in head] == [w.casefold() for w in prefix_words]:
        return " ".join(words[len(prefix_words) :])
    return text


def _grade(overall: object) -> str:
    """Arena Club's `overall` as text: 10 and 10.0 become "10", 9.5 "9.5"."""
    if isinstance(overall, float) and overall.is_integer():
        return str(int(overall))
    return _text(overall)


def _display_grader(grader: str) -> str:
    known = _GRADER_NAMES.get(grader.lower())
    if known is not None:
        return known
    return grader.replace("_", " ").title()
