import re
import sqlite3
from types import SimpleNamespace

import pytest

from conftest import CSRF, sign_in
from tw_fundamental import activity, theme, theme_store, users, web, wishlist
from tw_fundamental.theme import StockProfile, Theme, ThemeResult


# --------------------------------------------------------------------------- users.py

def test_first_account_is_admin_and_names_are_case_insensitive():
    a = users.create("Alice", "password123")
    b = users.create("bob", "password123")
    assert a.is_admin and not b.is_admin
    with pytest.raises(users.UserError, match="taken"):
        users.create("alice", "password123")
    assert users.get_by_name("ALICE").id == a.id


@pytest.mark.parametrize("name,pw", [("ab", "password123"), ("bad name", "password123"), ("a/b", "password123"),
                                     ("carol", "short")])
def test_validation(name, pw):
    with pytest.raises(users.UserError):
        users.create(name, pw)


def test_authenticate_and_password_change():
    u = users.create("alice", "password123")
    assert users.authenticate("alice", "wrong-password") is None
    assert users.authenticate("nobody", "password123") is None
    signed = users.authenticate("ALICE", "password123")
    assert signed.id == u.id and signed.last_login
    with pytest.raises(users.UserError, match="current password"):
        users.change_password(u.id, "wrong", "new-password-1")
    users.change_password(u.id, "password123", "new-password-1")
    assert users.get(u.id).session_version == u.session_version + 1
    assert users.authenticate("alice", "new-password-1")
    temp = users.reset_password(u.id)
    assert users.authenticate("alice", temp) and not users.authenticate("alice", "new-password-1")


def test_disabled_accounts_cannot_sign_in():
    users.create("admin", "password123")
    u = users.create("bob", "password123")
    users.set_active(u.id, False)
    assert users.authenticate("bob", "password123") is None
    users.set_active(u.id, True)
    assert users.authenticate("bob", "password123")


def test_last_active_admin_is_protected():
    a = users.create("admin", "password123")
    for action in (lambda: users.set_admin(a.id, False), lambda: users.set_active(a.id, False),
                   lambda: users.delete(a.id)):
        with pytest.raises(users.UserError, match="at least one active admin"):
            action()
    b = users.create("bob", "password123", is_admin=True)
    users.set_admin(a.id, False)
    assert not users.get(a.id).is_admin and users.get(b.id).is_admin


def test_secret_key_is_created_once(tmp_path):
    k = users.secret_key()
    assert len(k) == 64 and users.secret_key() == k
    assert (users.DATA_DIR / "secret_key").stat().st_mode & 0o077 == 0


# --------------------------------------------------------------------------- web

@pytest.fixture
def anon(monkeypatch):
    monkeypatch.setattr(web, "list_stocks", lambda: [{"id": "2330", "name": "台積電", "market": "twse",
                                                       "industry": "半導體業"}])
    web.app.config["TESTING"] = True
    return web.app.test_client()


def csrf_of(client, path):
    body = client.get(path).get_data(as_text=True)
    return re.search(r"name='csrf' value='([^']+)'", body).group(1)


def test_pages_require_sign_in(anon):
    r = anon.get("/themes?lang=en")
    assert r.status_code == 302 and "/login?next=/themes" in r.headers["Location"]
    assert anon.get("/").headers["Location"].endswith("/login")
    assert anon.get("/login").status_code == 200


def test_register_signs_in_and_first_user_is_admin(anon):
    token = csrf_of(anon, "/register")
    r = anon.post("/register", data={"csrf": token, "username": "alice", "password": "password123",
                                     "confirm": "password123", "next": "/themes"})
    assert r.status_code == 303 and r.headers["Location"] == "/themes"
    body = anon.get("/themes").get_data(as_text=True)
    assert "first user, so your account is an admin" in body and ">alice</a>" in body and "href='/admin'" in body


def test_register_errors(anon):
    users.create("alice", "password123")
    token = csrf_of(anon, "/register")
    r = anon.post("/register", data={"csrf": token, "username": "Alice", "password": "password123",
                                     "confirm": "password123"})
    assert r.status_code == 400 and "already taken" in r.get_data(as_text=True)
    r = anon.post("/register", data={"csrf": token, "username": "bob", "password": "password123",
                                     "confirm": "different1"})
    assert "do not match" in r.get_data(as_text=True)


def test_login_redirects_only_to_local_pages(anon):
    users.create("alice", "password123")
    token = csrf_of(anon, "/login")
    r = anon.post("/login", data={"csrf": token, "username": "alice", "password": "password123",
                                  "next": "//evil.example/x"})
    assert r.status_code == 303 and r.headers["Location"] == "/"
    token = csrf_of(anon, "/login")  # the session was replaced on sign-in
    assert anon.get("/themes").status_code == 200 and token


def test_login_throttles_repeated_failures(anon):
    users.create("alice", "password123")
    token = csrf_of(anon, "/login")
    for _ in range(5):
        r = anon.post("/login", data={"csrf": token, "username": "alice", "password": "nope-nope"})
        assert r.status_code == 401 and "Wrong username or password" in r.get_data(as_text=True)
    r = anon.post("/login", data={"csrf": token, "username": "alice", "password": "password123"})
    assert "Too many failed sign-ins" in r.get_data(as_text=True)


def test_post_without_csrf_token_is_rejected(anon):
    sign_in(anon)
    assert anon.post("/logout").status_code == 400
    assert anon.post("/logout", data={"csrf": CSRF}).status_code == 303
    assert anon.get("/themes").status_code == 302


def test_disable_and_password_change_end_sessions(anon):
    users.create("admin", "password123")
    bob = sign_in(anon, "bob", admin=False)
    assert anon.get("/themes").status_code == 200
    users.set_active(bob.id, False)
    assert anon.get("/themes").status_code == 302


def test_account_password_change_keeps_this_session(anon):
    sign_in(anon)
    r = anon.post("/account", data={"csrf": CSRF, "current": "password123", "new": "new-password-1",
                                    "confirm": "new-password-1"})
    assert r.status_code == 303
    assert "Password changed" in anon.get("/account").get_data(as_text=True)
    r = anon.post("/account", data={"csrf": csrf_of(anon, "/account"), "current": "wrong", "new": "x" * 9,
                                    "confirm": "x" * 9})
    assert r.status_code == 400 and "not correct" in r.get_data(as_text=True)


def test_claude_is_admin_only(anon, monkeypatch):
    from tw_fundamental import theme
    calls = []

    def fake_analyze(t, news_days=3, use_llm=False, refresh=False, lang="en"):
        calls.append(use_llm)
        return ThemeResult(t, [], {}, news_days, lang=lang)
    monkeypatch.setattr(theme, "analyze", fake_analyze)
    monkeypatch.setattr(web.llm, "available", lambda: True)
    users.create("admin", "password123")
    sign_in(anon, "bob", admin=False)
    assert "(admins only)" in anon.get("/themes").get_data(as_text=True)
    body = anon.get("/theme?name=cpo&llm=1").get_data(as_text=True)
    assert calls == [False] and "available to admins only" in body
    assert anon.get("/admin/users").status_code == 403


def saved_run(owner):
    r = ThemeResult(Theme("CPO", "CPO"), [StockProfile("2330", "台積電")], {}, 3)
    key = f"{abs(hash(owner)) % 16**8:08x}"  # ids are time + key: distinct keys keep same-second runs apart
    return theme_store.save(r, key, {"name": "CPO"}, owner=owner)


def test_saved_runs_are_shared_but_deletable_by_owner_or_admin(anon):
    users.create("admin", "password123")
    sign_in(anon, "bob", admin=False)
    mine, theirs = saved_run("bob"), saved_run("admin")
    body = anon.get("/themes/saved").get_data(as_text=True)
    assert body.count("/delete") == 1 and ">admin</td>" in body  # shared list, one deletable row
    assert anon.post(f"/theme/saved/{theirs}/delete", data={"csrf": CSRF}).status_code == 403
    assert anon.post(f"/theme/saved/{mine}/delete", data={"csrf": CSRF}).status_code == 303
    assert [m["id"] for m in theme_store.list_saved()] == [theirs]


def test_admin_manages_users(anon):
    me = sign_in(anon, "admin", admin=True)
    bob = users.create("bob", "password123")
    saved_run("bob")
    body = anon.get("/admin/users").get_data(as_text=True)
    assert "bob" in body and "(you)" in body

    def act(user_id, action):
        return anon.post(f"/admin/users/{user_id}/{action}", data={"csrf": CSRF}, follow_redirects=True)

    assert "bob is now an admin" in act(bob.id, "grant-admin").get_data(as_text=True)
    assert users.get(bob.id).is_admin
    act(bob.id, "revoke-admin")
    act(bob.id, "disable")
    assert not users.get(bob.id).is_active
    body = act(bob.id, "reset-password").get_data(as_text=True)
    temp = re.search(r"Temporary password for bob: (\S+)", body).group(1)
    users.set_active(bob.id, True)
    assert users.authenticate("bob", temp)
    assert "change your own account" in act(me.id, "disable").get_data(as_text=True)
    assert users.get(me.id).is_active
    act(bob.id, "delete")
    assert users.get(bob.id) is None and len(theme_store.list_saved()) == 1  # their runs stay
    assert anon.post(f"/admin/users/{bob.id}/delete", data={"csrf": CSRF}).status_code == 404


def test_first_request_loads_the_secret_key(anon, monkeypatch):
    monkeypatch.setattr(web.app, "secret_key", None)
    assert anon.get("/login").status_code == 200 and web.app.secret_key == users.secret_key()


# --------------------------------------------------------------------------- guests

@pytest.fixture
def guest(anon, monkeypatch):
    runs = []
    monkeypatch.setattr(theme, "analyze", lambda *a, **k: runs.append(a) or pytest.fail("a guest started a run"))
    anon.runs = runs
    r = anon.post("/login/guest", data={"csrf": csrf_of(anon, "/login"), "next": "/themes"})
    assert r.status_code == 303 and r.headers["Location"] == "/themes"
    return anon


def theme_run(title="cpo", lang="en"):
    t = Theme(title, title.upper(), keywords=[title])
    r = ThemeResult(t, [StockProfile("2330", "台積電")], {}, 3, lang=lang)
    return theme_store.save(r, theme_store.params_key(t, 3, False, lang), {"name": title}, owner="alice")


def test_guest_reads_reports_and_saved_analyses(guest, monkeypatch):
    monkeypatch.setattr(web, "build_report", lambda sid, *a, **k: SimpleNamespace(html=f"report {sid}"))
    rid = theme_run()
    assert guest.get("/").status_code == 200
    assert guest.get("/report/2330").get_data(as_text=True) == "report 2330"
    assert guest.get("/themes/saved").status_code == 200
    body = guest.get(f"/theme/saved/{rid}").get_data(as_text=True)
    assert "Saved analysis from" in body and "Run again" not in body
    body = guest.get("/theme?name=cpo").get_data(as_text=True)  # a preset opens its newest run
    assert "Saved analysis from" in body


def test_guest_cannot_request_or_run_analyses(guest):
    theme_run()
    body = guest.get("/themes").get_data(as_text=True)
    assert "Guest" in body and 'action="/requests"' not in body and "id='custom'" not in body and 'id="custom"' not in body
    assert "As a guest you can open saved analyses" in body
    token = csrf_of(guest, "/login")
    assert guest.post("/requests", data={"csrf": token, "topic": "Robots"}).status_code == 403
    assert wishlist.list_requests() == []
    assert guest.get("/theme?name=robots&stocks=2330").status_code == 403  # no saved run: not started
    assert guest.get("/theme?name=memory&refresh=1").status_code == 403
    assert guest.runs == []


def test_guest_cannot_reach_account_or_admin_pages(guest):
    rid = theme_run()
    token = csrf_of(guest, "/login")
    for path in ("/account", "/admin/users", "/admin/requests", "/requests/notices"):
        assert guest.get(path).status_code == 403, path
    assert guest.post(f"/theme/saved/{rid}/delete", data={"csrf": token}).status_code == 403
    assert theme_store.get_meta(rid)


def test_guest_can_then_sign_in_or_register(guest):
    users.create("alice", "password123")
    assert guest.get("/register").status_code == 200
    r = guest.post("/login", data={"csrf": csrf_of(guest, "/login"), "username": "alice", "password": "password123"})
    assert r.status_code == 303
    assert "Sign out" in guest.get("/themes").get_data(as_text=True)


def test_guest_session_needs_the_signed_cookie(anon):
    assert anon.get("/themes").status_code == 302  # no guest flag, no entry
    assert anon.post("/login/guest", data={"csrf": "wrong"}).status_code == 400
    assert not users.USERNAME_RE.match(users.GUEST.username)  # cannot collide with an account


# --------------------------------------------------------------------------- admin panel

def test_old_database_gains_the_privileged_column(tmp_path, monkeypatch):
    monkeypatch.setattr(users, "DATA_DIR", tmp_path / "old")
    users.DATA_DIR.mkdir()
    conn = sqlite3.connect(users.db_path())
    conn.execute(users.SCHEMA.replace("  is_privileged INTEGER NOT NULL DEFAULT 0,\n", ""))
    conn.execute("INSERT INTO users (username, password_hash, created) VALUES ('old', 'x', '2026-01-01')")
    conn.commit()
    conn.close()
    assert users.get_by_name("old").is_privileged is False


def test_role_changes_keep_one_role_and_one_admin():
    admin, bob = users.create("admin", "password123"), users.create("bob", "password123")
    users.set_privileged(bob.id, True)
    assert users.get(bob.id).is_privileged and users.get(bob.id).has_admin_rights and not users.get(bob.id).is_admin
    users.set_admin(bob.id, True)
    assert users.get(bob.id).is_admin and not users.get(bob.id).is_privileged
    users.set_privileged(bob.id, False)  # not a privileged user: leaves the admin role alone
    assert users.get(bob.id).is_admin
    users.set_privileged(bob.id, True)  # ends the admin role
    assert not users.get(bob.id).is_admin
    with pytest.raises(users.UserError, match="at least one active admin"):
        users.set_privileged(admin.id, True)  # bob's rights do not replace the last admin
    users.set_privileged(bob.id, False)
    assert not users.get(bob.id).has_admin_rights


def test_privileged_user_has_admin_rights(anon, monkeypatch):
    monkeypatch.setattr(web.llm, "available", lambda: True)
    used = []
    monkeypatch.setattr(theme, "analyze", lambda t, news_days=3, use_llm=False, refresh=False, lang="en":
                        used.append(use_llm) or ThemeResult(t, [StockProfile("2330", "台積電")], {}, news_days, lang=lang))
    admin = users.create("admin", "password123")
    amy = users.create("amy", "password123")
    me = sign_in(anon, "bob", admin=False)
    for path in ("/admin", "/admin/users", "/admin/activity", "/admin/requests"):
        assert anon.get(path).status_code == 403, path
    users.set_privileged(me.id, True)
    for path in ("/admin", "/admin/users", "/admin/activity", "/admin/requests"):
        assert anon.get(path).status_code == 200, path
    assert "Admin panel" in anon.get("/themes").get_data(as_text=True)
    assert "Privileged" in anon.get("/account").get_data(as_text=True)
    anon.get("/theme?name=cpo&llm=1")
    assert used == [True]  # Claude
    mine = saved_run("admin")
    assert anon.post(f"/theme/saved/{mine}/delete", data={"csrf": CSRF}).status_code == 303
    anon.post(f"/admin/users/{amy.id}/grant-privileged", data={"csrf": CSRF})
    assert users.get(amy.id).is_privileged
    anon.post(f"/admin/users/{amy.id}/disable", data={"csrf": CSRF})
    assert not users.get(amy.id).is_active
    assert users.get(admin.id).is_admin


def test_users_page_lists_everyone_with_roles_and_actions(anon):
    sign_in(anon, "admin", admin=True)
    bob, amy = users.create("bob", "password123"), users.create("amy", "password123")
    users.set_privileged(bob.id, True)
    body = anon.get("/admin/users").get_data(as_text=True)
    assert all(n in body for n in ("admin", "bob", "amy", "Privileged", "Make privileged", "Remove privileges"))
    assert f"/admin/activity?user={amy.id}" in body
    r = anon.post(f"/admin/users/{amy.id}/grant-privileged", data={"csrf": CSRF}, follow_redirects=True)
    assert "amy is now a privileged user" in r.get_data(as_text=True)
    anon.post(f"/admin/users/{amy.id}/revoke-privileged", data={"csrf": CSRF})
    assert not users.get(amy.id).is_privileged


def test_activity_records_what_users_do(anon, monkeypatch):
    monkeypatch.setattr(web, "build_report", lambda sid, *a, **k: SimpleNamespace(html="report"))
    bob = users.create("bob", "password123", is_admin=False)
    token = csrf_of(anon, "/login")
    anon.post("/login", data={"csrf": token, "username": "bob", "password": "wrong-password"})
    anon.post("/login", data={"csrf": token, "username": "typo-secret", "password": "x"})
    anon.post("/login", data={"csrf": token, "username": "bob", "password": "password123"})
    anon.get("/report/2330")
    token = csrf_of(anon, "/themes")
    anon.post("/requests", data={"csrf": token, "topic": "Robots"})
    anon.post("/logout", data={"csrf": token})
    assert [(e.action, e.detail) for e in reversed(activity.recent(bob.id))] == [
        ("sign-in-failed", ""), ("sign-in", ""), ("view-report", "2330"), ("request-topic", "Robots"), ("sign-out", "")]
    assert "typo-secret" not in str(activity.recent())


def test_activity_page_filters_and_pages(anon, monkeypatch):
    sign_in(anon, "admin", admin=True)
    bob, amy = users.create("bob", "password123"), users.create("amy", "password123")
    monkeypatch.setattr(activity, "PAGE_SIZE", 2)
    for i in range(3):
        activity.log(bob, "view-report", f"100{i}")
    activity.log(amy, "sign-in")
    activity.log(users.GUEST, "guest")
    body = anon.get("/admin").get_data(as_text=True)
    assert "Latest activity" in body and "Continued as guest" in body
    body = anon.get(f"/admin/activity?user={bob.id}").get_data(as_text=True)
    assert "1002" in body and "1001" in body and "1000" not in body and "amy</a>" not in body
    older = re.search(r"href='(/admin/activity\?[^']*before=\d+)'", body).group(1)
    assert "1000" in anon.get(older.replace("&amp;", "&")).get_data(as_text=True)
    body = anon.get("/admin/activity?action=guest").get_data(as_text=True)
    assert "Continued as guest" in body and "1002" not in body
    assert "<img" not in anon.get("/admin/activity?action=<img>").get_data(as_text=True)  # unknown action: ignored


def test_deleted_users_history_stays_but_is_not_inherited(anon):
    sign_in(anon, "admin", admin=True)
    bob = users.create("bob", "password123")
    activity.log(bob, "sign-in")
    anon.post(f"/admin/users/{bob.id}/delete", data={"csrf": CSRF})
    again = users.create("bob", "password123")
    assert activity.recent(again.id) == []
    assert "bob (deleted)" in anon.get("/admin/activity").get_data(as_text=True)
