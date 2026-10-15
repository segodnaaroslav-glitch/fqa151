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
    assert sale == {"sale_id": "s1", "item_id": "i1", "product_id": None, "price": 12.5, "status": "active"}
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


def test_redact():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl"
    text = m.redact(f'Bearer abc {jwt} {{"refresh_token": "opaque", "name": "x"}}')
    assert "abc" not in text and jwt not in text and "opaque" not in text and '"name": "x"' in text


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("OK", name)
