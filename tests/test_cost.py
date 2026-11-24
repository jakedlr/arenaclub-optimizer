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
