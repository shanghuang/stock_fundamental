import pytest

from conftest import CSRF, sign_in
from tw_fundamental import theme, theme_store, users, web, wishlist
from tw_fundamental.theme import StockProfile, ThemeResult


# --------------------------------------------------------------------------- wishlist.py

def test_submit_validates_and_limits():
    u = users.create("bob", "password123")
    r = wishlist.submit(u, "  Robot   arms ", " humanoids ")
    assert (r.topic, r.note, r.status) == ("Robot arms", "humanoids", "pending")
    with pytest.raises(users.UserError, match="already requested"):
        wishlist.submit(u, "robot ARMS")
    for bad in ("", "x" * 41):
        with pytest.raises(users.UserError):
            wishlist.submit(u, bad)
    for i in range(wishlist.MAX_PENDING - 1):
        wishlist.submit(u, f"topic {i}")
    with pytest.raises(users.UserError, match="at most 5 open requests"):
        wishlist.submit(u, "one too many")


def test_fulfill_covers_same_topic_and_notifies_once():
    bob, amy = users.create("bob", "password123"), users.create("amy", "password123")
    a, b = wishlist.submit(bob, "HBM"), wishlist.submit(amy, "hbm")
    other = wishlist.submit(amy, "CPO")
    assert wishlist.pending_count() == 3
    assert sorted(wishlist.fulfill(a.id, "20261103-120000-abcdef01", "admin")) == sorted([a.id, b.id])
    assert wishlist.fulfill(a.id, "x", "admin") == []  # already decided
    assert wishlist.get(b.id).saved_id == "20261103-120000-abcdef01" and wishlist.get(other.id).status == "pending"
    assert [n.id for n in wishlist.notifications(amy.id)] == [b.id]
    wishlist.mark_seen(amy.id)
    assert wishlist.notifications(amy.id) == [] and wishlist.notifications(bob.id)


def test_decline_and_withdraw():
    bob = users.create("bob", "password123")
    r = wishlist.submit(bob, "Shipping")
    assert wishlist.decline(r.id, "admin", " no listed stocks ") == [r.id]
    assert wishlist.get(r.id).reply == "no listed stocks" and wishlist.notifications(bob.id)
    r2 = wishlist.submit(bob, "Shipping")  # can ask again once decided
    assert not wishlist.withdraw(bob.id + 1, r2.id) and wishlist.withdraw(bob.id, r2.id)
    assert wishlist.get(r2.id) is None


# --------------------------------------------------------------------------- web

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(theme, "analyze", lambda t, news_days=3, use_llm=False, refresh=False, lang="en":
                        ThemeResult(t, [StockProfile("2330", "台積電")], {}, news_days, lang=lang))
    monkeypatch.setattr(web.llm, "available", lambda: True)
    web.app.config["TESTING"] = True
    return web.app.test_client()


def switch(client, u):
    with client.session_transaction() as s:
        s.update(uid=u.id, sv=u.session_version, csrf=CSRF)


def test_request_flow_from_user_to_admin_and_back(client):
    admin = users.create("admin", "password123")
    bob = sign_in(client, "bob", admin=False)
    body = client.get("/themes").get_data(as_text=True)
    assert "Request a topic" in body and "/admin/requests" not in body
    r = client.post("/requests", data={"csrf": CSRF, "topic": "Robots", "note": "humanoid supply chain"},
                    follow_redirects=True)
    assert "Request for “Robots” sent" in r.get_data(as_text=True)
    assert "Waiting for an admin" in client.get("/themes").get_data(as_text=True)
    assert client.get("/admin/requests").status_code == 403

    switch(client, admin)
    body = client.get("/themes").get_data(as_text=True)
    assert "<span class='badge'>1</span>" in body and "Waiting for review: 1." in body
    body = client.get("/admin/requests").get_data(as_text=True)
    assert "Robots" in body and "humanoid supply chain" in body and "name='llm' value='1' checked" in body
    req = wishlist.list_requests(status="pending")[0]
    body = client.get(f"/theme?name=Robots&stocks=2330&llm=1&req={req.id}").get_data(as_text=True)
    assert "Request marked as analyzed. Notified: bob." in body
    rid = wishlist.get(req.id).saved_id
    assert theme_store.get_meta(rid)["owner"] == "admin" and wishlist.pending_count() == 0

    switch(client, bob)
    body = client.get("/").get_data(as_text=True)
    assert "class='notice-pop'" in body and "“Robots”, has been analyzed" in body
    assert client.get("/requests/notices?next=/themes").get_data(as_text=True).count("class='notice-pop'") == 1
    r = client.get(f"/requests/{req.id}/open")
    assert r.headers["Location"].endswith(f"/theme/saved/{rid}")
    assert "class='notice-pop'" not in client.get("/themes").get_data(as_text=True)
    assert client.get("/requests/notices").get_data(as_text=True) == ""


def test_reused_run_also_fulfills(client):
    admin = sign_in(client, "admin", admin=True)
    bob = users.create("bob", "password123")
    client.get("/theme?name=cpo")  # saved run, reused below
    req = wishlist.submit(bob, "CPO")
    body = client.get(f"/theme?name=cpo&req={req.id}").get_data(as_text=True)
    assert "Showing the saved run" in body and "Notified: bob" in body
    assert wishlist.get(req.id).status == "done" and admin


def test_non_admin_cannot_fulfill_or_decline(client):
    users.create("admin", "password123")
    bob = sign_in(client, "bob", admin=False)
    req = wishlist.submit(bob, "CPO")
    client.get(f"/theme?name=cpo&req={req.id}")
    assert wishlist.get(req.id).status == "pending"
    assert client.post(f"/admin/requests/{req.id}/decline", data={"csrf": CSRF}).status_code == 403


def test_decline_notifies_with_reason_and_dismiss(client):
    admin = sign_in(client, "admin", admin=True)
    bob = users.create("bob", "password123")
    req = wishlist.submit(bob, "Shipping")
    r = client.post(f"/admin/requests/{req.id}/decline", data={"csrf": CSRF, "reply": "Too few listed stocks"},
                    follow_redirects=True)
    assert "Declined “Shipping”" in r.get_data(as_text=True) and "Recently decided" in r.get_data(as_text=True)
    r = client.post(f"/admin/requests/{req.id}/decline", data={"csrf": CSRF}, follow_redirects=True)
    assert "already decided" in r.get_data(as_text=True)

    switch(client, bob)
    body = client.get("/themes").get_data(as_text=True)
    assert "was declined" in body and "Too few listed stocks" in body
    r = client.post("/requests/seen", data={"csrf": CSRF, "next": "/themes"})
    assert r.status_code == 303 and r.headers["Location"] == "/themes"
    assert "class='notice-pop'" not in client.get("/themes").get_data(as_text=True) and admin


def test_users_only_open_their_own_requests(client):
    users.create("admin", "password123")
    amy = users.create("amy", "password123")
    req = wishlist.submit(amy, "CPO")
    sign_in(client, "bob", admin=False)
    assert client.get(f"/requests/{req.id}/open").status_code == 404
    assert client.post(f"/requests/{req.id}/withdraw", data={"csrf": CSRF}).status_code == 303
    assert wishlist.get(req.id) is not None


def test_any_admin_run_of_the_topic_fulfills_it(client):
    admin = sign_in(client, "admin", admin=True)
    bob = users.create("bob", "password123")
    dram, other = wishlist.submit(bob, "DRAM"), wishlist.submit(bob, "Shipping")
    body = client.get("/theme?name=memory").get_data(as_text=True)  # the preset card, no ?req=
    assert "Notified: bob" in body
    assert wishlist.get(dram.id).status == "done" and wishlist.get(other.id).status == "pending"
    wishlist.submit(bob, "Robots")
    client.get("/theme?name=robots&stocks=2330")  # ad-hoc themes match by name
    assert [r.topic for r in wishlist.list_requests(status="pending")] == ["Shipping"] and admin


def test_decided_requests_leave_the_users_list(client):
    admin = users.create("admin", "password123")
    bob = sign_in(client, "bob", admin=False)
    done, waiting = wishlist.submit(bob, "CPO"), wishlist.submit(bob, "HBM tools")
    wishlist.fulfill(done.id, "20261103-120000-abcdef01", admin.username)
    body = client.get("/themes").get_data(as_text=True)
    mine = body[body.index("id='requests-mine'"):]
    assert "HBM tools" in mine and "<b>CPO</b>" not in mine  # gone from the list ...
    assert "“CPO”, has been analyzed" in body  # ... and announced in the pop-up instead
    wishlist.fulfill(waiting.id, "20261103-120000-abcdef02", admin.username)
    assert "id='requests-mine'" not in client.get("/themes").get_data(as_text=True)
