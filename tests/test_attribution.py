"""Crediting a Value Quote's source, and keeping the output layer free of
provider modules."""

import ast
import inspect

from fake_values import quote

from arenaclub_optimizer import output
from arenaclub_optimizer.providers import cardvolume
from arenaclub_optimizer.values.attribution import (
    CARDVOLUME_NAME,
    CARDVOLUME_SITE_URL,
    citation,
)


def test_the_cardvolume_provider_uses_the_shared_name_and_site():
    assert cardvolume.NAME == CARDVOLUME_NAME == "cardvolume"
    assert cardvolume.SITE_URL == CARDVOLUME_SITE_URL == "https://cardvolume.com"


def test_a_quote_with_a_citation_is_cited_as_it_is():
    assert citation(quote(1.0, "pokeprice", citation="Cite me")) == "Cite me"


def test_a_cardvolume_quote_without_a_citation_is_attributed_to_cardvolume():
    bare = quote(1.0, CARDVOLUME_NAME, citation=None)

    assert citation(bare) == "CardVolume, https://cardvolume.com/"


def test_any_other_quote_without_a_citation_has_none():
    assert citation(quote(1.0, "pokeprice", citation=None)) is None
    assert citation(quote(1.0, "pokeprice", citation="")) is None


def test_output_does_not_import_a_provider_module():
    tree = ast.parse(inspect.getsource(output))
    imported = [
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    ] + [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    ]

    assert not [name for name in imported if "providers" in name]
