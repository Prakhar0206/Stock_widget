"""config.py: get_api_keys, Config.api_keys, key-aware check_budget / parse_config / load_config."""
import json
from decimal import Decimal

import pytest

from config import (BudgetError, Config, check_budget, get_api_key, get_api_keys, load_config,
                    min_poll_seconds, parse_config)


def test_get_api_keys_comma_separated_with_quotes_and_spaces():
    env = {"FINNHUB_API_KEY": ' "key1, key2 ", \'key3\' ,key4 '}
    assert get_api_keys(env) == ["key1", "key2", "key3", "key4"]


def test_get_api_keys_reads_both_variables_dedupes_and_orders():
    env = {"FINNHUB_API_KEY": "a,b", "FINNHUB_API_KEYS": "b, c ,,d"}
    assert get_api_keys(env) == ["a", "b", "c", "d"]


def test_get_api_keys_reads_numbered_variables():
    env = {"FINNHUB_API_KEY_1": "k1", "FINNHUB_API_KEY_2": "k2", "FINNHUB_API_KEY_3": "k3", "FINNHUB_API_KEY_4": "k4"}
    assert get_api_keys(env) == ["k1", "k2", "k3", "k4"]


def test_get_api_keys_empty_cases():
    assert get_api_keys({}) == []
    assert get_api_keys({"FINNHUB_API_KEY": '  "" ', "FINNHUB_API_KEYS": " , ; "}) == []


def test_get_api_keys_defaults_to_os_environ(monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "x1,x2")
    monkeypatch.delenv("FINNHUB_API_KEYS", raising=False)
    assert get_api_keys() == ["x1", "x2"]


def test_get_api_key_unchanged():
    assert get_api_key({"FINNHUB_API_KEY": ' "abc " '}) == "abc"
    assert get_api_key({}) is None


def test_check_budget_scales_with_keys():
    assert check_budget(30, 60) == Decimal(31)
    with pytest.raises(BudgetError, match="the limit is 48."):
        check_budget(30, 30)
    with pytest.raises(BudgetError):
        check_budget(30, 15)                          # 124 > 48
    assert check_budget(30, 15, num_keys=4) == Decimal(124)
    assert check_budget(30, 15, num_keys=4) <= 48 * 4
    with pytest.raises(BudgetError, match=r"limit is 192 \(4 keys x 48\)"):
        check_budget(30, 5, num_keys=4)               # 372 > 192
    

def test_check_budget_edges():
    with pytest.raises(BudgetError):
        check_budget(30, 15, num_keys=0)              # 0 keys counts as 1
    with pytest.raises(BudgetError):
        check_budget(5, 0, num_keys=4)


def test_min_poll_seconds():
    assert min_poll_seconds(30) == 39
    assert min_poll_seconds(30, 4) == 10
    assert min_poll_seconds(0, 1) == 2


def test_config_api_keys_default_and_not_in_repr():
    assert Config().api_keys == []
    assert "SECRET" not in repr(Config(api_keys=["SECRET"]))


def test_parse_config_api_keys_list_and_string():
    assert parse_config({"api_keys": [' "a" ', "b, c", "a"]}).api_keys == ["a", "b", "c"]
    assert parse_config({"api_keys": "a, b"}).api_keys == ["a", "b"]
    assert parse_config({}).api_keys == []


@pytest.mark.parametrize("bad", [123, {"k": "v"}, ["ok", 5], [None]])
def test_parse_config_api_keys_invalid(bad):
    with pytest.raises(ValueError, match="api_keys must be a list of strings"):
        parse_config({"api_keys": bad})


def test_parse_config_budget_counts_json_keys():
    d = {"poll_seconds": 15, "api_keys": ["a", "b", "c", "d"]}
    assert parse_config(d).poll_seconds == 15
    with pytest.raises(BudgetError):
        parse_config({"poll_seconds": 15, "api_keys": ["a"]})
    with pytest.raises(BudgetError):
        parse_config({"poll_seconds": 15})


def test_parse_config_env_keys_win_for_budget():
    assert parse_config({"poll_seconds": 15}, num_env_keys=4).poll_seconds == 15
    # env supplies 1 key, config lists 4: env wins, so 15 s is still refused
    with pytest.raises(BudgetError):
        parse_config({"poll_seconds": 15, "api_keys": list("abcd")}, num_env_keys=1)


def test_load_config_roundtrip_and_invalid_message_has_no_key(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"poll_seconds": 15, "api_keys": ["a", "b", "c", "d"]}))
    cfg, warns = load_config(p)
    assert cfg.poll_seconds == 15 and cfg.api_keys == list("abcd") and warns == []
    p.write_text(json.dumps({"poll_seconds": 15, "api_keys": ["SECRETKEY"]}))
    cfg, warns = load_config(p)
    assert cfg.poll_seconds == 60 and "SECRETKEY" not in warns[0] and "invalid" in warns[0]
    cfg, warns = load_config(p, num_env_keys=4)       # same file is fine when the env brings 4 keys
    assert warns == [] and cfg.poll_seconds == 15
    p.write_text(json.dumps({"api_keys": [12345678]}))
    assert "12345678" not in load_config(p)[1][0]
