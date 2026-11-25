"""Who a Value Quote's figure must be credited to.

Kept apart from the providers, so the output layer can cite a source without
depending on a provider module. No I/O.
"""

from arenaclub_optimizer.values.quote import ValueQuote

# CardVolume's provider name (a quote's `source`) and its site. Its terms ask
# for a citation wherever its figures are used.
CARDVOLUME_NAME = "cardvolume"
CARDVOLUME_SITE_URL = "https://cardvolume.com"


def citation(quote: ValueQuote) -> str | None:
    """The attribution to print for `quote`: its own citation if it has one.
    A CardVolume quote without one (which its provider never makes, though
    an old cache row might) is still credited to CardVolume. Any other quote
    without a citation needs none."""
    if quote.citation:
        return quote.citation
    if quote.source == CARDVOLUME_NAME:
        return f"CardVolume, {CARDVOLUME_SITE_URL}/"
    return None
