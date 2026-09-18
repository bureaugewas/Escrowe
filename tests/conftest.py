import pytest

from escrowe.agent import Agent
from escrowe.config import Attachment, Settings
from escrowe.service import Escrowe
from escrowe.store import Store

from fake_engine import register

register()   # makes kind="fake" available to Source/engines.build for the whole suite


@pytest.fixture
def svc(tmp_path):
    settings = Settings(home=tmp_path, jwt_secret="test-secret",
                        attachments=[Attachment("fake", "fake", "user=alice password=alice")],
                        llm_provider="mock", max_rows=50, query_timeout_s=5)
    return Escrowe(settings, store=Store(tmp_path / "cat.sqlite"), agent=Agent("mock"))


@pytest.fixture
def alice(svc):
    return svc.operator_principal()


@pytest.fixture
def bob(svc):
    return svc.principal(svc.login("bob", "bob")["token"])
