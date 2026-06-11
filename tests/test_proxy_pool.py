from src.config import AppConfig
from src.fetcher.engine import FetchEngine
from src.proxy_pool import FreeProxyPool, parse_proxy_lines


def test_parse_proxy_lines_accepts_supported_protocols_only():
    text = """
    http://1.1.1.1:8080
    https://2.2.2.2:8443
    socks4://3.3.3.3:1080
    socks5://4.4.4.4:1080
    ftp://5.5.5.5:21
    not-a-proxy
    http://1.1.1.1:8080
    """

    assert parse_proxy_lines(text) == [
        "http://1.1.1.1:8080",
        "https://2.2.2.2:8443",
        "socks4://3.3.3.3:1080",
        "socks5://4.4.4.4:1080",
    ]


def test_free_proxy_pool_returns_none_when_disabled():
    pool = FreeProxyPool(AppConfig(enable_free_proxy_pool=False))

    assert pool.get_proxy() is None


def test_free_proxy_pool_can_filter_by_scheme(monkeypatch):
    pool = FreeProxyPool(AppConfig(enable_free_proxy_pool=True))
    monkeypatch.setattr(pool, "_refresh_if_needed", lambda: None)
    pool._proxies = ["socks5://1.1.1.1:1080", "http://2.2.2.2:8080"]

    assert pool.get_proxy(allowed_schemes={"http", "https"}) == "http://2.2.2.2:8080"


def test_fetch_engine_prefers_explicit_proxy_over_free_pool(monkeypatch):
    cfg = AppConfig(
        https_proxy="http://stable-proxy:8080",
        enable_free_proxy_pool=True,
    )
    engine = FetchEngine(cfg)
    monkeypatch.setattr(engine._free_proxy_pool, "get_proxy", lambda: "http://free-proxy:8080")

    try:
        assert engine._select_proxy() == "http://stable-proxy:8080"
    finally:
        engine.close()
