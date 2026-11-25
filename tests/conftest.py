"""Shared fixtures. Tests use only tests/fixtures/ (synthetic) and config/config.yaml."""
import copy
import json
from pathlib import Path

import pytest
import yaml

from src.config import load_config

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"


def load_fixture(name: str):
    with open(FIXTURES / name, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="session")
def config():
    return load_config(ROOT / "config" / "config.yaml")


@pytest.fixture
def raw_config():
    """Fresh mutable dict of the real config, for building invalid variants."""
    with open(ROOT / "config" / "config.yaml", encoding="utf-8") as f:
        return copy.deepcopy(yaml.safe_load(f))
