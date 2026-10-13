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
    for body in (old, new):
        items = [m.norm_item(x) for x in m.extract_list(body, "items")]
        assert items == [{"item_id": "i1", "product_id": "P1", "name": "Knife", "status": "on_sale"}]
    sale = m.norm_sale({"id": "s1", "item": {"id": "i1"}, "price": {"amount": "12,50"}})
    assert sale == {"sale_id": "s1", "item_id": "i1", "price": 12.5}
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


def test_redact():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl"
    text = m.redact(f'Bearer abc {jwt} {{"refresh_token": "opaque", "name": "x"}}')
    assert "abc" not in text and jwt not in text and "opaque" not in text and '"name": "x"' in text


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("OK", name)
