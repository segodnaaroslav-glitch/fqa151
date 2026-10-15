"""
MM2 Pricer v2 — разовый репрайсер для dreampets.gg (версия для одного запуска).

Что делает при запуске:
  1) Поднимает Chrome (или Edge) на ВРЕМЕННОМ, одноразовом профиле (ваши
     куки/сессии не используются и не сохраняются).
  2) Открывает окно — вы входите в свой аккаунт вручную.
  3) Сканирует ВЕСЬ инвентарь (все страницы списка, плюс то, что показывает
     сайт на странице «Продажа») и делает ОДИН проход по товарам:
       а) считывает цены на рынке (топ-5 самых дешёвых, без ваших лотов);
       б) снимает все ваши активные лоты этого товара;
       в) выставляет все ваши штуки по цене 5-го лота топа (самого дорогого
          из топ-5) − 1 копейка.
  4) Разлогинивается (полностью удаляет временный профиль) и закрывается.

Никакого цикла: один запуск = одна смена цен.

Что нового в v2.1:
  * Предмет выставляется, если его статус не означает «продан / выведен /
    заблокирован» — переименование статусов на сайте больше не мешает.
  * Списки предметов и лотов читаются целиком, даже если сайт отдаёт их
    постранично.
  * Программа открывает страницу «Продажа» и подсматривает, откуда сайт сам
    берёт ваш инвентарь — так находятся предметы, которых нет в старом API.
  * Если сайт при выставлении требует новое поле, программа берёт его из
    данных предмета и повторяет запрос.

Что нового в v2 (чтобы обновления сайта не ломали программу):
  * Токен ищется не только в localStorage['auth']: программа подсматривает,
    с каким заголовком Authorization сайт сам ходит в своё API, и перебирает
    localStorage / sessionStorage / куки.
  * Запросы к API идут изнутри открытой вкладки (fetch с куками сайта),
    а если так не получается — напрямую, с теми же заголовками и куками.
  * Ответы API разбираются терпимо к переименованным полям и обёрткам.
  * Если адрес API отвечает 404, программа ищет новый адрес (версию API)
    в коде сайта и в запросах, которые делает сам сайт.
  * Все адреса и настройки можно переопределить в mm2pricer_config.json
    рядом с exe, без пересборки.
  * Подробный лог пишется в mm2pricer_log.txt рядом с exe (токены вырезаются).
  * Тестовый режим без изменения цен: флаг --dry-run или "dry_run": true.
"""

import os
import re
import sys
import json
import time
import base64
import shutil
import socket
import argparse
import tempfile
import threading
import traceback
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, urlencode, unquote, urljoin, parse_qsl, urlunparse

import requests
import websocket


for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


VERSION = "2.1"

ENDPOINT_KEYS = ("items_api", "profile_api", "sales_api", "sales_user_api", "sales_best_api")

DEFAULT_CONFIG = {
    "login_url": "https://dreampets.gg/mm2",
    # домен сайта: запросы на него и его поддомены считаются запросами сайта
    "site_domain": "dreampets.gg",

    "items_api": "https://mm2.dreampets.gg/api/items/v1/items/user",
    "profile_api": "https://auth.dreampets.gg/api/auth/v1/users/profile",
    "sales_api": "https://mm2.dreampets.gg/api/sales/v1/sales/",
    "sales_user_api": "https://mm2.dreampets.gg/api/sales/v1/sales/user",
    "sales_best_api": "https://mm2.dreampets.gg/api/sales/v1/sales/best",

    "currency": "rub",
    "min_listing_price": 2.0,
    "undercut": 0.01,
    "top_n": 5,
    "workers": 10,

    # статусы, при которых предмет точно можно выставить (или он уже выставлен)
    "inventory_statuses": ["in_inventory", "on_sale"],
    # слова в статусе, при которых предмет выставлять нельзя
    # (sold, withdrawn, trade_lock, pending, reserved ...)
    "skip_status_words": ["sold", "withdraw", "lock", "hold", "pend", "trade", "reserv",
                          "delet", "cancel", "block", "ban", "freez", "transfer", "deliver",
                          "process", "wait", "refund", "expire", "remov", "escrow"],
    # открыть страницу «Продажа» и взять инвентарь так, как его видит сайт
    "learn_from_site": True,

    "dry_run": False,
    # auto — через вкладку браузера, при неудаче напрямую; browser; requests
    "transport": "auto",
    "auto_discover": True,

    "browser_path": "",
    "browser_args": [],
    "cdp_port": 9333,
    "auth_timeout_s": 900,
}

BROWSER_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
BROWSER_NAMES = ("chrome", "google-chrome", "google-chrome-stable", "chromium",
                 "chromium-browser", "msedge")


# ---------- файлы рядом с программой ----------

def app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_PATH = os.path.join(app_dir(), "mm2pricer_config.json")
LOG_PATH = os.path.join(app_dir(), "mm2pricer_log.txt")


def load_config(path: str) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if not os.path.isfile(path):
        return cfg
    try:
        with open(path, encoding="utf-8-sig") as f:
            user = json.load(f)
    except Exception as e:
        print(f"Не смог прочитать {path}: {e}\nИспользую настройки по умолчанию.\n")
        return cfg
    for k, v in user.items():
        if k in DEFAULT_CONFIG:
            cfg[k] = v
        elif not k.startswith("_"):
            print(f"В {os.path.basename(path)} неизвестная настройка «{k}» — пропускаю.")
    print(f"Настройки взяты из {path}")
    return cfg


# ---------- лог ----------

_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*")
_BEARER_RE = re.compile(r"(Bearer\s+)[^\s\"',]+", re.I)
_TOKEN_FIELD_RE = re.compile(r"(\"[A-Za-z_]*(?:token|secret|password)[A-Za-z_]*\"\s*:\s*)\"[^\"]*\"", re.I)
_log_file = None
_log_lock = threading.Lock()


def redact(text: str) -> str:
    text = _TOKEN_FIELD_RE.sub(r'\1"<token>"', _JWT_RE.sub("<token>", text))
    return _BEARER_RE.sub(r"\1<token>", text)


def open_log(path: str):
    global _log_file
    try:
        _log_file = open(path, "w", encoding="utf-8")
    except Exception as e:
        print(f"Не смог создать лог {path}: {e}")


def log(msg):
    if _log_file is None:
        return
    line = f"{time.strftime('%H:%M:%S')} {redact(str(msg))}\n"
    with _log_lock:
        try:
            _log_file.write(line)
            _log_file.flush()
        except Exception:
            pass


def short(text, n=120) -> str:
    text = str(text).strip()
    return (text.splitlines()[0] if text else "")[:n]


# ---------- разбор ответов API ----------

def first(d, *keys):
    """Первое непустое значение из d по списку возможных имён поля."""
    if not isinstance(d, dict):
        return None
    for k in keys:
        v = d.get(k)
        if v is not None and v != "":
            return v
    return None


def extract_list(body, *keys, _depth=0):
    """Список из ответа: [..], {"items": [..]}, {"data": {"items": [..]}} и т.п."""
    if isinstance(body, list):
        return body
    if not isinstance(body, dict) or _depth > 2:
        return None
    for k in (*keys, "data", "result", "results", "rows", "list", "content"):
        if k in body:
            found = extract_list(body[k], *keys, _depth=_depth + 1)
            if found is not None:
                return found
    return None


def to_price(v) -> float | None:
    if isinstance(v, dict):
        v = first(v, "amount", "value", "price")
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", ".").strip())
    except ValueError:
        return None


# флаги в предмете, означающие «выставлять нельзя»
_BLOCK_IF_TRUE = ("locked", "is_locked", "trade_locked", "is_trade_locked", "on_hold", "is_on_hold",
                  "withdrawn", "is_withdrawn", "sold", "is_sold")
_BLOCK_IF_FALSE = ("tradable", "is_tradable", "tradeable", "can_sell", "sellable", "is_sellable",
                   "can_be_sold", "available", "is_available")


def norm_item(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    product = raw.get("product") if isinstance(raw.get("product"), dict) else {}
    item_id = first(raw, "item_id", "itemId", "id", "_id")
    pid = first(product, "product_id", "productId", "id", "_id") or first(raw, "product_id", "productId")
    if item_id is None or pid is None:
        return None
    name = first(product, "name", "title", "display_name") or first(raw, "name", "title") or pid
    blocked = (any(raw.get(k) is True for k in _BLOCK_IF_TRUE)
               or any(raw.get(k) is False for k in _BLOCK_IF_FALSE))
    return {
        "item_id": item_id,
        "product_id": pid,
        "name": str(name),
        "status": str(raw.get("status") or raw.get("state") or "").strip().lower(),
        "blocked": blocked,
        "raw": raw,
    }


def norm_sale(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    item = raw.get("item") if isinstance(raw.get("item"), dict) else {}
    product = raw.get("product") if isinstance(raw.get("product"), dict) else {}
    sale_id = first(raw, "sale_id", "saleId", "id", "_id")
    if sale_id is None:
        return None
    return {
        "sale_id": sale_id,
        "item_id": first(raw, "item_id", "itemId") or first(item, "item_id", "itemId", "id", "_id"),
        "product_id": first(raw, "product_id", "productId") or first(product, "product_id", "productId", "id"),
        "price": to_price(first(raw, "price", "amount", "cost")),
        "status": str(raw.get("status") or raw.get("state") or "").strip().lower(),
    }


_WORD_SPLIT_RE = re.compile(r"[^0-9a-zа-яё]+")


def _status_words(status: str) -> list[str]:
    return [w for w in _WORD_SPLIT_RE.split(status.lower()) if w]


def status_blocked(status: str, cfg: dict) -> bool:
    """Статус означает «выставлять нельзя» (продан, выведен, заблокирован...)."""
    if not status or status in {s.lower() for s in cfg["inventory_statuses"]}:
        return False
    stop = [w.lower() for w in cfg["skip_status_words"]]
    return any(word.startswith(s) for word in _status_words(status) for s in stop)


def looks_listed(status: str) -> bool:
    return any(w in ("sale", "listed", "selling", "onsale") for w in _status_words(status))


_SALE_DONE_WORDS = ("sold", "cancel", "complet", "finish", "expire", "close", "delet",
                    "withdraw", "inactive", "archiv")


def sale_active(sale: dict) -> bool:
    return not any(w.startswith(s) for w in _status_words(sale["status"]) for s in _SALE_DONE_WORDS)


_NESTED_ITEM_KEYS = ("items", "item_list", "itemList", "units", "children")
_ID_LIST_KEYS = ("item_ids", "itemIds", "ids")


def expand_items(lst: list) -> list[dict]:
    """Разворачивает сгруппированные записи («Candy ×6» -> 6 предметов):
    {"product": {...}, "items": [{...}, ...]} или {"product": {...}, "item_ids": [...]}."""
    out = []
    for x in lst:
        if not isinstance(x, dict):
            continue
        product = x.get("product") if isinstance(x.get("product"), dict) else None
        if product is not None:
            nested = next((x[k] for k in _NESTED_ITEM_KEYS if isinstance(x.get(k), list) and x[k]
                           and all(isinstance(y, dict) for y in x[k])), None)
            if nested is not None:
                for y in nested:
                    y = dict(y)
                    if not isinstance(y.get("product"), dict) and not first(y, "product_id", "productId"):
                        y["product"] = product
                    if "status" in x and "status" not in y:
                        y["status"] = x["status"]
                    out.append(y)
                continue
            ids = next((x[k] for k in _ID_LIST_KEYS if isinstance(x.get(k), list) and x[k]
                        and all(isinstance(i, (str, int)) for i in x[k])), None)
            if ids is not None:
                out.extend({"item_id": i, "product": product, "status": x.get("status")} for i in ids)
                continue
        out.append(x)
    return out


def ident(x) -> str:
    """Ключ записи для отсева повторов при постраничной загрузке."""
    if isinstance(x, dict):
        v = first(x, "item_id", "sale_id", "itemId", "saleId", "id", "_id")
        if v is not None:
            return str(v)
    return json.dumps(x, sort_keys=True, ensure_ascii=False, default=str)


def add_params(url: str, params: dict | None) -> str:
    if not params:
        return url
    u = urlparse(url)
    q = dict(parse_qsl(u.query, keep_blank_values=True))
    q.update({k: str(v) for k, v in params.items()})
    return urlunparse(u._replace(query=urlencode(q)))


_TOTAL_KEYS = ("total", "total_count", "totalCount", "total_items", "totalItems", "count")
_MORE_KEYS = ("has_more", "hasMore", "has_next", "hasNext", "hasNextPage", "has_next_page")
_PAGES_KEYS = ("pages", "total_pages", "totalPages", "last_page", "lastPage", "page_count", "pageCount")
_NEXT_KEYS = ("next", "next_page", "nextPage", "next_url", "nextUrl", "next_page_url")
_CURSOR_KEYS = ("next_cursor", "nextCursor", "cursor", "after", "next_token", "nextToken")
# размеры, на которых список часто обрезан страницей
_ROUND_SIZES = {10, 12, 15, 16, 20, 24, 25, 30, 32, 36, 40, 48, 50, 60, 64, 72, 80, 96, 100,
                120, 128, 150, 200, 250, 256, 300, 500, 1000}


def page_hints(body, n: int) -> dict:
    """Признаки того, что в ответе только часть списка."""
    hints = {"more": None, "total": None, "next": None, "cursor": None}
    if not isinstance(body, dict):
        return hints
    blocks = [body] + [body[k] for k in ("meta", "pagination", "paging", "page_info", "pageInfo", "data")
                       if isinstance(body.get(k), dict)]
    for b in blocks:
        for k in _TOTAL_KEYS:
            v = b.get(k)
            if isinstance(v, int) and not isinstance(v, bool) and v > n:
                hints["more"], hints["total"] = True, v
        for k in _MORE_KEYS:
            if isinstance(b.get(k), bool):
                hints["more"] = b[k] or bool(hints["more"])
        for k in _PAGES_KEYS:
            v = b.get(k)
            if isinstance(v, int) and not isinstance(v, bool) and v > 1:
                hints["more"] = True
        for k in _NEXT_KEYS:
            v = b.get(k)
            if isinstance(v, str) and v and (v.startswith("/") or "://" in v or "?" in v):
                hints["more"], hints["next"] = True, v
        for k in _CURSOR_KEYS:
            v = b.get(k)
            if isinstance(v, (str, int)) and not isinstance(v, bool) and v != "":
                hints["more"], hints["cursor"] = True, (k, v)
    return hints


def missing_fields(data) -> list[str]:
    """Какие поля сайт требует в запросе (по тексту ошибки 400/422)."""
    names = []
    if isinstance(data, dict):
        detail = data.get("detail")
        if isinstance(detail, list):
            for d in detail:
                if not isinstance(d, dict):
                    continue
                loc = d.get("loc") or []
                kind = f"{d.get('type', '')} {d.get('msg', '')}".lower()
                if loc and isinstance(loc[-1], str) and ("missing" in kind or "required" in kind):
                    names.append(loc[-1])
        errors = data.get("errors")
        if isinstance(errors, dict):
            names += [str(k).split(".")[-1] for k in errors]
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    names += re.findall(r"['\"`]?([A-Za-z_][A-Za-z0-9_]*)['\"`]? (?:is|field is|field) required", text or "")
    out = []
    for n in names:
        if n and n not in out:
            out.append(n)
    return out


def _name_variants(name: str) -> set[str]:
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
    parts = snake.split("_")
    camel = parts[0] + "".join(p.title() for p in parts[1:])
    return {name, snake, camel}


def find_field(raw: dict, name: str):
    """Значение поля name где-нибудь в данных предмета (account_id, roblox_account_id ...)."""
    variants = _name_variants(name)
    queue = [raw]
    while queue:
        d = queue.pop(0)
        for k, v in d.items():
            if k in variants and v is not None and not isinstance(v, (dict, list)):
                return v
        queue.extend(v for v in d.values() if isinstance(v, dict))
    # account_id -> {"account": {"id": ...}}
    m = re.match(r"^(.+?)_?(?:id|Id|ID)$", name)
    if m:
        for base in _name_variants(m.group(1)):
            sub = raw.get(base)
            if isinstance(sub, dict) and first(sub, "id", "_id") is not None:
                return first(sub, "id", "_id")
    return None


def profile_name(body) -> str | None:
    """Ник (или id) из ответа профиля; None — если это не профиль."""
    if not isinstance(body, dict):
        return None
    data = body.get("data")
    variants = [body, data, body.get("user"), body.get("profile")]
    if isinstance(data, dict):
        variants.append(data.get("user"))
    for v in variants:
        uid = first(v, "user_id", "userId", "id", "_id", "uid")
        if uid is not None:
            return str(first(v, "username", "name", "nickname", "login", "display_name") or uid)
    return None


_TOKEN_KEY_RE = re.compile(r"access.?token|^token$|jwt|auth.?token|id.?token", re.I)
_JWT_FULL_RE = re.compile(r"^eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*$")


def find_tokens(storage: dict, cookies: list[dict]) -> list[str]:
    """Кандидаты в access-токен из localStorage/sessionStorage/кук.

    Старый формат сайта (localStorage['auth'] -> {"data": "<JSON>"} -> accessToken)
    тоже находится: JSON-строки разворачиваются рекурсивно.
    """
    strong, weak = [], []

    def visit(v, key):
        if isinstance(v, dict):
            for k, x in v.items():
                visit(x, str(k))
        elif isinstance(v, list):
            for x in v:
                visit(x, key)
        elif isinstance(v, str):
            s = v.strip()
            if s[:1] in "{[":
                try:
                    visit(json.loads(s), key)
                    return
                except ValueError:
                    pass
            if "refresh" in key.lower():
                return
            if s.lower().startswith("bearer "):
                s = s[7:].strip()
            if _JWT_FULL_RE.match(s):
                (strong if _TOKEN_KEY_RE.search(key) else weak).append(s)
            elif _TOKEN_KEY_RE.search(key) and len(s) >= 20 and " " not in s:
                weak.append(s)

    visit(storage, "")
    for c in cookies:
        visit(unquote(c.get("value", "")), c.get("name", ""))
    out = []
    for t in strong + weak:
        if t not in out:
            out.append(t)
    return out[:4]


_API_PATH_RE = r"(?:https?://[A-Za-z0-9.-]+)?/api/[A-Za-z0-9_-]+/v\d+(?:/[A-Za-z0-9_${}:.-]*)*"


def host_in_domain(host: str | None, domain: str) -> bool:
    host = (host or "").lower()
    return host == domain or host.endswith("." + domain)


def endpoint_alternatives(url: str, known: list[str], domain: str) -> list[str]:
    """Возможные новые адреса для url вида /api/<служба>/v<N>/<путь>:
    та же служба с другой версией (и, возможно, на другом поддомене сайта)."""
    u = urlparse(url)
    m = re.match(r"^/api/([^/]+)/v(\d+)(/.*)?$", u.path)
    if not m:
        return []
    svc, rest = m.group(1), m.group(3) or ""
    exact, by_version = [], {}
    for s in known:
        su = urlparse(s if "://" in s else "https://placeholder" + s)
        netloc = su.netloc if "://" in s else u.netloc
        if "://" in s and not host_in_domain(su.hostname, domain):
            continue
        sm = re.match(r"^/api/([^/]+)/v(\d+)(/.*)?$", su.path)
        if not sm or sm.group(1) != svc:
            continue
        ver, srest = int(sm.group(2)), sm.group(3) or ""
        cand = f"{u.scheme}://{netloc}/api/{svc}/v{ver}{rest}"
        if srest.rstrip("/") == rest.rstrip("/"):
            exact.append((ver, cand))
        else:
            by_version.setdefault(ver, cand)
    out = []
    for _, cand in sorted(exact, reverse=True) + sorted(by_version.items(), reverse=True):
        if cand != url and cand not in out:
            out.append(cand)
    return out


# ---------- браузер (Chrome DevTools Protocol) ----------

class CDPError(Exception):
    pass


class ApiError(RuntimeError):
    def __init__(self, message: str, status=None):
        super().__init__(message)
        self.status = status


class CDPSession:
    """Постоянное соединение с вкладкой: команды + события (Network.*)."""

    def __init__(self, ws_url: str, on_event=None):
        self.ws = websocket.create_connection(ws_url, timeout=10, suppress_origin=True,
                                              enable_multithread=True)
        self.ws.settimeout(None)
        self.on_event = on_event
        self.alive = True
        self._next_id = 0
        self._lock = threading.Lock()
        self._pending = {}
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        try:
            while True:
                msg = json.loads(self.ws.recv())
                mid = msg.get("id")
                if mid is not None:
                    slot = self._pending.pop(mid, None)
                    if slot:
                        slot[1] = msg
                        slot[0].set()
                elif self.on_event:
                    try:
                        self.on_event(self, msg.get("method"), msg.get("params") or {})
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            self.alive = False
            for slot in list(self._pending.values()):
                slot[0].set()
            self._pending.clear()

    def call(self, method: str, params: dict | None = None, timeout: float = 30):
        if not self.alive:
            raise CDPError("соединение со вкладкой закрыто")
        slot = [threading.Event(), None]
        with self._lock:
            self._next_id += 1
            mid = self._next_id
            self._pending[mid] = slot
        try:
            self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        except Exception as e:
            self._pending.pop(mid, None)
            self.alive = False
            raise CDPError(f"{method}: {e}")
        if not slot[0].wait(timeout):
            self._pending.pop(mid, None)
            raise CDPError(f"{method}: нет ответа {timeout:.0f} с")
        msg = slot[1]
        if msg is None:
            raise CDPError("соединение со вкладкой закрыто")
        if "error" in msg:
            raise CDPError(f"{method}: {msg['error'].get('message')}")
        return msg.get("result") or {}

    def close(self):
        self.alive = False
        try:
            self.ws.close()
        except Exception:
            pass


# Метка для кода, который мы выполняем во вкладке: по ней наши собственные
# запросы отличаются от запросов сайта, когда мы подсматриваем заголовки.
_OUR_SOURCE = "mm2pricer-internal"

_FETCH_JS = """(async () => {
  const req = %s;
  let lastErr = '';
  for (const mode of req.modes) {
    try {
      const opts = {method: req.method, headers: req.headers, credentials: mode,
                    cache: 'no-store', signal: AbortSignal.timeout(req.timeout)};
      if (req.body !== null) opts.body = req.body;
      const r = await fetch(req.url, opts);
      return JSON.stringify({status: r.status, text: await r.text(), mode: mode});
    } catch (e) { lastErr = String(e); }
  }
  return JSON.stringify({error: lastErr});
})()
//# sourceURL=""" + _OUR_SOURCE

_STORAGE_JS = """(() => {
  const dump = s => { const o = {}; try { for (let i = 0; i < s.length; i++) {
    const k = s.key(i); o[k] = s.getItem(k); } } catch (e) {} return o; };
  return JSON.stringify({local: dump(window.localStorage), session: dump(window.sessionStorage)});
})()
//# sourceURL=""" + _OUR_SOURCE

_INLINE_SCRIPTS_JS = """JSON.stringify([...document.scripts].filter(s => !s.src).map(s => s.textContent || ''))
//# sourceURL=""" + _OUR_SOURCE

_LOGOUT_JS = "try { localStorage.clear(); sessionStorage.clear(); } catch (e) {}"

_SELL_LINK_JS = r"""(() => {
  const want = /^(продажа|продать|мои продажи|sell|selling)$/i;
  const els = [...document.querySelectorAll('a, button, [role="tab"], [role="link"], [role="button"]')];
  const el = els.find(e => want.test((e.innerText || e.textContent || '').trim()));
  if (!el) return '';
  el.click();
  return el.href || (el.innerText || '').trim();
})()
//# sourceURL=""" + _OUR_SOURCE

_SCROLL_JS = """(() => { window.scrollTo(0, document.body.scrollHeight);
  document.querySelectorAll('*').forEach(e => { if (e.scrollHeight > e.clientHeight + 50) e.scrollTop = e.scrollHeight; }); })()
//# sourceURL=""" + _OUR_SOURCE

# заголовки, которые не надо повторять за сайтом (их ставит сам браузер/requests)
_SKIP_HEADERS = {
    "accept", "accept-encoding", "accept-language", "cache-control", "connection",
    "content-length", "content-type", "cookie", "host", "origin", "pragma", "referer",
    "user-agent", "priority", "dnt", "upgrade-insecure-requests", "if-none-match",
    "if-modified-since", "te", "keep-alive", "range",
}


def pick_port(preferred: int) -> int:
    for port in ([preferred] if preferred else []) + [0]:
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("нет свободного порта")


class Browser:
    def __init__(self, cfg: dict, profile_dir: str):
        self.cfg = cfg
        self.domain = cfg["site_domain"].lower()
        self.profile_dir = profile_dir
        self.port = None
        self.proc = None
        self.http = requests.Session()
        self.http.trust_env = False          # DevTools на 127.0.0.1 — без прокси
        self.cred_mode = None                # credentials для fetch, который сработал
        self._sessions = {}                  # target id -> [CDPSession, url]
        self._sess_lock = threading.Lock()
        self._sniff_lock = threading.Lock()
        self._req_meta = {}                  # requestId -> (method, url)
        self.sniffed = {}                    # host -> {заголовок: значение}
        self.observed = {}                   # (method, url без query) -> status
        self.captured = []                   # ответы API, которые получил сам сайт
        self._body_threads = []
        self.last_api_event = time.time()
        self._cookie_cache = {}
        self._ua = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # --- запуск ---

    def find_executable(self) -> str | None:
        paths = [os.path.expandvars(self.cfg["browser_path"])] if self.cfg["browser_path"] else []
        for p in paths + BROWSER_PATHS:
            if os.path.isfile(p):
                return p
        for name in BROWSER_NAMES:
            p = shutil.which(name)
            if p:
                return p
        return None

    def launch(self) -> bool:
        exe = self.find_executable()
        if not exe:
            print("Не нашёл Chrome/Edge. Установите Google Chrome и запустите снова\n"
                  "(или укажите путь к браузеру в browser_path в mm2pricer_config.json).")
            return False
        self.port = pick_port(int(self.cfg["cdp_port"] or 0))
        args = [
            exe,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile_dir}",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            *self.cfg["browser_args"],
            self.cfg["login_url"],
        ]
        log(f"Запуск браузера: {exe}, порт {self.port}")
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 25
        while time.time() < deadline:
            try:
                info = self.http.get(f"{self.base}/json/version", timeout=2).json()
                self._ua = info.get("User-Agent")
                log(f"Браузер: {info.get('Browser')}")
                self.refresh()
                return True
            except Exception:
                time.sleep(1)
        print("Не дождался запуска браузера.")
        return True

    # --- вкладки ---

    def refresh(self):
        """Подключается ко всем вкладкам и включает в них наблюдение за сетью."""
        try:
            targets = self.http.get(f"{self.base}/json", timeout=5).json()
        except Exception:
            return
        pages = {t["id"]: t for t in targets
                 if t.get("type") == "page" and t.get("webSocketDebuggerUrl")}
        with self._sess_lock:
            for tid in list(self._sessions):
                sess = self._sessions[tid][0]
                if tid not in pages or not sess.alive:
                    sess.close()
                    del self._sessions[tid]
            for tid, t in pages.items():
                if tid in self._sessions:
                    self._sessions[tid][1] = t.get("url", "")
                    continue
                try:
                    sess = CDPSession(t["webSocketDebuggerUrl"], self._on_event)
                    sess.call("Network.enable", timeout=10)
                    self._sessions[tid] = [sess, t.get("url", "")]
                except Exception as e:
                    log(f"Не подключился к вкладке {t.get('url')}: {e}")

    def primary(self, site_only: bool = False) -> CDPSession | None:
        """Вкладка сайта; если её нет — любая (кроме site_only)."""
        with self._sess_lock:
            alive = [(s, u) for s, u in self._sessions.values() if s.alive]
        for s, u in alive:
            if host_in_domain(urlparse(u).hostname, self.domain):
                return s
        return alive[0][0] if alive and not site_only else None

    def evaluate(self, expression: str, await_promise=False, timeout: float = 30,
                 site_only: bool = False):
        last = CDPError("нет открытой вкладки сайта" if site_only else "нет открытой вкладки браузера")
        for _ in range(3):
            sess = self.primary(site_only)
            if sess is None:
                self.refresh()
                sess = self.primary(site_only)
            if sess is None:
                time.sleep(1)
                continue
            try:
                res = sess.call("Runtime.evaluate", {
                    "expression": expression,
                    "returnByValue": True,
                    "awaitPromise": await_promise,
                }, timeout=timeout)
            except CDPError as e:
                # например, вкладка как раз перезагружается
                last = e
                time.sleep(1)
                self.refresh()
                continue
            if "exceptionDetails" in res:
                last = CDPError(res["exceptionDetails"].get("text") or "ошибка JS")
                time.sleep(1)
                continue
            return (res.get("result") or {}).get("value")
        raise last

    # --- подсматривание запросов сайта ---

    @staticmethod
    def _from_us(initiator: dict) -> bool:
        stack = (initiator or {}).get("stack")
        while stack:
            if any(f.get("url") == _OUR_SOURCE for f in stack.get("callFrames") or []):
                return True
            stack = stack.get("parent")
        return False

    def _on_event(self, sess: CDPSession, method: str, p: dict):
        rid = p.get("requestId")
        if method == "Network.requestWillBeSent":
            req = p.get("request") or {}
            url = req.get("url", "")
            if not host_in_domain(urlparse(url).hostname, self.domain):
                return
            if req.get("method") == "OPTIONS" or self._from_us(p.get("initiator")):
                return
            if p.get("type") not in ("XHR", "Fetch") and "/api/" not in url:
                return
            self._req_meta[rid] = [req.get("method", "GET"), url, None, ""]
            self.last_api_event = time.time()
            self._remember_headers(url, req.get("headers") or {})
        elif method == "Network.requestWillBeSentExtraInfo":
            meta = self._req_meta.get(rid)
            if meta:
                self._remember_headers(meta[1], p.get("headers") or {})
        elif method == "Network.responseReceived":
            meta = self._req_meta.get(rid)
            if meta:
                resp = p.get("response") or {}
                meta[2], meta[3] = resp.get("status"), resp.get("mimeType") or ""
                self.observed[(meta[0], meta[1].split("?")[0])] = meta[2]
        elif method == "Network.loadingFinished":
            meta = self._req_meta.get(rid)
            if meta:
                self.last_api_event = time.time()
                if "json" in meta[3] or not meta[3]:
                    # тело ответа сайта — чтобы увидеть, откуда сайт берёт инвентарь.
                    # Запрашиваем в отдельном потоке: этот поток читает ответы CDP.
                    t = threading.Thread(target=self._grab_body, args=(sess, rid, list(meta)), daemon=True)
                    self._body_threads.append(t)
                    t.start()

    def _grab_body(self, sess: CDPSession, rid: str, meta: list):
        try:
            res = sess.call("Network.getResponseBody", {"requestId": rid}, timeout=15)
        except Exception:
            return
        body = res.get("body", "")
        if res.get("base64Encoded"):
            try:
                body = base64.b64decode(body).decode("utf-8", "replace")
            except Exception:
                return
        if len(body) <= 5_000_000:
            self.captured.append({"method": meta[0], "url": meta[1], "status": meta[2], "body": body})

    def wait_quiet(self, min_s: float, quiet_s: float, max_s: float):
        """Ждёт, пока сайт перестанет делать запросы к API."""
        start = time.time()
        while True:
            time.sleep(0.5)
            now = time.time()
            if now - start >= max_s:
                break
            if now - start >= min_s and now - self.last_api_event >= quiet_s:
                break
        for t in list(self._body_threads):
            t.join(timeout=5)

    def open_sell_page(self) -> str:
        """Нажимает «Продажа» в меню сайта. Возвращает, куда перешли ('' — не нашёл)."""
        try:
            return self.evaluate(_SELL_LINK_JS, timeout=10, site_only=True) or ""
        except Exception as e:
            log(f"Не смог открыть «Продажа»: {e}")
            return ""

    def scroll_down(self):
        try:
            self.evaluate(_SCROLL_JS, timeout=10, site_only=True)
        except Exception:
            pass

    def _remember_headers(self, url: str, headers: dict):
        keep = {}
        for k, v in headers.items():
            lk = k.lower()
            if lk in _SKIP_HEADERS or lk.startswith(("sec-", ":", "proxy-", "access-control-")):
                continue
            keep[lk] = v
        if keep:
            host = urlparse(url).hostname or ""
            with self._sniff_lock:
                self.sniffed.setdefault(host, {}).update(keep)

    def site_headers(self, host: str) -> dict:
        """Заголовки, с которыми сайт сам ходит в API (сначала — для этого хоста)."""
        with self._sniff_lock:
            merged = {}
            for hdrs in self.sniffed.values():
                merged.update(hdrs)
            merged.update(self.sniffed.get(host, {}))
        return merged

    # --- данные вкладки ---

    def storage_dump(self) -> dict:
        # только со вкладки сайта: чужие токены (например, Roblox) сайту не отправляем
        try:
            return json.loads(self.evaluate(_STORAGE_JS, timeout=10, site_only=True) or "{}")
        except Exception:
            return {}

    def cookies(self, urls: list[str]) -> list[dict]:
        """Куки, которые браузер отправил бы на эти адреса (включая httpOnly)."""
        sess = self.primary()
        if sess is None:
            return []
        try:
            return sess.call("Network.getCookies", {"urls": urls}, timeout=10).get("cookies", [])
        except Exception:
            return []

    def cookie_header(self, url: str) -> str:
        host = urlparse(url).hostname or ""
        cached = self._cookie_cache.get(host)
        if cached and time.time() - cached[0] < 20:
            return cached[1]
        value = "; ".join(f"{c['name']}={c['value']}" for c in self.cookies([url]))
        self._cookie_cache[host] = (time.time(), value)
        return value

    def user_agent(self) -> str | None:
        return self._ua

    def fetch(self, method: str, url: str, headers: dict, body: str | None, timeout: float,
              api: bool = True):
        """HTTP-запрос изнутри вкладки сайта. Возвращает (status, text)."""
        if not api:
            modes = ["omit"]
        elif self.cred_mode:
            modes = [self.cred_mode]
        elif method == "GET":
            modes = ["include", "omit"]
        else:
            modes = ["include"]
        payload = {"method": method, "url": url, "headers": headers, "body": body,
                   "modes": modes, "timeout": int(timeout * 1000)}
        raw = self.evaluate(_FETCH_JS % json.dumps(payload), await_promise=True,
                            timeout=timeout + 10)
        res = json.loads(raw or "{}")
        if "status" not in res:
            raise CDPError(f"fetch из вкладки не прошёл: {res.get('error')}")
        if api and self.cred_mode is None and method == "GET":
            self.cred_mode = res.get("mode")
        return res["status"], res.get("text", "")

    def script_sources(self) -> list[str]:
        """Тексты JS сайта — чтобы найти в них адреса API."""
        texts = []
        try:
            texts.extend(json.loads(self.evaluate(_INLINE_SCRIPTS_JS, timeout=15, site_only=True) or "[]"))
        except Exception:
            pass
        sess = self.primary(site_only=True)
        if sess is None:
            return texts
        try:
            sess.call("Page.enable", timeout=15)
            tree = sess.call("Page.getResourceTree", timeout=15)["frameTree"]
        except Exception as e:
            log(f"Page.getResourceTree: {e}")
            return texts
        stack = [tree]
        while stack:
            node = stack.pop()
            frame_id = node["frame"]["id"]
            for res in node.get("resources", []):
                if res.get("type") != "Script":
                    continue
                try:
                    c = sess.call("Page.getResourceContent",
                                  {"frameId": frame_id, "url": res["url"]}, timeout=15)
                    if not c.get("base64Encoded"):
                        texts.append(c.get("content", ""))
                        continue
                except Exception as e:
                    log(f"Page.getResourceContent {res['url']}: {e}")
                try:
                    status, text = self.fetch("GET", res["url"], {}, None, 15, api=False)
                    if status == 200:
                        texts.append(text)
                except Exception:
                    pass
            stack.extend(node.get("childFrames", []))
        return texts

    # --- выход ---

    def close(self):
        # разлогин: чистим хранилища и куки, затем закрываем браузер
        sess = self.primary()
        if sess:
            try:
                sess.call("Runtime.evaluate", {"expression": _LOGOUT_JS}, timeout=5)
                sess.call("Network.clearBrowserCookies", timeout=5)
            except Exception:
                pass
        with self._sess_lock:
            for s, _ in self._sessions.values():
                s.close()
            self._sessions.clear()
        if self.port:
            try:
                ws_url = self.http.get(f"{self.base}/json/version", timeout=3).json()["webSocketDebuggerUrl"]
                ws = websocket.create_connection(ws_url, timeout=5, suppress_origin=True)
                ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
                ws.close()
            except Exception:
                pass
        if self.proc:
            try:
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.terminate()
                    self.proc.wait(timeout=5)
                except Exception:
                    pass
        if os.name == "nt":
            # добиваем процессы браузера именно этого профиля
            ps = (
                "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' OR Name='msedge.exe'\" | "
                f"Where-Object {{ $_.CommandLine -like '*{self.profile_dir}*' }} | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
            )
            try:
                subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                               capture_output=True, timeout=15)
            except Exception:
                pass
        elif self.proc and self.proc.poll() is None:
            self.proc.kill()
        time.sleep(1)
        for _ in range(5):
            shutil.rmtree(self.profile_dir, ignore_errors=True)
            if not os.path.exists(self.profile_dir):
                return
            time.sleep(1)


# ---------- API ----------

class Api:
    def __init__(self, cfg: dict, browser: Browser):
        self.cfg = cfg
        self.browser = browser
        self.endpoints = {k: cfg[k] for k in ENDPOINT_KEYS}
        # чем авторизуемся: ("sniffed", None) — как сайт, ("token", "Bearer ..."),
        # ("cookies", None) — только куки
        self.auth = ("cookies", None)
        self.http = requests.Session()
        self._discover_lock = threading.Lock()
        self._discover_at = {}
        self.extra_fields = set()            # поля, которые сайт требует при выставлении

    # --- транспорт ---

    def headers_for(self, url: str) -> dict:
        h = {k: v for k, v in self.browser.site_headers(urlparse(url).hostname or "").items()
             if k != "authorization" or self.auth[0] == "sniffed"}
        if self.auth[0] == "token":
            h["authorization"] = self.auth[1]
        h["accept"] = "application/json"
        return h

    def _via_requests(self, method, url, headers, body, timeout):
        h = dict(headers)
        login = urlparse(self.cfg["login_url"])
        h["Origin"] = f"{login.scheme}://{login.netloc}"
        h["Referer"] = self.cfg["login_url"]
        if self.browser.user_agent():
            h["User-Agent"] = self.browser.user_agent()
        cookie = self.browser.cookie_header(url)
        if cookie:
            h["Cookie"] = cookie
        r = self.http.request(method, url, headers=h, data=body, timeout=timeout)
        return r.status_code, r.text

    def _transport(self, method, url, headers, body, timeout):
        mode = self.cfg["transport"]
        if mode == "requests":
            order = ["requests"]
        elif mode == "browser":
            order = ["browser"]
        elif method == "GET":
            order = ["browser", "requests"]
        else:
            # изменяющие запросы не повторяем другим способом — чтобы не задвоить
            order = ["browser"] if self.browser.cred_mode else ["requests"]
        last = None
        for how in order:
            try:
                if how == "browser":
                    return self.browser.fetch(method, url, headers, body, timeout)
                return self._via_requests(method, url, headers, body, timeout)
            except Exception as e:
                last = e
                log(f"{how}: {method} {url}: {e}")
        raise RuntimeError(f"нет связи с сайтом ({short(last)})")

    def _send(self, method, url, params=None, body=None, timeout=20):
        full = add_params(url, params)
        headers = self.headers_for(url)
        data = None
        if body is not None:
            data = json.dumps(body)
            headers["content-type"] = "application/json"
        for attempt in range(4):
            status, text = self._transport(method, full, headers, data, timeout)
            retry = status == 429 or (method == "GET" and status in (502, 503, 504))
            if not retry or attempt == 3:
                break
            time.sleep(1.5 * 2 ** attempt)
        log(f"{method} {full} {data or ''} -> {status} {text[:2000]!r}")
        try:
            return status, json.loads(text) if text else None
        except ValueError:
            return status, text

    def request(self, method: str, key: str, params=None, body=None):
        url = self.endpoints[key]
        status, data = self._send(method, url, params, body)
        if status != 404 or not self.cfg["auto_discover"]:
            return status, data
        with self._discover_lock:
            current = self.endpoints[key]
            if current != url:                       # другой поток уже нашёл новый адрес
                return self._send(method, current, params, body)
            # ищем заново раз в минуту или сразу, как сайт сделал новые запросы
            seen = len(self.browser.observed)
            last_at, last_seen = self._discover_at.get(key, (0, -1))
            if time.time() - last_at < 60 and seen == last_seen:
                return status, data
            self._discover_at[key] = (time.time(), seen)
            for cand in endpoint_alternatives(url, self.known_paths(), self.browser.domain)[:4]:
                st, dt = self._send(method, cand, params, body)
                if st != 404:
                    self.endpoints[key] = cand
                    print(f"  (адрес API изменился: {url} -> {cand})")
                    log(f"Новый адрес {key}: {cand}")
                    return st, dt
        return status, data

    def known_paths(self) -> list[str]:
        """Адреса API из кода сайта и из запросов, которые делал сам сайт."""
        found = set()
        for text in self.browser.script_sources():
            found.update(re.findall(_API_PATH_RE, text))
        log(f"Адреса API в коде сайта ({len(found)}):\n  " + "\n  ".join(sorted(found)))
        observed = [u for (_, u) in list(self.browser.observed)]
        return observed + sorted(found)

    @staticmethod
    def _fail(what: str, status, data) -> "ApiError":
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        return ApiError(f"{what} вернул {status}: {short(text, 100)}", status)

    # --- методы ---

    def get_list(self, key: str, field: str, what: str) -> list:
        """Весь список (все страницы) с адреса key."""
        status, data = self.request("GET", key)
        if status != 200:
            raise self._fail(what, status, data)
        if extract_list(data, field) is None:
            raise RuntimeError(f"{what}: не понял формат ответа ({short(json.dumps(data, ensure_ascii=False), 100)})")
        return self.collect_pages(self.endpoints[key], field, data)

    def collect_pages(self, url: str, field: str, body) -> list:
        """Дочитывает остальные страницы списка, если сайт отдал не всё.
        Пробует ссылку/курсор на следующую страницу, page, offset и большой limit —
        засчитывается только то, что приносит новые записи."""
        out, seen = [], set()

        def add(lst) -> int:
            new = 0
            for x in lst or []:
                k = ident(x)
                if k not in seen:
                    seen.add(k)
                    out.append(x)
                    new += 1
            return new

        first_page = extract_list(body, field) or []
        add(first_page)
        hints = page_hints(body, len(first_page))
        if hints["more"] is False or (hints["more"] is None and len(first_page) not in _ROUND_SIZES):
            return out
        budget = [60]

        def done() -> bool:
            return budget[0] <= 0 or (hints["total"] is not None and len(out) >= hints["total"])

        def get(u, params=None):
            budget[0] -= 1
            st, b = self._send("GET", u, params)
            return (extract_list(b, field) if st == 200 else None), b

        # 1) готовая ссылка на следующую страницу или курсор
        b = body
        while not done():
            h = page_hints(b, len(out))
            if h["next"]:
                lst, b = get(urljoin(url, h["next"]))
            elif h["cursor"]:
                key, value = h["cursor"]
                for name in dict.fromkeys(("cursor", key, "after")):
                    lst, b2 = get(url, {name: value})
                    if lst and any(ident(x) not in seen for x in lst):
                        b = b2
                        break
                else:
                    break
            else:
                break
            if not add(lst):
                break
        # 2) большой размер страницы (если сайт ограничивает — берём его максимум)
        size = {}
        for name in ("limit", "per_page", "page_size", "pageSize", "size", "take"):
            if done():
                break
            lst, b = get(url, {name: 1000})
            value = 1000
            if lst is None:
                m = re.search(r"(?:less than or equal to|<=|не больше|max\w*)\D{0,20}(\d+)",
                              json.dumps(b, ensure_ascii=False) if b else "", re.I)
                value = int(m.group(1)) if m else 100
                lst, _ = get(url, {name: value})
            if add(lst):
                size = {name: value}
                break
        # 3) номера страниц (с 1 или с 0) и 4) смещение — с найденным размером страницы
        for make, start in ((lambda i: {"page": i}, 2), (lambda i: {"page": i}, 1),
                            (lambda i: {"offset": len(out)}, 0), (lambda i: {"skip": len(out)}, 0)):
            step = start
            while not done():
                lst, _ = get(url, {**size, **make(step)})
                if not add(lst):
                    break
                step += 1
        if len(out) > len(first_page):
            log(f"Список {url}: на первой странице {len(first_page)}, всего {len(out)}")
        return out

    def get_best_sales(self, product_id, count: int) -> list[dict]:
        params = {"product_id": product_id, "currency": self.cfg["currency"], "count": count}
        status, data = self.request("GET", "sales_best_api", params=params)
        if status in (400, 422) and count != self.cfg["top_n"]:
            params["count"] = self.cfg["top_n"]
            status, data = self.request("GET", "sales_best_api", params=params)
        if status != 200:
            raise self._fail("sales/best", status, data)
        sales = [s for s in map(norm_sale, extract_list(data, "sales") or []) if s]
        return sorted((s for s in sales if s["price"] is not None), key=lambda s: s["price"])

    def create_sale(self, item: dict, price: float):
        entry = {"item_id": item["item_id"], "product_id": item["product_id"],
                 "price": round(price, 2), "currency": self.cfg["currency"]}
        # поля, которые сайт уже требовал раньше, кладём сразу
        for name in list(self.extra_fields):
            value = find_field(item["raw"], name)
            if value is not None:
                entry.setdefault(name, value)
        for _ in range(3):
            status, data = self.request("POST", "sales_api", body={"items": [entry]})
            if status in (200, 201, 204):
                return
            # сайт требует новое поле — берём его из данных предмета и повторяем
            added = {}
            for name in missing_fields(data):
                if name not in entry:
                    value = find_field(item["raw"], name)
                    if value is not None:
                        added[name] = value
            if not added:
                break
            log(f"create_sale: сайт просит поля {sorted(added)} — добавляю из данных предмета")
            entry.update(added)
            self.extra_fields.update(added)
        raise self._fail("create_sale", status, data)

    def cancel_sales(self, sale_ids: list):
        if not sale_ids:
            return
        status, data = self.request("DELETE", "sales_api", body={"sale_ids": sale_ids})
        if status not in (200, 204):
            raise self._fail("cancel_sales", status, data)


# ---------- вход ----------

def detect_login(api: Api, browser: Browser, try_cookies: bool) -> str | None:
    candidates = []
    if "authorization" in browser.site_headers(""):
        candidates.append(("sniffed", None))
    storage = browser.storage_dump()
    cookies = browser.cookies([api.cfg["login_url"], *api.endpoints.values()])
    for tok in find_tokens(storage, cookies):
        candidates.append(("token", f"Bearer {tok}"))
    if try_cookies:
        candidates.append(("cookies", None))

    for cand in candidates:
        api.auth = cand
        status, body = api.request("GET", "profile_api")
        name = profile_name(body) if status == 200 else None
        if name:
            log(f"Вход подтверждён через {cand[0]}, профиль {name}")
            return name
        if status in (404, 405) or (status == 200 and not name):
            # профиль переехал/поменялся — проверяем вход по списку предметов
            st, data = api.request("GET", "items_api")
            if st == 200 and extract_list(data, "items") is not None:
                log(f"Вход подтверждён через {cand[0]} (по списку предметов)")
                return "аккаунт"
    api.auth = ("cookies", None)
    return None


def wait_for_login(api: Api, browser: Browser, timeout_s: float) -> str | None:
    deadline = time.time() + timeout_s
    n = 0
    while time.time() < deadline:
        if browser.proc and browser.proc.poll() is not None:
            print("Окно браузера закрыто.")
            return None
        browser.refresh()
        name = detect_login(api, browser, try_cookies=(n % 3 == 0))
        if name:
            return name
        n += 1
        time.sleep(3)
    return None


# ---------- логика ----------

_USERISH_RE = re.compile(r"user|inventor|/me(/|$)|/my(/|$)|account|profile|owned|mine", re.I)


def dict_lists(body, depth=0):
    """Все списки словарей внутри ответа."""
    if depth > 4:
        return
    if isinstance(body, list):
        if body and all(isinstance(x, dict) for x in body):
            yield body
    elif isinstance(body, dict):
        for v in body.values():
            yield from dict_lists(v, depth + 1)


def items_seen_by_site(captured: list[dict]) -> list[tuple[str, list]]:
    """Ваши предметы из ответов, которые получил сам сайт (страница «Продажа» и др.)."""
    found = []
    for cap in captured:
        if cap["method"] != "GET" or cap["status"] != 200:
            continue
        path = urlparse(cap["url"]).path
        if "sale" in path.lower() or not _USERISH_RE.search(path):
            continue
        try:
            body = json.loads(cap["body"])
        except ValueError:
            continue
        for lst in dict_lists(body):
            raw = expand_items(lst)
            good = [x for x in raw if norm_item(x)]
            if good and len(good) * 2 >= len(raw):
                found.append((cap["url"], good))
    return found


def load_inventory(api: Api, browser: Browser, cfg: dict):
    """Весь инвентарь и все ваши активные лоты."""
    print("Сканирую инвентарь...")
    items_error = None
    try:
        raw_items = expand_items(api.get_list("items_api", "items", "items"))
    except Exception as e:
        # старый адрес не отвечает — попробуем взять инвентарь со страницы сайта
        if not cfg["learn_from_site"]:
            raise
        items_error = e
        raw_items = []
        print(f"  Список предметов по старому адресу не получен ({short(e)}) — беру со страницы сайта.")

    if cfg["learn_from_site"]:
        if browser.open_sell_page():
            print("Открыл страницу «Продажа» — сверяю инвентарь с тем, что показывает сайт...")
            browser.wait_quiet(min_s=3, quiet_s=2.5, max_s=20)
            for _ in range(3):
                browser.scroll_down()
                time.sleep(1.5)
            browser.wait_quiet(min_s=1, quiet_s=2, max_s=10)
        else:
            log("Ссылку «Продажа» на странице не нашёл")
        ids = {str(it["item_id"]) for it in map(norm_item, raw_items) if it}
        for url, lst in items_seen_by_site(list(browser.captured)):
            new = [x for x in lst if str(norm_item(x)["item_id"]) not in ids]
            if new:
                ids.update(str(norm_item(x)["item_id"]) for x in new)
                raw_items.extend(new)
                print(f"  + {len(new)} шт. со страницы сайта ({urlparse(url).path})")
                log(f"Предметы со страницы сайта: {url} (+{len(new)})")

    if items_error is not None and not raw_items:
        raise items_error
    items, seen = [], set()
    for x in raw_items:
        it = norm_item(x)
        if it and str(it["item_id"]) not in seen:
            seen.add(str(it["item_id"]))
            items.append(it)
    if raw_items and not items:
        print("Сайт вернул предметы в незнакомом формате — подробности в логе.")
        log(f"Пример ответа: {json.dumps(raw_items[0], ensure_ascii=False)[:1500]}")

    sales = [s for s in map(norm_sale, api.get_list("sales_user_api", "sales", "sales/user")) if s]
    return items, [s for s in sales if sale_active(s)]


def plan_products(items: list[dict], my_sales: list[dict], cfg: dict):
    """product_id -> {name, items}: что (пере)выставляем; плюс что пропущено и почему."""
    listed = {str(s["item_id"]) for s in my_sales if s["item_id"] is not None}
    products, skipped = {}, Counter()
    for it in items:
        if str(it["item_id"]) not in listed:
            if it["blocked"] or status_blocked(it["status"], cfg):
                skipped[it["status"] or "заблокирован"] += 1
                continue
            if looks_listed(it["status"]):
                # числится на продаже, но лота среди ваших нет — снять его нечем
                skipped[f"{it['status']} без лота"] += 1
                continue
        products.setdefault(it["product_id"], {"name": it["name"], "items": []})["items"].append(it)
    return products, skipped


def _create_retry(api: Api, item: dict, price: float):
    for attempt in range(3):
        try:
            api.create_sale(item, price)
            return
        except ApiError as e:
            # 4xx — сайт отказал осознанно, повтор не поможет
            if attempt == 2 or (e.status is not None and e.status < 500 and e.status != 429):
                raise
        except Exception:
            if attempt == 2:
                raise
        time.sleep(1)


def target_price(peak: float, cfg: dict) -> float:
    # в копейках, чтобы не было ошибок округления
    peak_cents = round(peak * 100)
    min_cents = round(cfg["min_listing_price"] * 100)
    step = max(1, round(cfg["undercut"] * 100))
    return max(peak_cents - step, min_cents) / 100.0


def reprice_one(api: Api, cfg: dict, pid, name: str, prod_items: list[dict],
                sales_by_item: dict, my_item_ids: set, my_sale_ids: set):
    """Один товар: прочитать рынок -> снять свои лоты -> выставить все штуки
    по цене 5-го лота в топе (самого дорогого из топ-5) − 0.01. Возвращает (отчёт, изменено)."""
    top_n = int(cfg["top_n"])
    listed = [(it, sales_by_item[str(it["item_id"])]) for it in prod_items
              if str(it["item_id"]) in sales_by_item]
    sale_ids = [s["sale_id"] for _, s in listed]
    prev_prices = {str(it["item_id"]): s["price"] for it, s in listed}
    removed = len(sale_ids)
    count = len(prod_items)

    # а) рынок без своих лотов: берём с запасом на свои и выкидываем их.
    # Читаем ДО снятия, чтобы при ошибке чтения лоты не остались снятыми.
    best = api.get_best_sales(pid, count=top_n + removed)
    market = sorted(s["price"] for s in best
                    if str(s["sale_id"]) not in my_sale_ids and str(s["item_id"]) not in my_item_ids)[:top_n]
    if not market:
        return f"{name}: на рынке нет других продавцов -> оставил как было", False

    peak = market[-1]
    target = target_price(peak, cfg)
    top = f"{len(market)}-й в топе {peak:.2f}"

    if len(prev_prices) == count and all(
            p is not None and round(p * 100) == round(target * 100) for p in prev_prices.values()):
        return f"{name}: {top} -> {count} шт. уже стоят по {target:.2f}", False

    if cfg["dry_run"]:
        return f"{name}: [ТЕСТ] {top} -> снял бы {removed}, выставил бы {count} шт. по {target:.2f}", False

    # б) снимаем свои активные лоты этого товара
    if sale_ids:
        api.cancel_sales(sale_ids)
        time.sleep(0.4)

    # в) выставляем все штуки; ошибка на одной не мешает остальным
    failed = []
    for it in prod_items:
        try:
            _create_retry(api, it, target)
        except Exception as e:
            failed.append(e)
    if failed:
        raise RuntimeError(f"выставил {count - len(failed)} из {count} по {target:.2f}; {short(failed[0], 100)}")
    return f"{name}: {top} -> снял {removed}, выставил {count} шт. по {target:.2f}", True


def run_pricing(api: Api, browser: Browser, cfg: dict):
    t0 = time.time()
    items, my_sales = load_inventory(api, browser, cfg)
    statuses = Counter(it["status"] or "без статуса" for it in items)
    print(f"Предметов: {len(items)} ({', '.join(f'{k}: {v}' for k, v in statuses.most_common())}), "
          f"ваших лотов на продаже: {len(my_sales)}")
    log("Предметы (id | товар | название | статус):\n  " + "\n  ".join(
        f"{it['item_id']} | {it['product_id']} | {it['name']} | {it['status']}"
        f"{' | заблокирован' if it['blocked'] else ''}" for it in items))
    if items:
        log(f"Пример предмета: {json.dumps(items[0]['raw'], ensure_ascii=False)[:1500]}")
    if my_sales:
        log(f"Лоты: {[(s['sale_id'], s['item_id'], s['price']) for s in my_sales]}")

    products, skipped = plan_products(items, my_sales, cfg)
    if skipped:
        print("Пропускаю (выставлять нельзя): " + ", ".join(f"{k}: {v}" for k, v in skipped.items()))
    sales_by_item = {str(s["item_id"]): s for s in my_sales if s["item_id"] is not None}
    my_item_ids = {str(it["item_id"]) for it in items}
    my_sale_ids = {str(s["sale_id"]) for s in my_sales}

    total = len(products)
    workers = max(1, int(cfg["workers"]))
    print(f"Товаров к выставлению: {total} ({sum(len(p['items']) for p in products.values())} шт.). "
          f"Обрабатываю ({workers} параллельно): чтение цен -> снятие лотов -> выставление...\n")

    changed = errors = done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(reprice_one, api, cfg, pid, p["name"], p["items"],
                               sales_by_item, my_item_ids, my_sale_ids): p
                   for pid, p in products.items()}
        for fut in as_completed(futures):
            p = futures[fut]
            done += 1
            try:
                report, did_change = fut.result()
            except Exception as e:
                errors += 1
                log(f"{p['name']}: {traceback.format_exc()}")
                print(f"  [{done}/{total}] {p['name']}: ошибка ({short(e)})")
                continue
            if did_change:
                changed += 1
            print(f"  [{done}/{total}] {report}")

    print(f"\nГотово за {time.time() - t0:.1f} с. Переоценено товаров: {changed}. Ошибок: {errors}.")


def log_diagnostics(browser: Browser, api: Api | None):
    with browser._sniff_lock:
        headers = {h: sorted(v) for h, v in browser.sniffed.items()}
    log(f"Заголовки, которые сайт сам слал в API: {headers}")
    log("Запросы сайта к API:\n  " + "\n  ".join(
        f"{m} {u} -> {s}" for (m, u), s in sorted(list(browser.observed.items()), key=str)))
    if api:
        log(f"Итоговые адреса API: {api.endpoints}")
        log(f"Авторизация: {api.auth[0]}, fetch из вкладки: {browser.cred_mode or 'нет'}")
    # что сайт сам получал от своего API — по этому видно любые его изменения
    last = {}
    for cap in list(browser.captured):
        last[(cap["method"], cap["url"])] = cap
    for cap in list(last.values())[-80:]:
        log(f"Ответ API сайту: {cap['method']} {cap['url']} -> {cap['status']}\n  {cap['body'][:3000]}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="MM2 Pricer для dreampets.gg")
    parser.add_argument("--dry-run", action="store_true", help="только показать, ничего не менять")
    parser.add_argument("--config", default=CONFIG_PATH, help="путь к mm2pricer_config.json")
    parser.add_argument("--log", default=LOG_PATH, help="куда писать лог")
    parser.add_argument("--no-pause", action="store_true", help="не ждать Enter в конце")
    args, _ = parser.parse_known_args(argv)

    print(f"=== MM2 Pricer v{VERSION} ===")
    print("Одноразовый запуск: вход -> смена цен -> выход.\n")
    cfg = load_config(args.config)
    if args.dry_run:
        cfg["dry_run"] = True
    if cfg["dry_run"]:
        print("ТЕСТОВЫЙ РЕЖИМ: цены не меняются, только показываю, что было бы сделано.\n")
    open_log(args.log)
    log(f"MM2 Pricer v{VERSION}, Python {sys.version.split()[0]}, {sys.platform}")
    log(f"Настройки: {json.dumps(cfg, ensure_ascii=False)}")

    profile_dir = tempfile.mkdtemp(prefix="mm2pricer-")
    browser = Browser(cfg, profile_dir)
    api = None
    try:
        if not browser.launch():
            return

        print(f"\nОткрыл {cfg['login_url']} — войдите в свой аккаунт в окне браузера.")
        print("Жду вход в аккаунт...")
        api = Api(cfg, browser)
        user = wait_for_login(api, browser, cfg["auth_timeout_s"])
        if not user:
            print("Не дождался входа в аккаунт. Завершаюсь.")
            return

        print(f"Вход подтверждён ({user}). Не трогайте окно браузера до конца работы.\n")
        run_pricing(api, browser, cfg)
    except KeyboardInterrupt:
        print("\nПрервано.")
    except Exception as e:
        log(traceback.format_exc())
        print(f"\nОшибка: {short(e, 300)}")
    finally:
        log_diagnostics(browser, api)
        print("\nРазлогиниваюсь и закрываю браузер...")
        browser.close()
        print("Завершено.")
        if _log_file is not None:
            print(f"Подробный лог: {args.log}")
        if not args.no_pause:
            try:
                input("\nНажмите Enter, чтобы закрыть окно...")
            except Exception:
                pass


if __name__ == "__main__":
    main()
