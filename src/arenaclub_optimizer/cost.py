"""Cost of one Rip: list price plus Slab Safe, tax and payment-method fee. Pure."""

from dataclasses import dataclass
from enum import StrEnum

SLAB_SAFE_RATE = 0.10
ACH_RATE = 0.008
ACH_CAP = 5.00
CARD_RATE = 0.029
CARD_FIXED = 0.30


class PaymentMethod(StrEnum):
    wallet = "wallet"
    ach = "ach"
    card = "card"


@dataclass(frozen=True)
class CostModel:
    payment_method: PaymentMethod = PaymentMethod.wallet
    tax_rate: float = 0.0

    def __post_init__(self) -> None:
        if not 0.0 <= self.tax_rate < 1.0:
            raise ValueError(f"tax_rate must be in [0, 1), got {self.tax_rate}")

    def cost(self, price: float, slab_safe: bool = False) -> float:
        """Return the Cost of one Rip at list `price`, rounded to cents."""
        if not price > 0:
            raise ValueError(f"price must be > 0, got {price}")
        base = price * (1 + SLAB_SAFE_RATE) if slab_safe else price
        subtotal = base + base * self.tax_rate
        return round(subtotal + self._fee(subtotal), 2)

    def _fee(self, amount: float) -> float:
        match self.payment_method:
            case PaymentMethod.wallet:
                return 0.0
            case PaymentMethod.ach:
                return min(amount * ACH_RATE, ACH_CAP)
            case PaymentMethod.card:
                return amount * CARD_RATE + CARD_FIXED
