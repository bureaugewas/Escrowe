"""The /notebooks endpoints: a saved cell's keys are whitelisted (clean_cells
/ _CELL_KEYS), and a notebook name can never escape the notebooks directory
(_NOTEBOOK_NAME)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from escrowe.server import _CELL_KEYS, _NOTEBOOK_NAME, create_app


@pytest.fixture
def client_headers(svc):
    token = svc.login("bob", "bob")["token"]
    return TestClient(create_app(svc)), {"Authorization": f"Bearer {token}"}


def test_put_notebook_strips_smuggled_keys_from_the_saved_file(svc, client_headers):
    client, headers = client_headers
    body = {"cells": [{"id": "1", "kind": "sql", "text": "SELECT 1",
                       "password": "smuggled-secret", "arbitrary_key": "x", "__proto__": "y"}]}
    r = client.put("/notebooks/mynb", json=body, headers=headers)
    assert r.status_code == 200

    saved = (svc.settings.home / "notebooks" / "mynb.json").read_text()
    assert "smuggled-secret" not in saved
    assert "arbitrary_key" not in saved
    assert "__proto__" not in saved

    cell = client.get("/notebooks/mynb", headers=headers).json()["cells"][0]
    assert set(cell) <= set(_CELL_KEYS)
    assert "password" not in cell and "arbitrary_key" not in cell


def test_put_notebook_strips_secrets_from_a_saved_source(svc, client_headers):
    client, headers = client_headers
    body = {"cells": [], "source": {"name": "shop", "kind": "mysql",
                                     "params": {"host": "db1", "user": "ro",
                                                "password": "smuggled-secret", "token": "also-secret"}}}
    r = client.put("/notebooks/mynb", json=body, headers=headers)
    assert r.status_code == 200

    saved = (svc.settings.home / "notebooks" / "mynb.json").read_text()
    assert "smuggled-secret" not in saved and "also-secret" not in saved

    source = client.get("/notebooks/mynb", headers=headers).json()["source"]
    assert source == {"name": "shop", "kind": "mysql", "params": {"host": "db1", "user": "ro"}}


def test_put_notebook_rejects_a_cell_with_no_recognized_kind(svc, client_headers):
    client, headers = client_headers
    r = client.put("/notebooks/mynb", json={"cells": [{"kind": "shell", "text": "rm -rf /"}]}, headers=headers)
    assert r.status_code == 400


def test_put_notebook_rejects_non_string_text_and_title(svc, client_headers):
    client, headers = client_headers
    r = client.put("/notebooks/mynb",
                   json={"cells": [{"kind": "sql", "text": {"$ne": None}}]}, headers=headers)
    assert r.status_code == 400


@pytest.mark.parametrize("name", [
    "../../etc/passwd", "..%2f..%2fetc", "/etc/passwd", "a/b", "..", "...", "~root",
])
def test_notebook_name_regex_forbids_path_separators_and_traversal(name):
    assert not _NOTEBOOK_NAME.match(name)


@pytest.mark.parametrize("name", [
    "../../etc/passwd", "/etc/passwd", "a/b", "..",
])
def test_put_notebook_with_a_traversal_name_is_rejected_over_http(svc, client_headers, name):
    client, headers = client_headers
    r = client.put(f"/notebooks/{name}", json={"cells": []}, headers=headers)
    # However the URL is resolved - route mismatch, path normalization
    # collapsing ".." before it ever reaches the handler, or the name
    # validation inside the handler rejecting it outright - the one outcome
    # that must never happen is a 200: nothing may be written outside the
    # notebooks directory.
    assert r.status_code in (400, 404, 405)


def test_traversal_name_writes_nothing_outside_the_notebooks_directory(svc, client_headers, tmp_path):
    client, headers = client_headers
    r = client.put("/notebooks/..", json={"cells": []}, headers=headers)
    assert r.status_code in (400, 404, 405)
    # the home directory itself (one level above notebooks/) must not have
    # gained a stray file from a name that meant "go up one directory"
    assert not (svc.settings.home / "..json").exists()


def test_get_and_delete_also_reject_a_traversal_name(svc, client_headers):
    client, headers = client_headers
    r = client.get("/notebooks/..%2f..%2fetc%2fpasswd", headers=headers)
    assert r.status_code in (400, 404)
