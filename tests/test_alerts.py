import json
import logging
import os
from datetime import UTC, datetime, timedelta, timezone

import pytest
from fake_values import NO_VALUES

from arenaclub_optimizer.alerts import (
    DEFAULT_EV_STEP_PCT,
    DEFAULT_STALE_AFTER,
    MAX_MESSAGE_BYTES,
    MAX_TITLE_BYTES,
    STATE_FILENAME,
    Alert,
    AlertedPack,
    AlertKind,
    AlertState,
    HealthKind,
    HealthOutcome,
    Priority,
    detect,
    detect_health,
    load_state,
    save_state,
)
from arenaclub_optimizer.config import state_dir
from arenaclub_optimizer.ev import EVResult
from arenaclub_optimizer.fetch import FetchResult
from arenaclub_optimizer.model import NormalizationStatus, SkippedPack, SlabPack
from arenaclub_optimizer.ranking import RankedPack, RankingReport
from arenaclub_optimizer.risk import RiskResult

T0 = datetime(2026, 11, 26, 10, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=10)
T2 = T1 + timedelta(minutes=10)
T3 = T2 + timedelta(minutes=10)

HV_NOTE = (
    "Only 12% of pulls return at least your cost; "
    "a typical pull returns 0.40× your cost."
)


def ranked(
    pack_id: str = "gp",
    ev_pct: float = 0.042,
    *,
    price_level: str = "Gold",
    category: str = "Pokémon",
    price: float = 100.0,
    p_profit: float = 0.31,
    note: str | None = None,
    notes: tuple[str, ...] = (),
) -> RankedPack:
    """A Top Result with hand-set figures: Cost is the price, so Buyback EV $
    is `ev_pct` x price."""
    pack = SlabPack(pack_id, price_level, category, price, True, ())
    ev = EVResult(
        cost=price,
        market_ev=ev_pct * price,
        market_ev_pct=ev_pct,
        buyback_ev=ev_pct * price,
        buyback_ev_pct=ev_pct,
        slab_safe_ev=None,
        value_coverage=1.0,
        rankable=True,
        lower_bound=False,
        missing_top_tiers=(),
        normalization_status=NormalizationStatus.OK,
    )
    risk = RiskResult(
        p_profit=p_profit,
        std_ratio=1.0,
        median_ratio=0.4,
        top_heaviness=0.1,
        high_variance=note is not None,
        note=note,
        cost=price,
        outcomes=(),
    )
    all_notes = ((note,) if note else ()) + notes
    return RankedPack(pack, ev, risk, p_profit, 0.1, 0.0, all_notes)


def report(*results: RankedPack, skipped: tuple[SkippedPack, ...] = ()):
    ordered = sorted(results, key=lambda r: -r.ev.buyback_ev_pct)
    return RankingReport(tuple(ordered), (), skipped, T0, NO_VALUES)


def run(*reports: RankingReport, step: float = DEFAULT_EV_STEP_PCT):
    """Feed each report to `detect` in turn, from empty state. Returns the
    Alerts of each cycle and the final state."""
    state = AlertState()
    cycles = []
    for i, each in enumerate(reports):
        alerts, state = detect(
            state, each, now=T0 + timedelta(minutes=10 * i), ev_step_pct=step
        )
        cycles.append(alerts)
    return cycles, state


# --- detect: Entered ---------------------------------------------------------


def test_a_new_top_result_is_entered():
    alerts, state = detect(AlertState(), report(ranked("gp", 0.042)), now=T0)

    [alert] = alerts
    assert alert.kind is AlertKind.ENTERED
    assert alert.result is not None
    assert alert.result.pack.pack_id == "gp"
    assert state.packs == {"gp": AlertedPack(buyback_ev_pct=0.042, alerted_at=T0)}


def test_an_entered_alert_carries_the_figures_in_its_text():
    [alert], _ = detect(
        AlertState(), report(ranked("gp", 0.042, p_profit=0.31)), now=T0
    )

    assert alert.message.splitlines()[0] == (
        "Gold Pokémon: Buyback EV +4.2% ($4.20), P(profit) 31%"
    )
    assert "Gold Pokémon" in alert.title
    assert alert.priority is Priority.DEFAULT
    assert alert.priority == "default"


def test_an_entered_alert_with_negative_ev_shows_the_sign_on_both_figures():
    [alert], _ = detect(AlertState(), report(ranked("gp", -0.03)), now=T0)

    assert "Buyback EV -3.0% (-$3.00)" in alert.message


def test_an_entered_alert_carries_the_high_variance_note():
    result = ranked("gp", 0.042, p_profit=0.12, note=HV_NOTE)

    [alert], _ = detect(AlertState(), report(result), now=T0)

    assert alert.result is not None
    assert alert.result.risk.note == HV_NOTE
    assert f"High-variance Note: {HV_NOTE}" in alert.message
    assert "high-variance" in alert.tags


def test_an_alert_without_a_note_has_no_note_line():
    [alert], _ = detect(AlertState(), report(ranked("gp")), now=T0)

    assert "High-variance" not in alert.message
    assert "high-variance" not in alert.tags


def test_other_notes_are_included_after_the_figures():
    lower = "Lower bound: some Chase cards have no Market Value."
    [alert], _ = detect(AlertState(), report(ranked("gp", notes=(lower,))), now=T0)

    assert lower in alert.message.splitlines()


def test_the_price_is_in_the_message():
    [alert], _ = detect(AlertState(), report(ranked("gp", price=50.0)), now=T0)

    assert "$50.00" in alert.message


def test_a_pack_still_in_the_results_is_not_alerted_again():
    cycles, state = run(report(ranked("gp", 0.042)), report(ranked("gp", 0.042)))

    assert [len(c) for c in cycles] == [1, 0]
    # The last alerted value and time stay those of the first Alert; only
    # last_seen_at moves.
    assert state.packs["gp"] == AlertedPack(0.042, T0, last_seen_at=T1)


def test_an_empty_report_sends_nothing():
    alerts, state = detect(AlertState(), report(), now=T0)

    assert alerts == []
    assert state.packs == {}


# --- detect: Improved ---------------------------------------------------------


def test_a_rise_of_exactly_the_step_is_improved():
    # 0.10 -> 0.15 is 0.04999999999999999 in floats: still counts as the step.
    cycles, state = run(report(ranked("gp", 0.10)), report(ranked("gp", 0.15)))

    [alert] = cycles[1]
    assert alert.kind is AlertKind.IMPROVED
    assert alert.priority is Priority.DEFAULT
    assert state.packs["gp"] == AlertedPack(0.15, T1)


def test_an_improved_alert_says_where_it_rose_from():
    cycles, _ = run(report(ranked("gp", 0.042)), report(ranked("gp", 0.092)))

    [alert] = cycles[1]
    lines = alert.message.splitlines()
    assert lines[0] == "Gold Pokémon: Buyback EV +9.2% ($9.20), P(profit) 31%"
    assert "up from +4.2%" in alert.message
    assert "Gold Pokémon" in alert.title


def test_a_rise_below_the_step_is_not_improved():
    cycles, state = run(report(ranked("gp", 0.10)), report(ranked("gp", 0.149)))

    assert cycles[1] == []
    assert state.packs["gp"] == AlertedPack(0.10, T0, last_seen_at=T1)


def test_small_rises_add_up_against_the_last_alerted_value():
    cycles, state = run(
        report(ranked("gp", 0.10)),
        report(ranked("gp", 0.13)),
        report(ranked("gp", 0.16)),
    )

    assert [len(c) for c in cycles] == [1, 0, 1]
    assert cycles[2][0].kind is AlertKind.IMPROVED
    assert state.packs["gp"] == AlertedPack(0.16, T2)


def test_the_step_is_configurable():
    cycles, _ = run(report(ranked("gp", 0.10)), report(ranked("gp", 0.12)), step=0.02)

    assert [a.kind for a in cycles[1]] == [AlertKind.IMPROVED]


def test_a_dip_then_recovery_to_the_same_level_does_not_re_alert():
    cycles, state = run(
        report(ranked("gp", 0.10)),
        report(ranked("gp", 0.02)),
        report(ranked("gp", 0.10)),
        report(ranked("gp", 0.14)),
    )

    assert [len(c) for c in cycles] == [1, 0, 0, 0]
    assert state.packs["gp"] == AlertedPack(0.10, T0, last_seen_at=T3)


def test_a_dip_then_rise_past_the_last_alerted_value_plus_step_alerts():
    cycles, _ = run(
        report(ranked("gp", 0.10)),
        report(ranked("gp", 0.02)),
        report(ranked("gp", 0.15)),
    )

    assert [a.kind for a in cycles[2]] == [AlertKind.IMPROVED]


@pytest.mark.parametrize("step", [0.0, -0.01, float("nan"), float("inf")])
def test_a_step_that_is_not_positive_and_finite_is_rejected(step):
    with pytest.raises(ValueError, match="ev_step_pct"):
        detect(AlertState(), report(), now=T0, ev_step_pct=step)


# --- detect: Left ---------------------------------------------------------------


def test_a_pack_that_left_is_dropped_silently():
    cycles, state = run(report(ranked("gp", 0.10)), report())

    assert cycles[1] == []
    assert state.packs == {}


def test_a_pack_that_left_and_came_back_is_entered_again():
    cycles, state = run(
        report(ranked("gp", 0.10)), report(), report(ranked("gp", 0.10))
    )

    assert [len(c) for c in cycles] == [1, 0, 1]
    assert cycles[2][0].kind is AlertKind.ENTERED
    assert state.packs["gp"] == AlertedPack(0.10, T2)


def test_a_pack_skipped_this_cycle_keeps_its_entry():
    # A skipped pack couldn't be evaluated (e.g. a network error), so it is
    # unknown rather than gone: it must not be re-alerted when it returns.
    skipped = (SkippedPack("gp", "Gold Pokémon", "timed out"),)
    cycles, state = run(
        report(ranked("gp", 0.10)),
        report(skipped=skipped),
        report(ranked("gp", 0.10)),
    )

    assert [len(c) for c in cycles] == [1, 0, 0]
    assert state.packs["gp"] == AlertedPack(0.10, T0, last_seen_at=T2)


def test_an_alerted_pack_defaults_last_seen_at_to_alerted_at():
    assert AlertedPack(0.1, T0).last_seen_at == T0


def skipped_report(pack_id: str = "gp") -> RankingReport:
    return report(skipped=(SkippedPack(pack_id, "Gold Pokémon", "Hit Rates rejected"),))


def test_a_skipped_pack_does_not_refresh_last_seen_at():
    state = AlertState(packs={"gp": AlertedPack(0.10, T0)})

    _, state = detect(state, skipped_report(), now=T0 + timedelta(hours=1))

    assert state.packs["gp"] == AlertedPack(0.10, T0, last_seen_at=T0)


def test_a_skipped_pack_is_kept_up_to_stale_after():
    state = AlertState(packs={"gp": AlertedPack(0.10, T0)})

    _, state = detect(state, skipped_report(), now=T0 + DEFAULT_STALE_AFTER)

    assert "gp" in state.packs


def test_a_pack_skipped_for_longer_than_stale_after_is_dropped():
    state = AlertState(packs={"gp": AlertedPack(0.10, T0)})
    later = T0 + DEFAULT_STALE_AFTER + timedelta(minutes=1)

    alerts, state = detect(state, skipped_report(), now=later)

    assert alerts == []
    assert state.packs == {}


def test_a_long_skipped_pack_that_returns_is_entered_again():
    # E.g. its Hit Rates were rejected for days, then fixed. Its return at
    # 0.12 is below the old 0.10 + step, but it counts as Entered.
    state = AlertState(packs={"gp": AlertedPack(0.10, T0)})
    hour = timedelta(hours=1)
    for n in range(1, 30):
        _, state = detect(state, skipped_report(), now=T0 + n * hour)

    alerts, state = detect(state, report(ranked("gp", 0.12)), now=T0 + 30 * hour)

    assert [a.kind for a in alerts] == [AlertKind.ENTERED]


def test_a_pack_seen_in_the_results_restarts_the_stale_clock():
    later = T0 + timedelta(hours=20)
    state = AlertState(packs={"gp": AlertedPack(0.10, T0)})
    _, state = detect(state, report(ranked("gp", 0.10)), now=later)

    _, state = detect(state, skipped_report(), now=later + timedelta(hours=20))

    assert state.packs["gp"] == AlertedPack(0.10, T0, last_seen_at=later)


def test_stale_after_is_configurable():
    state = AlertState(packs={"gp": AlertedPack(0.10, T0)})

    _, state = detect(
        state,
        skipped_report(),
        now=T0 + timedelta(hours=2),
        stale_after=timedelta(hours=1),
    )

    assert state.packs == {}


@pytest.mark.parametrize("stale_after", [timedelta(0), timedelta(hours=-1)])
def test_a_stale_after_that_is_not_positive_is_rejected(stale_after):
    with pytest.raises(ValueError, match="stale_after"):
        detect(AlertState(), report(), now=T0, stale_after=stale_after)


def test_detect_rejects_a_naive_now():
    with pytest.raises(ValueError, match="now"):
        detect(AlertState(), report(), now=datetime(2026, 11, 26, 10, 0))


def test_an_aware_now_in_another_zone_is_accepted():
    zone = timezone(timedelta(hours=-8))
    alerts, _ = detect(AlertState(), report(ranked("gp")), now=T0.astimezone(zone))

    assert len(alerts) == 1


def test_detect_keeps_the_health_status():
    previous = AlertState(health=HealthKind.BLOCKED)

    _, state = detect(previous, report(ranked("gp")), now=T0)

    assert state.health is HealthKind.BLOCKED


def test_detect_does_not_change_the_previous_state():
    previous = AlertState()

    detect(previous, report(ranked("gp")), now=T0)

    assert previous == AlertState()


# --- detect: ordering ------------------------------------------------------------


def test_alerts_are_sorted_by_buyback_ev_pct_descending():
    first, _ = detect(
        AlertState(), report(ranked("a", 0.10), ranked("b", 0.05)), now=T0
    )
    _, state = detect(AlertState(), report(ranked("a", 0.10)), now=T0)
    # Next cycle: "a" improves to 0.20 and "b" (new) enters at 0.30, "c" at 0.01.
    second, _ = detect(
        state,
        report(ranked("a", 0.20), ranked("b", 0.30), ranked("c", 0.01)),
        now=T1,
    )

    assert [a.result.pack.pack_id for a in first] == ["a", "b"]
    assert [(a.result.pack.pack_id, a.kind) for a in second] == [
        ("b", AlertKind.ENTERED),
        ("a", AlertKind.IMPROVED),
        ("c", AlertKind.ENTERED),
    ]


# --- detect_health ----------------------------------------------------------------

FAILURES = [
    HealthOutcome.blocked(429, 120.0),
    HealthOutcome.api_error("Arena Club returned HTTP 502"),
    HealthOutcome.degraded(skipped=7, total=10),
]


def test_ok_while_ok_is_silent():
    alert, state = detect_health(AlertState(), HealthOutcome.ok())

    assert alert is None
    assert state.health is HealthKind.OK


@pytest.mark.parametrize("outcome", FAILURES, ids=lambda o: o.kind.value)
def test_the_first_failure_sends_one_high_priority_alert(outcome):
    alert, state = detect_health(AlertState(), outcome)

    assert alert is not None
    assert alert.kind is AlertKind.HEALTH_FAILED
    assert alert.priority is Priority.HIGH
    assert alert.priority == "high"
    assert alert.result is None
    assert "warning" in alert.tags
    assert state.health is outcome.kind


@pytest.mark.parametrize("outcome", FAILURES, ids=lambda o: o.kind.value)
def test_a_repeated_failure_is_silent(outcome):
    _, state = detect_health(AlertState(), outcome)

    alert, state = detect_health(state, outcome)

    assert alert is None
    assert state.health is outcome.kind


def test_a_different_failure_while_failing_is_silent():
    _, state = detect_health(AlertState(), HealthOutcome.blocked(429, None))

    alert, state = detect_health(state, HealthOutcome.api_error("timed out"))

    assert alert is None
    assert state.health is HealthKind.API_ERROR


@pytest.mark.parametrize("outcome", FAILURES, ids=lambda o: o.kind.value)
def test_returning_to_ok_sends_one_recovery_alert(outcome):
    _, state = detect_health(AlertState(), outcome)

    alert, state = detect_health(state, HealthOutcome.ok())
    again, state = detect_health(state, HealthOutcome.ok())

    assert alert is not None
    assert alert.kind is AlertKind.HEALTH_RECOVERED
    assert alert.priority is Priority.DEFAULT
    assert again is None
    assert state.health is HealthKind.OK


def test_a_full_outage_cycle_sends_exactly_two_alerts():
    outcomes = [
        HealthOutcome.ok(),
        HealthOutcome.blocked(429, 60.0),
        HealthOutcome.blocked(429, 60.0),
        HealthOutcome.degraded(skipped=9, total=10),
        HealthOutcome.ok(),
        HealthOutcome.ok(),
    ]
    state = AlertState()
    sent = []
    for outcome in outcomes:
        alert, state = detect_health(state, outcome)
        if alert is not None:
            sent.append(alert.kind)

    assert sent == [AlertKind.HEALTH_FAILED, AlertKind.HEALTH_RECOVERED]


def test_detect_health_keeps_the_pack_entries():
    _, state = detect(AlertState(), report(ranked("gp")), now=T0)

    _, after = detect_health(state, HealthOutcome.blocked(429, None))

    assert after.packs == state.packs


def test_the_blocked_alert_gives_the_status_and_retry_after():
    alert, _ = detect_health(AlertState(), HealthOutcome.blocked(429, 120.0))

    assert alert is not None
    assert "HTTP 429" in alert.message
    assert "120s" in alert.message


def test_the_blocked_alert_without_retry_after():
    alert, _ = detect_health(AlertState(), HealthOutcome.blocked(403, None))

    assert alert is not None
    assert "HTTP 403" in alert.message
    assert "retry" not in alert.message.casefold()


def test_the_api_error_alert_gives_the_error():
    alert, _ = detect_health(
        AlertState(), HealthOutcome.api_error("Arena Club returned HTTP 502")
    )

    assert alert is not None
    assert "Arena Club returned HTTP 502" in alert.message


def test_the_degraded_alert_gives_the_counts():
    alert, _ = detect_health(AlertState(), HealthOutcome.degraded(7, 10))

    assert alert is not None
    assert "7 of 10 Slab Packs" in alert.message


def test_a_long_error_is_cut_to_fit_the_message_limit():
    alert, _ = detect_health(AlertState(), HealthOutcome.api_error("é" * 10_000))

    assert alert is not None
    size = len(alert.message.encode("utf-8"))
    assert size <= MAX_MESSAGE_BYTES < 4096
    first, *rest = alert.message.splitlines()
    assert first.endswith("…")
    assert rest == [
        "Watch Mode is backing off and will send one Alert when requests work again."
    ]


@pytest.mark.parametrize(
    ("skipped", "total", "kind"),
    [
        (0, 0, HealthKind.OK),
        (0, 10, HealthKind.OK),
        (5, 10, HealthKind.OK),
        (6, 10, HealthKind.DEGRADED),
        (1, 1, HealthKind.DEGRADED),
    ],
)
def test_from_fetch_is_degraded_when_more_than_half_were_skipped(skipped, total, kind):
    pack = SlabPack("p", "Gold", "Pokémon", 100.0, True, ())
    packs = tuple((pack, None) for _ in range(total - skipped))
    skips = tuple(SkippedPack(f"s{i}", "Gold Pokémon", "x") for i in range(skipped))

    outcome = HealthOutcome.from_fetch(FetchResult(packs, skips))

    assert outcome.kind is kind
    if kind is HealthKind.DEGRADED:
        assert (outcome.skipped, outcome.total) == (skipped, total)


# --- state storage -----------------------------------------------------------------


def sample_state() -> AlertState:
    return AlertState(
        packs={
            "gp": AlertedPack(0.042, T0, last_seen_at=T2),
            "gf": AlertedPack(-0.01, T1),
        },
        health=HealthKind.DEGRADED,
    )


def test_state_round_trips_through_the_file(tmp_path):
    save_state(sample_state(), tmp_path)

    assert load_state(tmp_path) == sample_state()


def test_state_is_stored_as_json_in_alerts_json(tmp_path):
    save_state(sample_state(), tmp_path)

    data = json.loads((tmp_path / STATE_FILENAME).read_text(encoding="utf-8"))
    assert STATE_FILENAME == "alerts.json"
    assert data["health"] == "degraded"
    assert data["packs"]["gp"] == {
        "buyback_ev_pct": 0.042,
        "alerted_at": T0.isoformat(),
        "last_seen_at": T2.isoformat(),
    }
    assert data["version"] == 1


def test_a_file_without_last_seen_at_defaults_it_to_alerted_at(tmp_path):
    (tmp_path / STATE_FILENAME).write_text(
        '{"version": 1, "health": "ok", "packs": {"gp": {"buyback_ev_pct": 0.1,'
        ' "alerted_at": "2026-11-26T10:00:00+00:00"}}}',
        encoding="utf-8",
    )

    state = load_state(tmp_path)

    assert state.packs["gp"] == AlertedPack(0.1, T0, last_seen_at=T0)


def test_the_default_directory_is_the_state_dir():
    save_state(sample_state())

    assert (state_dir() / STATE_FILENAME).is_file()
    assert load_state() == sample_state()


def test_saving_creates_the_directory(tmp_path):
    directory = tmp_path / "a" / "b"

    save_state(sample_state(), directory)

    assert load_state(directory) == sample_state()


def test_the_state_directory_is_private(tmp_path):
    directory = tmp_path / "state"

    save_state(sample_state(), directory)

    assert directory.stat().st_mode & 0o777 == 0o700


def test_a_missing_file_is_empty_state_without_a_warning(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        assert load_state(tmp_path) == AlertState()

    assert caplog.records == []


@pytest.mark.parametrize(
    "text",
    [
        "{not json",
        "[]",
        '{"version": 99, "health": "ok", "packs": {}}',
        '{"version": 1, "health": "sideways", "packs": {}}',
        '{"version": 1, "health": "ok", "packs": []}',
        '{"version": 1, "health": "ok", "packs": {"gp": {"buyback_ev_pct": "x",'
        ' "alerted_at": "2026-11-26T10:00:00+00:00"}}}',
        '{"version": 1, "health": "ok", "packs": {"gp": {"buyback_ev_pct": true,'
        ' "alerted_at": "2026-11-26T10:00:00+00:00"}}}',
        '{"version": 1, "health": "ok", "packs": {"gp": {"buyback_ev_pct": NaN,'
        ' "alerted_at": "2026-11-26T10:00:00+00:00"}}}',
        '{"version": 1, "health": "ok", "packs": {"gp": {"buyback_ev_pct": 0.1,'
        ' "alerted_at": "yesterday"}}}',
        '{"version": 1, "health": "ok", "packs": {"gp": {"buyback_ev_pct": 0.1}}}',
        '{"version": 1, "health": "ok", "packs": {"gp": {"buyback_ev_pct": 0.1,'
        ' "alerted_at": "2026-11-26T10:00:00"}}}',
        '{"version": 1, "health": "ok", "packs": {"gp": {"buyback_ev_pct": 0.1,'
        ' "alerted_at": "2026-11-26T10:00:00+00:00", "last_seen_at": 5}}}',
        '{"version": 1, "health": "ok", "packs": {"gp": {"buyback_ev_pct": 0.1,'
        ' "alerted_at": "2026-11-26T10:00:00+00:00",'
        ' "last_seen_at": "2026-11-26T10:00:00"}}}',
        "",
    ],
)
def test_a_corrupt_file_is_logged_and_treated_as_empty(tmp_path, caplog, text):
    (tmp_path / STATE_FILENAME).write_text(text, encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="arenaclub_optimizer.alerts"):
        assert load_state(tmp_path) == AlertState()

    assert any(STATE_FILENAME in r.getMessage() for r in caplog.records)


def aside(directory) -> list:
    return sorted(directory.glob(f"{STATE_FILENAME}.corrupt-*"))


@pytest.mark.parametrize(
    "text",
    ["{not json", '{"version": 99, "health": "ok", "packs": {}}'],
    ids=["corrupt", "unknown-version"],
)
def test_a_corrupt_file_is_renamed_aside(tmp_path, caplog, text):
    (tmp_path / STATE_FILENAME).write_text(text, encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="arenaclub_optimizer.alerts"):
        load_state(tmp_path)

    [kept] = aside(tmp_path)
    assert kept.read_text(encoding="utf-8") == text
    assert not (tmp_path / STATE_FILENAME).exists()
    # The name ends in a UTC timestamp, and the log says where the file went.
    stamp = kept.name.removeprefix(f"{STATE_FILENAME}.corrupt-")
    datetime.strptime(stamp, "%Y%m%dT%H%M%SZ")
    assert any(kept.name in r.getMessage() for r in caplog.records)


def test_a_renamed_aside_file_is_not_read_again(tmp_path):
    (tmp_path / STATE_FILENAME).write_text("{not json", encoding="utf-8")
    load_state(tmp_path)

    save_state(sample_state(), tmp_path)

    assert load_state(tmp_path) == sample_state()
    assert len(aside(tmp_path)) == 1


def test_a_missing_file_is_not_renamed(tmp_path):
    load_state(tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_a_file_that_is_not_utf8_is_treated_as_empty(tmp_path, caplog):
    (tmp_path / STATE_FILENAME).write_bytes(b"\xff\xfe\x00garbage")

    with caplog.at_level(logging.WARNING, logger="arenaclub_optimizer.alerts"):
        assert load_state(tmp_path) == AlertState()

    assert caplog.records


def test_saving_leaves_no_temp_files(tmp_path):
    save_state(sample_state(), tmp_path)
    save_state(AlertState(), tmp_path)

    assert [p.name for p in tmp_path.iterdir()] == [STATE_FILENAME]


def test_a_failed_write_keeps_the_previous_file(tmp_path, monkeypatch):
    save_state(sample_state(), tmp_path)
    before = (tmp_path / STATE_FILENAME).read_bytes()

    def fail(*args, **kwargs):
        raise OSError("disk full")

    # The new state is fully written to a temp file, then the swap fails.
    with monkeypatch.context() as m:
        m.setattr(os, "replace", fail)
        with pytest.raises(OSError, match="disk full"):
            save_state(AlertState(), tmp_path)

    assert (tmp_path / STATE_FILENAME).read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == [STATE_FILENAME]
    assert load_state(tmp_path) == sample_state()


# --- Alert --------------------------------------------------------------------------


def test_every_alert_is_plain_text_under_the_limit():
    long_name = "Pokémon " * 1000
    [alert], _ = detect(
        AlertState(),
        report(ranked("gp", category=long_name, note=HV_NOTE * 50)),
        now=T0,
    )

    assert isinstance(alert, Alert)
    assert len(alert.message.encode("utf-8")) <= MAX_MESSAGE_BYTES
    assert len(alert.title.encode("utf-8")) <= MAX_TITLE_BYTES
    assert alert.title.endswith("…")
    assert isinstance(alert.tags, tuple)


def test_the_title_cap_is_about_250_bytes():
    assert 200 <= MAX_TITLE_BYTES <= 256


def test_a_short_title_is_not_cut():
    [alert], _ = detect(AlertState(), report(ranked("gp")), now=T0)

    assert alert.title == "New Top Result: Gold Pokémon"


def test_an_improved_title_is_capped_too():
    long_name = "Pokémon " * 1000
    _, state = detect(
        AlertState(), report(ranked("gp", 0.1, category=long_name)), now=T0
    )

    [alert], _ = detect(state, report(ranked("gp", 0.2, category=long_name)), now=T1)

    assert alert.kind is AlertKind.IMPROVED
    assert len(alert.title.encode("utf-8")) <= MAX_TITLE_BYTES
