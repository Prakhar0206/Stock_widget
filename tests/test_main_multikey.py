"""main.build_runtime: key pool wiring, config fallback, budget guard, notice forwarding."""
import json
from decimal import Decimal

import main
from providers.finnhub import FinnhubProvider
from providers.mock import MockProvider


def rt(tmp_path, env, config=None, watchlist=None, extra=()):
    if config is not None:
        (tmp_path / "config.json").write_text(json.dumps(config))
    if watchlist is not None:
        (tmp_path / "watchlist.json").write_text(json.dumps(watchlist))
    return main.build_runtime(main.parse_args(["--data-dir", str(tmp_path), *extra]), environ=env)


def test_env_keys_build_a_pool_and_allow_fast_polling(tmp_path):
    r = rt(tmp_path, {"FINNHUB_API_KEY": "k1, k2, k3, k4"}, config={"poll_seconds": 15})
    assert isinstance(r.provider, FinnhubProvider) and r.provider.key_count == 4
    assert r.polling and r.num_keys == 4 and r.cfg.poll_seconds == 15 and r.warnings == []


def test_single_env_key_behaves_like_before(tmp_path):
    r = rt(tmp_path, {"FINNHUB_API_KEY": ' "abc" '})
    assert isinstance(r.provider, FinnhubProvider) and r.provider.key_count == 1
    assert r.polling and r.cfg.poll_seconds == 60 and r.warnings == []


def test_no_keys_means_mock_provider_and_no_polling(tmp_path):
    r = rt(tmp_path, {})
    assert isinstance(r.provider, MockProvider) and not r.polling and r.num_keys == 0


def test_config_json_keys_are_the_fallback_and_env_wins(tmp_path):
    cfg = {"poll_seconds": 15, "api_keys": ["c1", "c2", "c3", "c4"]}
    r = rt(tmp_path, {}, config=cfg)
    assert r.provider.key_count == 4 and r.polling and r.cfg.poll_seconds == 15
    r2 = main.build_runtime(main.parse_args(["--data-dir", str(tmp_path)]),
                            environ={"FINNHUB_API_KEYS": "e1,e2,e3,e4,e5"})
    assert r2.provider.key_count == 5


def test_fast_poll_without_enough_keys_falls_back_to_defaults_with_warning(tmp_path):
    r = rt(tmp_path, {"FINNHUB_API_KEY": "k1"}, config={"poll_seconds": 15})
    assert r.cfg.poll_seconds == 60 and "config.json is invalid" in r.warnings[0]


def test_long_watchlist_slows_polling_instead_of_crashing(tmp_path):
    wl = {"symbols": [f"S{i}" for i in range(60)]}                 # 61 calls per poll
    r = rt(tmp_path, {"FINNHUB_API_KEY": "k1,k2"}, config={"poll_seconds": 30, "max_symbols": 30}, watchlist=wl)
    # 61 calls * 60 / 30 s = 122 > 96 (2 keys) -> slowed to ceil(61*60/96) = 39 s
    assert r.cfg.poll_seconds == 39 and "Polling slowed to every 39 s" in r.warnings[0]


def test_watchlist_size_is_best_effort(tmp_path):
    assert main.watchlist_size(tmp_path) is None
    (tmp_path / "watchlist.json").write_text("not json")
    assert main.watchlist_size(tmp_path) is None
    (tmp_path / "watchlist.json").write_text(json.dumps(["A", "B"]))
    assert main.watchlist_size(tmp_path) == 2
    (tmp_path / "watchlist.json").write_text(json.dumps({"weird": 1}))
    assert main.watchlist_size(tmp_path) is None


def test_mock_mode_ignores_env_keys(tmp_path):
    r = rt(tmp_path, {"FINNHUB_API_KEY": "k1,k2"}, extra=["--mock"])
    assert r.demo and not isinstance(r.provider, FinnhubProvider)


def test_forward_pool_notices_dedupes_and_tolerates_plain_providers():
    warns = []
    class P:
        def __init__(self): self.n = ["Finnhub key #2 was rejected (HTTP 401) and is skipped."]
        def pop_notices(self):
            out, self.n = self.n, []
            return out
    p = P()
    main.forward_pool_notices(p, warns)
    main.forward_pool_notices(p, warns)
    assert warns == ["Finnhub key #2 was rejected (HTTP 401) and is skipped."]
    main.forward_pool_notices(object(), warns)           # no pop_notices: silently ignored
    assert len(warns) == 1
