import pytest


@pytest.fixture(autouse=True)
def _isolated_cache_home(tmp_path_factory, monkeypatch):
    """Point XDG_CACHE_HOME at a temp dir, so no test can touch ~/.cache."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path_factory.mktemp("xdg-cache")))
