import pytest

from tw_fundamental import theme_store, users, web


@pytest.fixture(autouse=True)
def isolated_data(monkeypatch, tmp_path):
    """Every test gets its own accounts database and saved-analysis store."""
    monkeypatch.setattr(users, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(theme_store, "store_dir", lambda: tmp_path / "themes")
    monkeypatch.setattr(web.app, "secret_key", "test-secret")
    web._failed_logins.clear()


CSRF = "test-csrf"


def sign_in(client, username="alice", admin=True, password="password123"):
    """Create an account and put it in the test client's session (with a known CSRF token)."""
    u = users.create(username, password, is_admin=admin)
    with client.session_transaction() as s:
        s.update(uid=u.id, sv=u.session_version, csrf=CSRF)
    return u
