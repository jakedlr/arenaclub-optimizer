import httpx
import pytest


@pytest.fixture(autouse=True)
def _isolated_cache_home(tmp_path_factory, monkeypatch):
    """Point XDG_CACHE_HOME at a temp dir, so no test can touch ~/.cache."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path_factory.mktemp("xdg-cache")))


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Fail any request that would leave the process: tests use
    httpx.MockTransport or fakes, never the real services. Also hide a real
    PokéPrice key, so no code path can pick one up."""

    def refuse(self, request):
        raise RuntimeError(f"a test tried to reach the network: {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
    monkeypatch.delenv("POKEPRICE_API_KEY", raising=False)
