import dataclasses
from datetime import UTC, datetime

import pytest

from arenaclub_optimizer.arena_mapping import PackRates
from arenaclub_optimizer.config import Config, Override, Overrides
from arenaclub_optimizer.cost import CostModel, PaymentMethod
from arenaclub_optimizer.ev import evaluate_ev
from arenaclub_optimizer.model import (
    ChecklistCard,
    NormalizationStatus,
    SlabPack,
    TierType,
    ValueConfidence,
)
from arenaclub_optimizer.ranking import (
    RankedPack,
    RankingReport,
    SkippedPack,
    UnrankablePack,
    category_key,
    pack_name,
    rank,
)
from arenaclub_optimizer.risk import evaluate_risk

RATES = PackRates(buyback_rate=0.9, slab_safe_floor=0.8)
NO_OVERRIDES = Overrides()
WHEN = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


def card(
    card_id: str,
    hit_rate: float = 1.0,
    value: float | None = 0.0,
    *,
    tier: str = "Lineup",
    tier_type: TierType = TierType.LINE_UP,
    label: str | None = None,
    cert: str | None = None,
) -> ChecklistCard:
    return ChecklistCard(
        card_id=card_id,
        label=label if label is not None else f"label {card_id}",
        tier_name=tier,
        tier_type=tier_type,
        hit_rate=hit_rate,
        grader="psa",
        grade="10",
        market_value=value,
        value_confidence=None if value is None else ValueConfidence.HIGH,
        cert_number=cert,
    )


def pack(
    pack_id: str,
    cards: tuple[ChecklistCard, ...] | list[ChecklistCard],
    *,
    price: float = 100.0,
    category: str = "Pokemon",
    level: str = "Gold",
    active: bool = True,
    insurance: bool = True,
) -> SlabPack:
    return SlabPack(
        pack_id=pack_id,
        price_level=level,
        category=category,
        price=price,
        active=active,
        checklist=tuple(cards),
        insurance_available=insurance,
    )


def flat_pack(pack_id: str, value: float, **kwargs) -> SlabPack:
    """A one-card pack whose only Pull is worth `value`: EV % = 0.9 value/price - 1."""
    return pack(pack_id, [card(f"card_{pack_id}", 1.0, value)], **kwargs)


def risky_pack(pack_id: str, **kwargs) -> SlabPack:
    """A lottery pack: P(profit) 0.5% and Top-heaviness near 100%."""
    return pack(
        pack_id,
        [
            card(f"card_{pack_id}_hit", 0.005, 100_000.0, tier="Grail"),
            card(f"card_{pack_id}_miss", 0.995, 0.0),
        ],
        **kwargs,
    )


def unvalued_pack(pack_id: str, **kwargs) -> SlabPack:
    """Half the Hit Rate probability has no Market Value: Value Coverage 50%."""
    return pack(
        pack_id,
        [card(f"card_{pack_id}_a", 0.5, 200.0), card(f"card_{pack_id}_b", 0.5, None)],
        **kwargs,
    )


def with_filters(config: Config | None = None, **filters) -> Config:
    config = config or Config()
    return dataclasses.replace(
        config, filters=dataclasses.replace(config.filters, **filters)
    )


def run(packs, config: Config | None = None, overrides=NO_OVERRIDES, **kwargs):
    pairs = [p if isinstance(p, tuple) else (p, RATES) for p in packs]
    return rank(pairs, config or Config(), overrides, generated_at=WHEN, **kwargs)


def ids(report: RankingReport) -> list[str]:
    return [r.pack.pack_id for r in report.results]


# --- ordering and top N -------------------------------------------------------


def test_results_are_sorted_by_buyback_ev_pct_descending():
    report = run([flat_pack("low", 100), flat_pack("high", 150), flat_pack("mid", 120)])

    assert ids(report) == ["high", "mid", "low"]
    pcts = [r.ev.buyback_ev_pct for r in report.results]
    assert pcts == sorted(pcts, reverse=True)


def test_keeps_only_the_top_n():
    packs = [flat_pack(f"p{i}", 100 + i) for i in range(5)]

    report = run(packs, with_filters(top=2))

    assert ids(report) == ["p4", "p3"]


def test_default_top_is_ten():
    packs = [flat_pack(f"p{i:02}", 100 + i) for i in range(12)]

    assert len(run(packs).results) == 10


def test_ties_are_ordered_deterministically():
    packs = [flat_pack("b", 120), flat_pack("a", 120), flat_pack("c", 120)]

    first = ids(run(packs))
    second = ids(run(list(reversed(packs))))

    assert first == second


# --- EV and risk inputs -------------------------------------------------------


def test_uses_each_packs_own_buyback_rate_and_slab_safe_floor():
    generous = PackRates(buyback_rate=1.0, slab_safe_floor=0.95)
    stingy = PackRates(buyback_rate=0.5, slab_safe_floor=0.1)
    a, b = flat_pack("a", 120), flat_pack("b", 120)

    report = run([(a, stingy), (b, generous)])

    by_id = {r.pack.pack_id: r for r in report.results}
    model = CostModel()
    assert by_id["a"].ev == evaluate_ev(a, model, buyback_rate=0.5, slab_safe_floor=0.1)
    assert by_id["b"].ev == evaluate_ev(
        b, model, buyback_rate=1.0, slab_safe_floor=0.95
    )
    assert by_id["b"].risk == evaluate_risk(b, model, buyback_rate=1.0)
    assert ids(report) == ["b", "a"]


def test_uses_k_cost_and_thresholds_from_config():
    config = Config(
        k=1.3,
        payment_method=PaymentMethod.card,
        tax_rate=0.07,
        coverage_threshold=0.4,
        p_profit_threshold=0.6,
        top_heaviness_threshold=0.2,
    )
    p = unvalued_pack("u")

    report = run([p], config)

    [result] = report.results
    model = CostModel(PaymentMethod.card, 0.07)
    assert result.ev == evaluate_ev(
        p, model, k=1.3, buyback_rate=0.9, slab_safe_floor=0.8, coverage_threshold=0.4
    )
    assert result.risk == evaluate_risk(
        p,
        model,
        k=1.3,
        buyback_rate=0.9,
        p_profit_threshold=0.6,
        top_heaviness_threshold=0.2,
    )


def test_p_at_least_multiples_come_from_the_risk_result():
    report = run([risky_pack("r")])

    [result] = report.results
    assert result.p_1x == result.risk.p_at_least(1.0)
    assert result.p_2x == result.risk.p_at_least(2.0)
    assert result.p_10x == result.risk.p_at_least(10.0)
    assert result.p_10x == pytest.approx(0.005)


# --- Manual Overrides ---------------------------------------------------------


def test_manual_override_by_card_id_sets_the_market_value():
    p = pack("p", [card("card_x", 1.0, None)])
    overrides = Overrides(by_card_id={"card_x": Override(market_value=150.0)})

    report = run([p], overrides=overrides)

    [result] = report.results
    [valued] = result.pack.checklist
    assert valued.market_value == 150.0
    assert valued.value_confidence is ValueConfidence.HIGH
    assert result.ev.buyback_ev == pytest.approx(0.9 * 150 - 100)


def test_manual_override_by_cert_number_wins_over_card_id():
    p = pack("p", [card("card_x", 1.0, None, cert="12345678")])
    overrides = Overrides(
        by_cert={"12345678": Override(market_value=300.0)},
        by_card_id={"card_x": Override(market_value=10.0)},
    )

    [result] = run([p], overrides=overrides).results

    assert result.pack.checklist[0].market_value == 300.0


def test_manual_override_replaces_an_existing_market_value():
    p = pack("p", [card("card_x", 1.0, 50.0)])
    overrides = Overrides(by_card_id={"card_x": Override(market_value=500.0)})

    [result] = run([p], overrides=overrides).results

    assert result.pack.checklist[0].market_value == 500.0


def test_manual_overrides_are_keyed_on_card_id_not_label():
    twins = [
        card("card_1", 0.5, None, label="2021 Umbreon #215 PSA 10"),
        card("card_2", 0.5, None, label="2021 Umbreon #215 PSA 10"),
    ]
    overrides = Overrides(by_card_id={"card_1": Override(market_value=200.0)})

    report = run([pack("p", twins)], overrides=overrides)

    [entry] = report.unrankable
    assert entry.value_coverage == pytest.approx(0.5)


def test_manual_overrides_can_make_a_pack_rankable():
    p = unvalued_pack("u")
    overrides = Overrides(by_card_id={"card_u_b": Override(market_value=0.0)})

    assert run([p]).results == ()
    assert ids(run([p], overrides=overrides)) == ["u"]


# --- exclusions -----------------------------------------------------------------


def test_inactive_packs_are_excluded_entirely():
    report = run([flat_pack("sold-out", 500, active=False), flat_pack("live", 120)])

    assert ids(report) == ["live"]
    assert report.unrankable == ()
    assert report.skipped == ()


def test_inactive_pack_with_an_empty_checklist_is_excluded_without_error():
    report = run([pack("sold-out", [], active=False)])

    assert report == RankingReport((), (), (), WHEN)


def test_buyback_disabled_packs_are_skipped_with_a_reason():
    disabled = PackRates(buyback_rate=None, slab_safe_floor=0.8)

    report = run([(flat_pack("off", 500), disabled), flat_pack("on", 120)])

    assert ids(report) == ["on"]
    [skipped] = report.skipped
    assert skipped.pack_id == "off"
    assert skipped.name == "Gold Pokemon"
    assert "buyback" in skipped.reason.lower()


def test_rejected_hit_rates_are_skipped_and_ranking_continues():
    broken = pack("broken", [card("card_a", 0.3, 100.0), card("card_b", 0.2, 100.0)])

    report = run([broken, flat_pack("ok", 120)])

    assert ids(report) == ["ok"]
    [skipped] = report.skipped
    assert skipped.pack_id == "broken"
    assert "hit rate" in skipped.reason.lower()


def test_an_active_pack_with_no_checklist_is_skipped():
    report = run([pack("empty", []), flat_pack("ok", 120)])

    assert ids(report) == ["ok"]
    [skipped] = report.skipped
    assert skipped.pack_id == "empty"
    assert skipped.reason


def test_packs_below_value_coverage_are_unrankable_with_their_coverage():
    report = run([unvalued_pack("u"), flat_pack("ok", 120)])

    assert ids(report) == ["ok"]
    [entry] = report.unrankable
    assert isinstance(entry, UnrankablePack)
    assert entry.pack.pack_id == "u"
    assert entry.value_coverage == pytest.approx(0.5)


def test_prior_skips_are_carried_into_the_report_first():
    earlier = SkippedPack("p0", "Gold Football", "could not fetch")
    broken = pack("broken", [card("card_a", 0.3, 100.0)])

    report = run([broken], skipped=[earlier])

    assert report.skipped[0] == earlier
    assert [s.pack_id for s in report.skipped] == ["p0", "broken"]


# --- filters ------------------------------------------------------------------


def test_category_filter_is_case_insensitive():
    packs = [
        flat_pack("pk", 120, category="Pokemon"),
        flat_pack("fb", 130, category="Football"),
        flat_pack("op", 140, category="One Piece"),
    ]

    report = run(packs, with_filters(categories=("POKEMON", "one piece")))

    assert ids(report) == ["op", "pk"]


def test_category_filter_ignores_accents():
    report = run(
        [flat_pack("pk", 120, category="Pokemon")],
        with_filters(categories=("Pokémon",)),
    )

    assert ids(report) == ["pk"]


def test_category_key_folds_case_accents_and_spacing():
    assert category_key("  Pokémon ") == category_key("POKEMON")
    assert category_key("One  Piece") == category_key("one piece")
    assert category_key("Football") != category_key("Pokemon")


def test_price_filter_is_inclusive():
    packs = [flat_pack(f"p{p}", p * 1.5, price=p) for p in (25, 50, 100, 150, 250)]

    report = run(packs, with_filters(price_min=50, price_max=150))

    assert sorted(ids(report)) == ["p100", "p150", "p50"]


def test_min_ev_pct_filter():
    packs = [flat_pack("a", 110), flat_pack("b", 120), flat_pack("c", 100)]

    # EV %: a = -1%, b = +8%, c = -10%
    report = run(packs, with_filters(min_ev_pct=-0.05))

    assert ids(report) == ["b", "a"]


def test_min_p_profit_filter():
    report = run(
        [risky_pack("risky"), flat_pack("safe", 120)], with_filters(min_p_profit=0.5)
    )

    assert ids(report) == ["safe"]


def test_exclude_high_variance_filter():
    packs = [risky_pack("risky"), flat_pack("safe", 120)]

    assert sorted(ids(run(packs))) == ["risky", "safe"]
    assert ids(run(packs, with_filters(exclude_high_variance=True))) == ["safe"]


def test_category_and_price_filters_apply_before_skips_and_coverage():
    disabled = PackRates(buyback_rate=None, slab_safe_floor=0.8)
    packs = [
        (flat_pack("off", 120, category="Football"), disabled),
        unvalued_pack("u", category="Football"),
        unvalued_pack("pricey", price=1000),
        flat_pack("ok", 120),
    ]

    report = run(packs, with_filters(categories=("pokemon",), price_max=500))

    assert ids(report) == ["ok"]
    assert report.unrankable == ()
    assert report.skipped == ()


def test_metric_filters_do_not_affect_unrankable_packs():
    report = run([unvalued_pack("u")], with_filters(min_ev_pct=10.0))

    assert [u.pack.pack_id for u in report.unrankable] == ["u"]


def test_top_n_applies_after_the_other_filters():
    packs = [risky_pack("risky"), flat_pack("a", 120), flat_pack("b", 110)]

    report = run(packs, with_filters(exclude_high_variance=True, top=1))

    assert ids(report) == ["a"]


# --- notes --------------------------------------------------------------------


def test_high_variance_note_is_included():
    [result] = run([risky_pack("r")]).results

    assert result.risk.note is not None
    assert result.risk.note in result.notes


def test_no_notes_for_a_plain_fully_valued_pack():
    [result] = run([flat_pack("ok", 120)]).results

    assert result.notes == ()


def test_lower_bound_note_names_the_unvalued_top_tiers():
    p = pack(
        "p",
        [
            card("card_g", 0.01, None, tier="Grail", tier_type=TierType.GRAIL),
            card("card_c", 0.02, None, tier="Chase", tier_type=TierType.CHASE),
            card("card_l", 0.97, 150.0),
        ],
    )

    [result] = run([p]).results

    [note] = [n for n in result.notes if "lower bound" in n.lower()]
    assert "Grail" in note and "Chase" in note


def test_no_lower_bound_note_when_only_lineup_cards_lack_values():
    p = pack("p", [card("card_l1", 0.97, 150.0), card("card_l2", 0.03, None)])

    [result] = run([p]).results

    assert result.ev.lower_bound is True
    assert not any("lower bound" in n.lower() for n in result.notes)


def test_hit_rate_warning_becomes_a_note():
    p = pack("p", [card("card_a", 0.505, 150.0), card("card_b", 0.505, 150.0)])

    [result] = run([p]).results

    assert result.ev.normalization_status is NormalizationStatus.WARN
    assert any("hit rate" in n.lower() for n in result.notes)


# --- the report -----------------------------------------------------------------


def test_report_carries_generated_at():
    assert run([]).generated_at == WHEN


def test_generated_at_defaults_to_now_in_utc():
    before = datetime.now(UTC)
    report = rank([], Config(), NO_OVERRIDES)
    after = datetime.now(UTC)

    assert report.generated_at.tzinfo is not None
    assert before <= report.generated_at <= after


def test_report_holds_tuples_of_typed_entries():
    report = run([flat_pack("ok", 120), unvalued_pack("u")])

    assert isinstance(report.results, tuple)
    assert isinstance(report.results[0], RankedPack)
    assert isinstance(report.unrankable, tuple)
    assert isinstance(report.skipped, tuple)


def test_rank_accepts_any_iterable_of_pairs():
    pairs = ((p, RATES) for p in [flat_pack("a", 120), flat_pack("b", 130)])

    report = rank(pairs, Config(), NO_OVERRIDES, generated_at=WHEN)

    assert ids(report) == ["b", "a"]


def test_pack_name_is_price_level_then_category():
    assert pack_name(flat_pack("p", 1, level="Gold", category="Pokemon")) == (
        "Gold Pokemon"
    )
