import pytest

from arenaclub_optimizer.cost import CostModel, PaymentMethod


@pytest.mark.parametrize(
    ("price", "method", "slab_safe", "tax_rate", "expected"),
    [
        (100, PaymentMethod.wallet, False, 0.0, 100.00),
        (100, PaymentMethod.ach, False, 0.0, 100.80),
        (1000, PaymentMethod.ach, False, 0.0, 1005.00),  # 8.00 fee capped at 5.00
        (100, PaymentMethod.card, False, 0.0, 103.20),
        (100, PaymentMethod.wallet, True, 0.0, 110.00),
        # base 100 + tax 8 = 108; fee 108*0.029 + 0.30 = 3.432 -> 111.432
        (100, PaymentMethod.card, False, 0.08, 111.43),
    ],
)
def test_cost(price, method, slab_safe, tax_rate, expected):
    model = CostModel(payment_method=method, tax_rate=tax_rate)
    assert model.cost(price, slab_safe=slab_safe) == expected


def test_defaults_are_wallet_and_no_tax():
    model = CostModel()
    assert model.payment_method is PaymentMethod.wallet
    assert model.tax_rate == 0.0
    assert model.cost(100) == 100.00


def test_payment_method_values():
    assert {m.value for m in PaymentMethod} == {"wallet", "ach", "card"}


@pytest.mark.parametrize("price", [0, -1])
def test_non_positive_price_rejected(price):
    with pytest.raises(ValueError):
        CostModel().cost(price)


@pytest.mark.parametrize("tax_rate", [-0.01, 1.0, 1.5])
def test_tax_rate_out_of_range_rejected(tax_rate):
    with pytest.raises(ValueError):
        CostModel(tax_rate=tax_rate)


@pytest.mark.parametrize(
    ("price", "method", "slab_safe", "tax_rate", "expected"),
    [
        # 110 + 8.80 = 118.80; fee 3.4452 + 0.30 -> 122.5452
        (100, PaymentMethod.card, True, 0.08, 122.55),
        (625.00, PaymentMethod.ach, False, 0.0, 630.00),  # fee exactly 5.00
        (625.01, PaymentMethod.ach, False, 0.0, 630.01),  # 5.00008 capped to 5.00
        # Slab Safe base 625.009 crosses the cap: 625.009 + 5.00 -> 630.009
        (568.19, PaymentMethod.ach, True, 0.0, 630.01),
    ],
)
def test_cost_edge_cases(price, method, slab_safe, tax_rate, expected):
    model = CostModel(payment_method=method, tax_rate=tax_rate)
    assert model.cost(price, slab_safe=slab_safe) == expected


@pytest.mark.parametrize("price", [float("inf"), float("nan")])
def test_non_finite_price_rejected(price):
    with pytest.raises(ValueError):
        CostModel().cost(price)


def test_payment_method_string_is_coerced():
    assert CostModel(payment_method="card").payment_method is PaymentMethod.card


def test_unknown_payment_method_rejected():
    with pytest.raises(ValueError):
        CostModel(payment_method="bitcoin")
