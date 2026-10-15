"""Локальная имитация dreampets.gg для проверки MM2 Pricer без настоящего сайта.

variant="old":  как было до обновления — токен в localStorage['auth'], API v1,
                «плоские» ответы с item_id/product_id/sale_id, статус in_inventory.
variant="new":  «обновлённый» сайт — токен только в памяти страницы (берётся по
                httpOnly-куке), обязательный заголовок X-Client-Version, API v2,
                ответы обёрнуты в {"data": ...}, поля id/title, статус available,
                список предметов отдаётся по 3 штуки на страницу, при выставлении
                обязателен account_id (иначе 422 как у FastAPI).
variant="new2": как "new", но старый адрес предметов отдаёт только выставленные,
                а инвентарь (сгруппированный «товар ×N») сайт берёт с другого адреса —
                и только на странице «Продажа».
variant="market": как на настоящем сайте после обновления — два маркета.
                Старый (api.*, API v1) и новый (mkt.*: inventory/v1 и market/v1, как в
                логе с dreampets.gg). Адреса выставления/снятия и лотов товара на новом
                маркете программе заранее не известны: «пользователь» (код страницы)
                один раз сам открывает товар, выставляет предмет и снимает его.
"""
import json
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

CLIENT_VERSION = "7"


def initial_state(variant):
    inv = "in_inventory" if variant == "old" else "available"
    items = {
        "i1": ("P1", "on_sale"), "i2": ("P1", inv), "i3": ("P2", inv), "i4": ("P3", "on_sale"),
        "i5": ("P4", "on_sale"), "i6": ("P1", "sold"), "i7": ("P2", "trade_lock"),
        "i8": ("P5", inv), "i9": ("P5", inv),
    }
    sales = {
        "s1": {"product": "P1", "item": "i1", "price": 15.00, "mine": True},
        "s4": {"product": "P3", "item": "i4", "price": 9.99, "mine": True},
        "s5": {"product": "P4", "item": "i5", "price": 4.99, "mine": True},
    }
    if variant != "old":
        # история: давно проданный лот — не должен считаться активным
        sales["h1"] = {"product": "P1", "item": "i6", "price": 20.00, "mine": True, "status": "sold"}
    market = {"P1": [10.00, 11.00, 12.00, 13.00, 14.00, 16.00], "P2": [1.50, 1.80],
              "P4": [4.00, 5.00], "P5": [3.00, 3.10, 3.20, 3.30, 3.40, 3.50]}
    n = 0
    for pid, prices in market.items():
        for price in prices:
            n += 1
            sales[f"m{n}"] = {"product": pid, "price": price}
    return {
        "inv_status": inv,
        "products": {"P1": "Knife A", "P2": "Gun B", "P3": "Pet C", "P4": "Gem D", "P5": "Candy"},
        "items": {iid: {"product": pid, "status": st, "account": "acc1"} for iid, (pid, st) in items.items()},
        "sales": sales,
    }


def initial_market_state():
    acc = {"acc1": {"name": "Ssob_indra", "tradable": True}, "acc2": {"name": "bruh", "tradable": False}}
    products = {"N1": "Bauble", "N2": "Bunnies", "N3": "Candy", "N4": "Lonely", "PX": "Demo"}
    inv = {"n1": ("N1", "acc1"), "n2": ("N1", "acc1"), "n3": ("N2", "acc1"), "n4": ("N3", "acc1"),
           "n5": ("N3", "acc2"), "n6": ("N4", "acc1"), "n7": ("N2", "acc1")}
    sales = {"ns3": {"product": "N2", "item": "n3", "price": 50.00, "user": "U1", "account": "acc1"}}
    market = {"N1": [20.00, 21.00, 22.00, 23.00, 24.00, 25.00], "N2": [40.00, 41.00, 42.00, 43.00, 44.00],
              "N3": [5.00, 5.10], "PX": [7.00, 8.00]}
    n = 0
    for pid, prices in market.items():
        for price in prices:
            n += 1
            sales[f"x{n}"] = {"product": pid, "item": f"other{n}", "price": price, "user": "U9"}
    return {"accounts": acc, "products": products,
            "inventory": {k: {"product": v[0], "account": v[1]} for k, v in inv.items()},
            "sales": sales,
            # старый маркет: один предмет, выставлен по 2.00, конкуренты тоже по 2.00
            "old_items": {"o1": {"product": "OLD1", "name": "Starfish", "status": "on_sale"}},
            "old_sales": {"os1": {"item": "o1", "price": 2.0}},
            "old_market": [2.0] * 6}


MARKET_APP_JS = """
const ROOT = location.host.replace(/^www\\./, '');
const API = location.protocol + '//api.' + ROOT, MKT = location.protocol + '//mkt.' + ROOT;
let token = null, uid = null;
async function call(base, path, opts = {}) {
  opts.credentials = 'include';
  opts.headers = Object.assign({}, opts.headers || {});
  if (token) opts.headers.Authorization = 'Bearer ' + token;
  const r = await fetch(base + path, opts);
  return r.status === 204 ? null : r.json();
}
async function ensureLogin() {
  const t = await call(API, '/api/auth/v2/token');
  if (t.token) { token = t.token; return; }
  await new Promise(r => setTimeout(r, 2000));            // «пользователь» входит
  await call(API, '/api/auth/v2/session', {method: 'POST'});
  token = (await call(API, '/api/auth/v2/token')).token;
}
async function runDemo() {                                  // «пользователь» показывает выставление
  await call(MKT, '/api/market/v1/market/products?search=&currency=rub&limit=72&offset=0');
  await call(MKT, '/api/market/v1/market/products/PX/sales?currency=rub&sort=price_asc&limit=20');
  const sale = await call(MKT, '/api/market/v1/sales', {method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({account_id: 'acc1', item_id: 'n6', price: '99.99', currency: 'rub'})});
  await call(MKT, `/api/market/v1/users/${uid}/sales`);
  await call(MKT, '/api/market/v1/sales/' + sale.id, {method: 'DELETE'});
  await call(MKT, `/api/market/v1/users/${uid}/sales`);
}
(async () => {
  await ensureLogin();
  uid = (await call(API, '/api/auth/v2/users/profile')).user_id;
  if (location.pathname.endsWith('/sell')) {
    await call(MKT, `/api/inventory/v1/users/${uid}/items`);
    await call(MKT, `/api/inventory/v1/users/${uid}/account-inventory-gates`);
    await call(MKT, `/api/market/v1/users/${uid}/sales`);
    const demo = await call(MKT, '/mock/demo');
    if (demo.run) setTimeout(runDemo, 4000);
  } else {
    await call(MKT, '/api/market/v1/market/products?search=&currency=rub&sort=popularity_desc&limit=72&offset=0');
  }
})();
"""


OLD_PAGE = """<!doctype html><html><head><title>DreamPets (old)</title></head><body>
<h1>dreampets mock (old)</h1><div id="who">не вошли</div>
<script src="/static/app.js"></script></body></html>"""

OLD_APP_JS = """
const API = location.origin;
// «пользователь» входит через 2 секунды после открытия страницы
setTimeout(async () => {
  const r = await fetch(API + '/api/auth/v1/login', {method: 'POST'});
  const {accessToken} = await r.json();
  localStorage.setItem('auth', JSON.stringify({data: JSON.stringify({accessToken}), v: 1}));
  const p = await (await fetch(API + '/api/auth/v1/users/profile',
      {headers: {Authorization: 'Bearer ' + accessToken}})).json();
  document.getElementById('who').textContent = p.username;
}, 2000);
"""

NEW_PAGE = """<!doctype html><html><head><title>DreamPets (new)</title></head><body>
<nav><a href="/mm2">Маркет</a> <a href="/mm2/items">Предметы</a> <a href="/mm2/sell">Продажа</a></nav>
<h1>dreampets mock (new)</h1><div id="who">не вошли</div><div id="inv"></div>
<script src="/static/app.js"></script></body></html>"""

NEW_APP_JS = """
const VARIANT = '%s';
const API = location.protocol + '//api.' + location.host.replace(/^www\\./, '');
const ROUTES = {
  session: '/api/auth/v2/session', token: '/api/auth/v2/token',
  profile: '/api/auth/v2/users/profile', items: '/api/items/v2/items/user',
  inventory: '/api/items/v2/accounts/items',
  sales: '/api/sales/v2/sales/', mySales: '/api/sales/v2/sales/user',
  best: '/api/sales/v2/sales/best',
};
let token = null;   // токен живёт только в памяти страницы
async function call(path, opts = {}) {
  opts.credentials = 'include';
  opts.headers = Object.assign({'X-Client-Version': '%s'}, opts.headers || {});
  if (token) opts.headers.Authorization = 'Bearer ' + token;
  return (await fetch(API + path, opts)).json();
}
async function ensureLogin() {
  const t = await call(ROUTES.token);                     // уже есть сессия (кука)?
  if (t.token) { token = t.token; return; }
  await new Promise(r => setTimeout(r, 2000));            // «пользователь» входит
  await call(ROUTES.session, {method: 'POST'});           // ставит httpOnly-куку
  token = (await call(ROUTES.token)).token;
}
(async () => {
  await ensureLogin();
  const p = await call(ROUTES.profile);
  document.getElementById('who').textContent = p.data.user.username;
  if (location.pathname.endsWith('/sell')) {
    const inv = await call(VARIANT === 'new2' ? ROUTES.inventory : ROUTES.items + '?page=1&limit=50');
    document.getElementById('inv').textContent = JSON.stringify(inv).length + ' bytes';
  }
  setInterval(() => call(ROUTES.profile), 5000);
})();
"""


class MockSite:
    def __init__(self, variant: str, host: str = "127.0.0.1", demo: bool = True):
        self.variant = variant
        self.state = initial_market_state() if variant == "market" else initial_state(variant)
        self.demo = demo
        self.lock = threading.Lock()
        self.tokens = set()
        self.sessions = {}
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_OPTIONS(self):
                self._send(204, None)

            def do_GET(self):
                site.handle(self, "GET")

            def do_POST(self):
                site.handle(self, "POST")

            def do_DELETE(self):
                site.handle(self, "DELETE")

            def _send(self, code, body, ctype="application/json", headers=None):
                data = b""
                if body is not None:
                    data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
                self.send_response(code)
                origin = self.headers.get("Origin")
                if origin:
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Access-Control-Allow-Credentials", "true")
                    self.send_header("Access-Control-Allow-Headers",
                                     "Authorization, Content-Type, X-Client-Version")
                    self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer((host, 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()

    # ---------- маршрутизация ----------

    def handle(self, h, method):
        u = urlparse(h.path)
        path, q = u.path, {k: v[0] for k, v in parse_qs(u.query).items()}
        length = int(h.headers.get("Content-Length") or 0)
        body = json.loads(h.rfile.read(length) or b"null") if length else None
        if path in ("/mm2", "/mm2/sell", "/mm2/items"):
            return h._send(200, OLD_PAGE if self.variant == "old" else NEW_PAGE, "text/html; charset=utf-8")
        if path == "/static/app.js":
            if self.variant == "market":
                js = MARKET_APP_JS
            else:
                js = OLD_APP_JS if self.variant == "old" else NEW_APP_JS % (self.variant, CLIENT_VERSION)
            return h._send(200, js, "application/javascript")
        if self.variant == "market":
            return self.market_api(h, method, path, q, body)
        if self.variant == "old":
            return self.old_api(h, method, path, q, body)
        return self.new_api(h, method, path, q, body)

    def _bearer(self, h):
        auth = h.headers.get("Authorization", "")
        return auth[7:] if auth.startswith("Bearer ") else None

    def _new_token(self):
        tok = "eyJhbGciOiJIUzI1NiJ9." + secrets.token_urlsafe(16) + "." + secrets.token_urlsafe(8)
        self.tokens.add(tok)
        return tok

    # ---------- старое API ----------

    def old_api(self, h, method, path, q, body):
        if path == "/api/auth/v1/login" and method == "POST":
            return h._send(200, {"accessToken": self._new_token()})
        if self._bearer(h) not in self.tokens:
            return h._send(401, {"error": "unauthorized"})
        if path == "/api/auth/v1/users/profile":
            return h._send(200, {"user_id": 42, "username": "tester"})
        if path == "/api/items/v1/items/user":
            return h._send(200, {"items": [self.item_view(i, old=True) for i in self.state["items"]]})
        if path == "/api/sales/v1/sales/user":
            return h._send(200, {"sales": self.my_sales_view(old=True)})
        if path == "/api/sales/v1/sales/best":
            return h._send(200, {"sales": self.best_view(q, old=True)})
        if path == "/api/sales/v1/sales/" and method == "POST":
            return self.create(h, body)
        if path == "/api/sales/v1/sales/" and method == "DELETE":
            return self.cancel(h, body)
        return h._send(404, {"error": "not found"})

    # ---------- новое API ----------

    def new_api(self, h, method, path, q, body):
        if not path.startswith("/api/") or not h.headers.get("Host", "").startswith("api."):
            return h._send(404, {"error": "not found"})
        if "/v1/" in path:
            return h._send(404, {"error": "not found"})
        if h.headers.get("X-Client-Version") != CLIENT_VERSION:
            return h._send(403, {"error": "client outdated"})
        if path == "/api/auth/v2/session" and method == "POST":
            sid = secrets.token_urlsafe(12)
            self.sessions[sid] = True
            return h._send(200, {"ok": True},
                           headers={"Set-Cookie": f"sid={sid}; Path=/; HttpOnly; SameSite=Lax"})
        if path == "/api/auth/v2/token":
            cookie = h.headers.get("Cookie", "")
            sid = dict(p.strip().split("=", 1) for p in cookie.split(";") if "=" in p).get("sid")
            if sid not in self.sessions:
                return h._send(401, {"error": "no session"})
            return h._send(200, {"token": self._new_token()})
        if self._bearer(h) not in self.tokens:
            return h._send(401, {"error": "unauthorized"})
        if path == "/api/auth/v2/users/profile":
            return h._send(200, {"data": {"user": {"id": 42, "username": "tester",
                                                   "accounts": [{"id": "acc1", "name": "bruh"}]}}})
        if path == "/api/items/v2/items/user":
            ids = list(self.state["items"])
            if self.variant == "new2":   # старый адрес: только выставленные
                ids = [i for i in ids if self.state["items"][i]["status"] == "on_sale"]
                return h._send(200, {"data": {"items": [self.item_view(i, old=False) for i in ids]}})
            limit, page = int(q.get("limit", 3)), int(q.get("page", 1))
            if limit > 50:
                return h._send(422, {"detail": [{"loc": ["query", "limit"], "type": "less_than_equal",
                                                 "msg": "Input should be less than or equal to 50"}]})
            chunk = ids[(page - 1) * limit: page * limit]
            return h._send(200, {"data": {"items": [self.item_view(i, old=False) for i in chunk],
                                          "total": len(ids), "page": page, "limit": limit}})
        if path == "/api/items/v2/accounts/items":
            groups = {}
            for iid, it in self.state["items"].items():
                if it["status"] == "on_sale":
                    continue
                g = groups.setdefault(it["product"], {
                    "product": {"id": it["product"], "title": self.state["products"][it["product"]]},
                    "count": 0, "items": []})
                g["count"] += 1
                g["items"].append({"id": iid, "status": it["status"],
                                   "account": {"id": it["account"], "name": "bruh"}})
            return h._send(200, {"data": {"groups": list(groups.values())}})
        if path == "/api/sales/v2/sales/user":
            return h._send(200, {"data": {"sales": self.my_sales_view(old=False)}})
        if path == "/api/sales/v2/sales/best":
            return h._send(200, {"data": {"sales": self.best_view(q, old=False)}})
        if path == "/api/sales/v2/sales/" and method == "POST":
            return self.create(h, body)
        if path == "/api/sales/v2/sales/" and method == "DELETE":
            return self.cancel(h, body)
        return h._send(404, {"error": "not found"})

    # ---------- данные ----------

    def item_view(self, iid, old):
        st = self.state
        it = st["items"][iid]
        pid = it["product"]
        if old:
            return {"item_id": iid, "status": it["status"],
                    "product": {"product_id": pid, "name": st["products"][pid]}}
        return {"id": iid, "status": it["status"], "account": {"id": it["account"], "name": "bruh"},
                "product": {"id": pid, "title": st["products"][pid]}}

    def _sale_view(self, sid, s, old):
        if old:
            return {"sale_id": sid, "item_id": s.get("item"), "product_id": s["product"],
                    "price": s["price"]}
        return {"id": sid, "item": {"id": s.get("item")}, "productId": s["product"],
                "price": f"{s['price']:.2f}", "status": s.get("status", "active")}

    def my_sales_view(self, old):
        return [self._sale_view(sid, s, old) for sid, s in self.state["sales"].items()
                if s.get("mine") and (not old or s.get("status") != "sold")]

    def best_view(self, q, old):
        pid, count = q.get("product_id"), int(q.get("count", 5))
        rows = sorted(((sid, s) for sid, s in self.state["sales"].items()
                       if s["product"] == pid and s.get("status") != "sold"),
                      key=lambda x: x[1]["price"])[:count]
        return [self._sale_view(sid, s, old) for sid, s in rows]

    def create(self, h, body):
        with self.lock:
            for it in body["items"]:
                item = self.state["items"].get(it["item_id"])
                if self.variant != "old" and "account_id" not in it:
                    return h._send(422, {"detail": [{"loc": ["body", "items", 0, "account_id"],
                                                     "type": "missing", "msg": "Field required"}]})
                if not item or item["status"] != self.state["inv_status"]:
                    return h._send(400, {"error": f"item {it['item_id']} not available"})
                if item["product"] != it["product_id"]:
                    return h._send(400, {"error": "product mismatch"})
                if self.variant != "old" and it["account_id"] != item["account"]:
                    return h._send(400, {"error": "wrong account"})
                sid = "n" + secrets.token_hex(3)
                self.state["sales"][sid] = {"product": item["product"], "item": it["item_id"],
                                            "price": float(it["price"]), "mine": True}
                item["status"] = "on_sale"
        return h._send(201, {"ok": True})

    def cancel(self, h, body):
        with self.lock:
            for sid in body["sale_ids"]:
                s = self.state["sales"].get(sid)
                if not s or not s.get("mine") or s.get("status") == "sold":
                    return h._send(400, {"error": f"sale {sid} not yours"})
                del self.state["sales"][sid]
                self.state["items"][s["item"]]["status"] = self.state["inv_status"]
        return h._send(204, None)

    # ---------- вариант market: два маркета ----------

    def market_api(self, h, method, path, q, body):
        host = h.headers.get("Host", "")
        st = self.state
        if host.startswith("api."):
            if path == "/api/auth/v2/session" and method == "POST":
                sid = secrets.token_urlsafe(12)
                self.sessions[sid] = True
                return h._send(200, {"ok": True},
                               headers={"Set-Cookie": f"sid={sid}; Path=/; HttpOnly; SameSite=Lax"})
            if path == "/api/auth/v2/token":
                cookie = h.headers.get("Cookie", "")
                sid = dict(p.strip().split("=", 1) for p in cookie.split(";") if "=" in p).get("sid")
                if sid not in self.sessions:
                    return h._send(401, {"error": "no session"})
                return h._send(200, {"token": self._new_token()})
            if self._bearer(h) not in self.tokens:
                return h._send(403, {"detail": "Not authenticated"})
            if path == "/api/auth/v2/users/profile":
                return h._send(200, {"user_id": "U1", "username": "tester", "email": "t.e.s.t@mail.ru"})
            if path == "/api/items/v1/items/user":
                return h._send(200, {"items": [
                    {"item_id": iid, "status": it["status"],
                     "product": {"product_id": it["product"], "name": it["name"]}}
                    for iid, it in st["old_items"].items()]})
            if path == "/api/sales/v1/sales/user":
                return h._send(200, {"sales": [{"sale_id": sid, "item_id": x["item"], "price": x["price"]}
                                               for sid, x in st["old_sales"].items()]})
            if path == "/api/sales/v1/sales/best":
                if q.get("product_id") != "OLD1":
                    return h._send(400, {"detail": "The product does not exist."})
                rows = [{"sale_id": f"om{i}", "price": p} for i, p in enumerate(st["old_market"])]
                rows += [{"sale_id": sid, "price": x["price"]} for sid, x in st["old_sales"].items()]
                rows.sort(key=lambda r: r["price"])
                return h._send(200, {"sales": rows[:int(q.get("count", 5))]})
            return h._send(404, {"detail": "Not Found"})

        if not host.startswith("mkt."):
            return h._send(404, {"detail": "Not Found"})
        if path == "/mock/demo":
            run = self.demo
            self.demo = False
            return h._send(200, {"run": run})
        if self._bearer(h) not in self.tokens:
            return h._send(403, {"detail": "Not authenticated"})
        on_sale = {x["item"] for x in st["sales"].values() if x.get("user") == "U1"}
        if path == "/api/inventory/v1/users/U1/items":
            return h._send(200, {"items": [
                {"id": iid, "user_id": "U1", "account_id": it["account"], "product_id": it["product"],
                 "product": {"id": it["product"], "name": st["products"][it["product"]]}}
                for iid, it in st["inventory"].items()]})
        if path == "/api/inventory/v1/users/U1/account-inventory-gates":
            return h._send(200, {"gates": [
                {"account_id": aid, "account": {"roblox_display_name": a["name"]}, "tradable": a["tradable"]}
                for aid, a in st["accounts"].items()]})
        if path == "/api/market/v1/users/U1/sales":
            return h._send(200, {"sales": [self._mkt_sale(sid, x) for sid, x in st["sales"].items()
                                           if x.get("user") == "U1"]})
        if path == "/api/market/v1/market/products":
            return h._send(200, {"products": [{"product_id": pid, "name": n} for pid, n in st["products"].items()]})
        m = re.fullmatch(r"/api/market/v1/market/products/([^/]+)/sales", path)
        if m and method == "GET":
            if m.group(1) not in st["products"]:
                return h._send(404, {"detail": "Product not found"})
            rows = sorted((self._mkt_sale(sid, x) for sid, x in st["sales"].items()
                           if x["product"] == m.group(1)), key=lambda r: float(r["price"]))
            return h._send(200, {"sales": rows[:int(q.get("limit", 20))], "total": len(rows)})
        if path == "/api/market/v1/sales" and method == "POST":
            with self.lock:
                it = st["inventory"].get(body.get("item_id"))
                if not it or body.get("account_id") != it["account"]:
                    return h._send(400, {"detail": "Item not found"})
                if not st["accounts"][it["account"]]["tradable"]:
                    return h._send(400, {"detail": "Account is frozen"})
                if body["item_id"] in on_sale:
                    return h._send(409, {"detail": "Item is already on sale"})
                if not isinstance(body.get("price"), str):
                    return h._send(422, {"detail": [{"loc": ["body", "price"], "type": "string_type"}]})
                sid = "ms" + secrets.token_hex(3)
                st["sales"][sid] = {"product": it["product"], "item": body["item_id"],
                                    "price": float(body["price"]), "user": "U1", "account": it["account"]}
            return h._send(201, self._mkt_sale(sid, st["sales"][sid]))
        m = re.fullmatch(r"/api/market/v1/sales/([^/]+)", path)
        if m and method == "DELETE":
            with self.lock:
                x = st["sales"].get(m.group(1))
                if not x or x.get("user") != "U1":
                    return h._send(404, {"detail": "Sale not found"})
                del st["sales"][m.group(1)]
            return h._send(204, None)
        return h._send(404, {"detail": "Not Found"})

    @staticmethod
    def _mkt_sale(sid, x):
        return {"id": sid, "user_id": x.get("user"), "account_id": x.get("account"), "item_id": x["item"],
                "product_id": x["product"], "price": f"{x['price']:.2f}", "currency": "rub",
                "status": "available"}

    def my_prices(self):
        """item_id -> цена активного лота пользователя (для проверки результата)."""
        if self.variant == "market":
            st = self.state
            out = {x["item"]: x["price"] for x in st["old_sales"].values()}
            out.update({x["item"]: x["price"] for x in st["sales"].values() if x.get("user") == "U1"})
            return out
        return {s["item"]: s["price"] for s in self.state["sales"].values()
                if s.get("mine") and s.get("status") != "sold"}
