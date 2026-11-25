import pytest

from src.config import ConfigError, load_config, parse_config


def test_valid_config_loads(config):
    assert config.owner.nooks_user_id == "5koD9szETWeI1F0DcRTqkoHGPlY2"
    assert config.limits.daily_call_cap == 40
    assert config.company_size.max_employees == 2000
    assert [w.name for w in config.call_windows] == ["morning", "afternoon"]
    assert config.call_windows[0].start.hour == 8 and config.call_windows[0].start.minute == 45
    assert config.territory.timezones["Australia"].states["Queensland"] == "Australia/Brisbane"
    assert config.territory.timezones["New Zealand"].default == "Pacific/Auckland"
    assert config.tiers["tier1"].min_employees == 200
    assert config.signals.weights.cooldown_penalty == -30
    assert "Funding" in config.signals.scoop_types_keep
    assert config.filters.drop_if_no_domain is True
    assert config.sources[0] == "salesforce"


def test_missing_file():
    with pytest.raises(FileNotFoundError):
        load_config("nope.yaml")


def test_invalid_missing_section(raw_config):
    del raw_config["limits"]
    with pytest.raises(ConfigError, match="limits"):
        parse_config(raw_config)


def test_invalid_wrong_type(raw_config):
    raw_config["limits"]["daily_call_cap"] = "forty"
    with pytest.raises(ConfigError, match=r"limits\.daily_call_cap"):
        parse_config(raw_config)


def test_invalid_bool_is_not_int(raw_config):
    raw_config["limits"]["daily_call_cap"] = True
    with pytest.raises(ConfigError, match=r"limits\.daily_call_cap"):
        parse_config(raw_config)


def test_invalid_timezone_name(raw_config):
    raw_config["territory"]["timezones"]["Australia"]["Queensland"] = "Australia/Nowhere"
    with pytest.raises(ConfigError, match="IANA"):
        parse_config(raw_config)


def test_invalid_call_window(raw_config):
    raw_config["call_windows"][0]["start"] = "12:00"
    raw_config["call_windows"][0]["end"] = "09:00"
    with pytest.raises(ConfigError, match="start must be before end"):
        parse_config(raw_config)


def test_invalid_size_range(raw_config):
    raw_config["company_size"]["min_employees"] = 5000
    with pytest.raises(ConfigError, match="min_employees"):
        parse_config(raw_config)
