import pytest

from src.config import normalise_au_state, resolve_timezone

STATE_TZ = [
    ("New South Wales", "Australia/Sydney"), ("NSW", "Australia/Sydney"),
    ("Australian Capital Territory", "Australia/Sydney"), ("ACT", "Australia/Sydney"),
    ("Victoria", "Australia/Melbourne"), ("VIC", "Australia/Melbourne"),
    ("Queensland", "Australia/Brisbane"), ("QLD", "Australia/Brisbane"),
    ("Western Australia", "Australia/Perth"), ("WA", "Australia/Perth"),
    ("South Australia", "Australia/Adelaide"), ("SA", "Australia/Adelaide"),
    ("Tasmania", "Australia/Hobart"), ("TAS", "Australia/Hobart"),
    ("Northern Territory", "Australia/Darwin"), ("NT", "Australia/Darwin"),
]


@pytest.mark.parametrize("state,tz", STATE_TZ)
def test_every_state_and_abbreviation(config, state, tz):
    r = resolve_timezone(config, "Australia", state)
    assert (r.tz, r.source, r.guessed) == (tz, "state", False)


@pytest.mark.parametrize("raw", ["queensland", "QUEENSLAND", "  Qld ", "vIc"])
def test_mixed_case_and_spaces(config, raw):
    assert resolve_timezone(config, "AUSTRALIA", raw).source == "state"


def test_null_state_falls_back_to_country_default_as_guess(config):
    r = resolve_timezone(config, "Australia", None)
    assert (r.tz, r.source, r.guessed) == ("Australia/Sydney", "country_default", True)
    assert "null" in r.reason


@pytest.mark.parametrize("junk", ["Other", "n/a", "NULL", "NY", "Outside U.S./Canada", "", "Aukland"])
def test_junk_state_gives_none_with_reason_then_default(config, junk):
    state, why = normalise_au_state(junk)
    assert state is None and why
    r = resolve_timezone(config, "Australia", junk)
    assert r.tz == "Australia/Sydney" and r.guessed


def test_hq_state_used_when_billing_state_missing(config):
    r = resolve_timezone(config, "AU", None, None, hq_state="Queensland")
    assert (r.tz, r.source) == ("Australia/Brisbane", "hq_state")


def test_city_hint_when_states_missing(config):
    r = resolve_timezone(config, "Australia", None, "Perth")
    assert (r.tz, r.source) == ("Australia/Perth", "city")


def test_city_in_state_field(config):
    assert resolve_timezone(config, "Australia", "Sydney").tz == "Australia/Sydney"


def test_billing_state_beats_hq_and_city(config):
    assert resolve_timezone(config, "Australia", "WA", "Sydney", "Victoria").tz == "Australia/Perth"


@pytest.mark.parametrize("state", ["Auckland", "Wellington", "Aukland", None])
def test_nz_always_auckland(config, state):
    r = resolve_timezone(config, "New Zealand", state)
    assert (r.tz, r.source, r.guessed) == ("Pacific/Auckland", "nz", False)


@pytest.mark.parametrize("country", ["Singapore", None, ""])
def test_non_anz_country_is_none(config, country):
    r = resolve_timezone(config, country, "NSW")
    assert r.tz is None and r.reason
