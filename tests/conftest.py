import pytest
from fake_engine import register

from escrowe.agent import Agent
from escrowe.config import Settings
from escrowe.service import Escrowe
from escrowe.sources import Source
from escrowe.store import Store

register()   # makes kind="fake" available for the whole suite


@pytest.fixture
def svc(tmp_path):
    """An Escrowe connected as alice (who cannot see `employees`), with the mock agent."""
    settings = Settings(home=tmp_path, jwt_secret="test-secret", llm_provider="mock",
                        max_rows=50, query_timeout_s=5)
    svc = Escrowe(settings, store=Store(tmp_path / "cat.sqlite"), agent=Agent("mock"))
    svc.set_source(Source("fake", "fake", {"user": "alice", "password": "alice"}), persist=False)
    return svc


@pytest.fixture
def alice(svc):
    return svc.operator_principal()


@pytest.fixture
def bob(svc):
    return svc.principal(svc.login("bob", "bob")["token"])
