"""Fake value providers and card builders for the value pipeline tests."""

from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime

from arenaclub_optimizer.model import (
    CardIdentity,
    ChecklistCard,
    TierType,
    ValueConfidence,
)
from arenaclub_optimizer.values import (
    CreditBudget,
    NotFound,
    ProviderUnavailable,
    Unmatched,
    ValueQuote,
)

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
TODAY = date(2026, 11, 24)

type Answer = ValueQuote | NotFound | Unmatched | ProviderUnavailable


def identity(card_name: str = "Umbreon VMAX", **overrides) -> CardIdentity:
    fields = {
        "year": 2021,
        "set_name": "2021 Pokemon SWSH",
        "insert": "Evolving Skies",
        "subset_name": "Evolving Skies",
        "parallel_name": "Base",
        "card_name": card_name,
        "set_number": "215",
        "grader": "psa",
        "grade": "10",
        "cert_number": None,
    }
    fields.update(overrides)
    return CardIdentity(**fields)


def checklist_card(
    card_id: str,
    card_name: str | None = None,
    *,
    grader: str = "psa",
    grade: str = "10",
    cert: str | None = None,
    label: str | None = None,
    value: float | None = None,
) -> ChecklistCard:
    """A Checklist card whose identity's card name defaults to its id."""
    name = card_name if card_name is not None else card_id
    return ChecklistCard(
        card_id=card_id,
        label=label if label is not None else f"label {card_id}",
        tier_name="Lineup",
        tier_type=TierType.LINE_UP,
        hit_rate=1.0,
        grader=grader,
        grade=grade,
        market_value=value,
        value_confidence=None if value is None else ValueConfidence.HIGH,
        cert_number=cert,
        identity=identity(name, grader=grader, grade=grade, cert_number=cert),
    )


def quote(
    value: float,
    source: str,
    *,
    confidence: ValueConfidence = ValueConfidence.HIGH,
    as_of: date = TODAY,
    sales_count: int | None = 10,
    citation: str | None = None,
) -> ValueQuote:
    return ValueQuote(
        market_value=value,
        source=source,
        as_of=as_of,
        sales_count=sales_count,
        confidence=confidence,
        citation=citation,
    )


class Clock:
    """A settable clock for CreditBudget, so a provider's spending happens at
    the same moment the resolver checks the budget."""

    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class FakeProvider:
    """Answers by card name. A name it has no answer for is Unmatched.

    `answers` values can be a quote, NotFound, Unmatched, or a
    ProviderUnavailable to raise. A callable is called with the identity.
    With `budget`, each quote spends `credit_cost` first, as a real provider
    spends before each request, and raises ProviderUnavailable when it can't.
    """

    def __init__(
        self,
        name: str,
        answers: Mapping[str, Answer] | Callable[[CardIdentity], Answer] = (),
        *,
        credit_cost: int = 0,
        budget: CreditBudget | None = None,
    ) -> None:
        self.name = name
        self.credit_cost = credit_cost
        self._answers = answers if callable(answers) else dict(answers)
        self._budget = budget
        self.calls: list[CardIdentity] = []

    def quote(self, identity: CardIdentity) -> ValueQuote | NotFound | Unmatched:
        self.calls.append(identity)
        if self._budget is not None and not self._budget.try_spend(
            self.name, self.credit_cost
        ):
            raise ProviderUnavailable(reason="out of credits")
        if callable(self._answers):
            answer = self._answers(identity)
        else:
            answer = self._answers.get(identity.card_name or "", Unmatched())
        if isinstance(answer, ProviderUnavailable):
            raise answer
        return answer

    @property
    def called_names(self) -> list[str | None]:
        return [call.card_name for call in self.calls]
