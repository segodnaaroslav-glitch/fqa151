"""Проверки разбора ответов и расчёта цены. Запуск: python tests/test_units.py"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mm2_pricer as m  # noqa: E402

CFG = m.DEFAULT_CONFIG


def test_target_price():
    assert m.target_price(14.00, CFG) == 13.99
    assert m.target_price(2.01, CFG) == 2.00
    assert m.target_price(2.00, CFG) == 2.00
    assert m.target_price(1.50, CFG) == 2.00
    assert m.target_price(0.3 + 0.6, dict(CFG, min_listing_price=0)) == 0.89


def test_find_tokens_old_format():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl"
    storage = {"local": {"auth": json.dumps({"data": json.dumps({"accessToken": jwt,
                                                                   "refreshToken": jwt + "x"})})}}
    assert m.find_tokens(storage, []) == [jwt]


def test_find_tokens_cookie_and_session():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIyIn0.c2ln"
    found = m.find_tokens({"session": {"x": "Bearer " + jwt}}, [{"name": "access_token", "value": "opaque-token-1234567890"}])
    assert found == [jwt, "opaque-token-1234567890"]


def test_extract_and_normalize():
    old = {"items": [{"item_id": "i1", "status": "On_Sale", "product": {"product_id": "P1", "name": "Knife"}}]}
    new = {"data": {"items": [{"id": "i1", "status": "on_sale", "product": {"id": "P1", "title": "Knife"}}]}}
    keys = ("item_id", "product_id", "name", "status", "blocked")
    for body in (old, new):
        items = [m.norm_item(x) for x in m.extract_list(body, "items")]
        assert [{k: i[k] for k in keys} for i in items] == [
            {"item_id": "i1", "product_id": "P1", "name": "Knife", "status": "on_sale", "blocked": False}]
    assert m.norm_item({"id": 1, "product_id": 2, "tradable": False})["blocked"] is True
    sale = m.norm_sale({"id": "s1", "item": {"id": "i1"}, "price": {"amount": "12,50"}, "status": "Active"})
    assert sale == {"sale_id": "s1", "item_id": "i1", "product_id": None, "price": 12.5,
                    "status": "active", "user_id": None}
    assert m.sale_active(sale)
    assert not m.sale_active(dict(sale, status="sold"))
    assert m.extract_list({"error": "x"}, "items") is None


def test_profile_name():
    assert m.profile_name({"user_id": 1, "username": "bob"}) == "bob"
    assert m.profile_name({"data": {"user": {"id": 2, "nickname": "amy"}}}) == "amy"
    assert m.profile_name({"error": "unauthorized"}) is None


def test_endpoint_alternatives():
    url = "https://mm2.dreampets.gg/api/sales/v1/sales/best"
    known = ["/api/sales/v2/sales/best", "/api/sales/v3", "https://evil.example/api/sales/v9/sales/best",
             "https://api.dreampets.gg/api/sales/v2/sales/best?x=1", "/api/items/v2/items/user"]
    assert m.endpoint_alternatives(url, known, "dreampets.gg") == [
        "https://mm2.dreampets.gg/api/sales/v2/sales/best",
        "https://api.dreampets.gg/api/sales/v2/sales/best",
        "https://mm2.dreampets.gg/api/sales/v3/sales/best",
    ]
    assert m.endpoint_alternatives("https://mm2.dreampets.gg/api/sales/v1/sales/",
                                   ["/api/sales/v2/sales"], "dreampets.gg") == [
        "https://mm2.dreampets.gg/api/sales/v2/sales/"]


def test_status_blocked():
    cfg = m.DEFAULT_CONFIG
    for st in ("in_inventory", "on_sale", "available", "inventory", "", "tradable", "active"):
        assert not m.status_blocked(st, cfg), st
    for st in ("sold", "withdrawn", "trade_lock", "pending_withdrawal", "on_hold", "locked", "reserved"):
        assert m.status_blocked(st, cfg), st
    assert m.looks_listed("on_sale") and m.looks_listed("listed") and not m.looks_listed("available")


def test_expand_items():
    product = {"id": "P1", "title": "Candy"}
    groups = [{"product": product, "count": 2, "items": [{"id": "a"}, {"id": "b", "status": "x"}],
               "status": "available"},
              {"product": {"id": "P2"}, "item_ids": ["c"]},
              {"id": "d", "product": {"id": "P3"}}]
    got = [m.norm_item(x) for x in m.expand_items(groups)]
    assert [(i["item_id"], i["product_id"], i["status"]) for i in got] == [
        ("a", "P1", "available"), ("b", "P1", "x"), ("c", "P2", ""), ("d", "P3", "")]


def test_pagination_helpers():
    assert m.page_hints({"items": [1, 2, 3], "total": 9}, 3)["more"] is True
    assert m.page_hints({"items": [1], "has_more": False}, 1)["more"] is False
    assert m.page_hints({"data": {"items": [], "next": "/api/x?page=2"}}, 0)["next"] == "/api/x?page=2"
    assert m.page_hints({"items": [1]}, 1)["more"] is None
    assert m.add_params("https://a/b?page=1&limit=50", {"page": 2}) == "https://a/b?page=2&limit=50"
    assert m.add_params("https://a/b", None) == "https://a/b"


def test_missing_fields_and_find_field():
    fastapi = {"detail": [{"loc": ["body", "items", 0, "account_id"], "type": "missing", "msg": "Field required"},
                          {"loc": ["body", "items", 0, "price"], "type": "greater_than", "msg": "too low"}]}
    assert m.missing_fields(fastapi) == ["account_id"]
    assert m.missing_fields({"message": "robloxAccountId is required"}) == ["robloxAccountId"]
    assert m.missing_fields({"errors": {"items.0.account_id": ["required"]}}) == ["account_id"]
    raw = {"id": "i1", "account": {"id": "acc1"}, "meta": {"roblox_account_id": 77}}
    assert m.find_field(raw, "account_id") == "acc1"
    assert m.find_field(raw, "robloxAccountId") == 77
    assert m.find_field(raw, "nothing") is None


def _cap(seq, method, url, status=200, body=None, post=None):
    return {"seq": seq, "method": method, "url": url, "status": status,
            "body": json.dumps(body) if body is not None else "", "post": post}


def test_learn_market_ops():
    # формат — как в настоящем логе нового маркета
    uid = "95071cd0-u"
    item = m.norm_item({"id": "it-1", "user_id": uid, "account_id": "acc-1", "product_id": "pr-1",
                        "product": {"id": "pr-1", "name": "Bauble"}})
    base = "https://mm2-test.dreampets.gg/api/market/v1"
    caps = [
        _cap(1, "GET", f"{base}/market/products?currency=rub", body={"products": [{"product_id": "pr-9", "name": "X"}]}),
        _cap(2, "GET", f"{base}/market/products/pr-9/sales?currency=rub&sort=price_asc",
             body={"sales": [{"id": "s-a", "user_id": "other", "item_id": "x1", "price": "10.50"}]}),
        _cap(3, "POST", f"{base}/sales", status=201,
             body={"id": "sale-new", "item_id": "it-1", "price": "99.99"},
             post=json.dumps({"account_id": "acc-1", "item_id": "it-1", "price": "99.99", "currency": "rub"})),
        _cap(4, "GET", f"{base}/users/{uid}/sales",
             body={"sales": [{"id": "sale-new", "user_id": uid, "item_id": "it-1", "price": "99.99"}]}),
        _cap(5, "DELETE", f"{base}/sales/sale-new", status=204),
    ]
    ops = m.learn_market_ops(caps, {"it-1": item}, {"pr-1", "pr-9"}, uid)
    assert ops["best"]["url"] == f"{base}/market/products/{{{{product_id}}}}/sales?currency=rub&sort=price_asc"
    assert ops["create"]["url"] == f"{base}/sales"
    assert ops["create"]["body"] == {"account_id": "{{account_id}}", "item_id": "{{item_id}}",
                                     "price": "{{price}}", "currency": "rub"}
    assert ops["create"]["price_type"] == "str" and ops["create"]["price_scale"] == 1
    assert ops["cancel"]["url"] == f"{base}/sales/{{{{sale_id}}}}" and ops["cancel"]["method"] == "DELETE"
    # повтор по образцу для другого предмета и цены
    other = m.norm_item({"id": "it-2", "account_id": "acc-2", "product_id": "pr-2", "product": {"id": "pr-2"}})
    nm = m.NewMarket.__new__(m.NewMarket)
    nm.api = type("A", (), {"user_id": uid})()
    nm.ops, nm.cfg = ops, m.DEFAULT_CONFIG
    vals = nm._values(item=other, price=13.99)
    assert m._fill(ops["create"]["body"], vals) == {"account_id": "acc-2", "item_id": "it-2",
                                                     "price": "13.99", "currency": "rub"}
    assert m._fill(ops["cancel"]["url"], {"sale_id": "zz"}) == f"{base}/sales/zz"


def test_learn_rejects_bulk_listing():
    a = m.norm_item({"id": "it-1", "product_id": "pr-1", "product": {"id": "pr-1"}})
    b = m.norm_item({"id": "it-2", "product_id": "pr-1", "product": {"id": "pr-1"}})
    caps = [_cap(1, "POST", "https://x.dreampets.gg/api/market/v1/sales", 201, body={"ok": True},
                 post=json.dumps({"items": [{"item_id": "it-1", "price": "5"}, {"item_id": "it-2", "price": "5"}]}))]
    assert "create" not in m.learn_market_ops(caps, {"it-1": a, "it-2": b}, set(), "u")


def test_learn_price_in_kopecks():
    item = m.norm_item({"id": "it-1", "product_id": "pr-1", "product": {"id": "pr-1"}})
    caps = [_cap(1, "POST", "https://x.dreampets.gg/api/market/v1/sales", 201,
                 body={"ok": True}, post=json.dumps({"item_id": "it-1", "price": 9999})),
            _cap(2, "GET", "https://x.dreampets.gg/api/market/v1/users/u/sales",
                 body={"sales": [{"id": "s1", "item_id": "it-1", "price": "99.99"}]})]
    ops = m.learn_market_ops(caps, {"it-1": item}, set(), "u")
    assert ops["create"]["price_scale"] == 100 and ops["create"]["price_type"] == "num"
    nm = m.NewMarket.__new__(m.NewMarket)
    nm.api = type("A", (), {"user_id": "u"})()
    nm.ops, nm.cfg = ops, m.DEFAULT_CONFIG
    assert nm._values(price=13.99)["price"] == 1399


def test_apply_floor():
    cfg = dict(m.DEFAULT_CONFIG, max_drop_percent=20)
    floors = {}
    assert m.apply_floor(23.99, "k", floors, cfg) == (23.99, "")          # первая цена — точка отсчёта
    assert floors["k"] == 19.20
    assert m.apply_floor(21.00, "k", floors, cfg) == (21.00, "")          # в пределах 20% — следуем рынку
    price, note = m.apply_floor(14.99, "k", floors, cfg)                  # обвал — держим границу
    assert price == 19.20 and "19.20" in note
    assert m.apply_floor(30.00, "k", floors, cfg) == (30.00, "")          # вверх — без ограничений
    assert m.apply_floor(1.0, "k", None, cfg) == (1.0, "")                # разовый запуск — без границы
    assert m.apply_floor(1.0, "z", {}, dict(cfg, max_drop_percent=-1)) == (1.0, "")


def test_redact():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl"
    text = m.redact(f'Bearer abc {jwt} {{"refresh_token": "opaque", "name": "x", "email": "a.b@mail.ru"}}')
    assert "abc" not in text and jwt not in text and "opaque" not in text and '"name": "x"' in text
    assert "a.b@mail.ru" not in text


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("OK", name)
