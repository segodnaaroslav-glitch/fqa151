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
import math
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


VERSION = "2.3"

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

    # новый маркет ({user_id} подставляется из профиля)
    "new_market": True,
    "inventory_api": "https://mm2-test.dreampets.gg/api/inventory/v1/users/{user_id}/items",
    "inventory_gates_api": "https://mm2-test.dreampets.gg/api/inventory/v1/users/{user_id}/account-inventory-gates",
    "market_sales_user_api": "https://mm2-test.dreampets.gg/api/market/v1/users/{user_id}/sales",
    # сколько ждать, пока вы один раз покажете выставление на новом маркете
    "teach_timeout_s": 900,

    # режим: ask — спросить при запуске, once — один проход, watch — следить постоянно
    "mode": "ask",
    # пауза между проверками рынка в режиме слежения, секунд
    "watch_interval_s": 60,
    # защита от ценовой войны: за один запуск цена товара не опускается ниже,
    # чем на столько процентов от цены, выставленной на первой проверке (-1 — без защиты)
    "max_drop_percent": 20,

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
LEARNED_PATH = os.path.join(app_dir(), "mm2pricer_learned.json")


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
_log_size = 0
_LOG_LIMIT = 50_000_000      # при слежении лог не растёт бесконечно
_log_quiet = False           # в повторных проверках не пишем тела удачных GET-запросов


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def redact(text: str) -> str:
    text = _TOKEN_FIELD_RE.sub(r'\1"<token>"', _JWT_RE.sub("<token>", text))
    return _EMAIL_RE.sub("<email>", _BEARER_RE.sub(r"\1<token>", text))


def open_log(path: str):
    global _log_file
    try:
        _log_file = open(path, "w", encoding="utf-8")
    except Exception as e:
        print(f"Не смог создать лог {path}: {e}")


def log(msg):
    global _log_size
    if _log_file is None:
        return
    line = f"{time.strftime('%H:%M:%S')} {redact(str(msg))}\n"
    with _log_lock:
        try:
            if _log_size + len(line) > _LOG_LIMIT:
                _log_file.seek(0)
                _log_file.truncate()
                _log_size = 0
                _log_file.write(f"{time.strftime('%H:%M:%S')} (начало лога обрезано — он стал слишком большим)\n")
            _log_file.write(line)
            _log_file.flush()
            _log_size += len(line)
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
    seller = raw.get("seller") if isinstance(raw.get("seller"), dict) else {}
    return {
        "sale_id": sale_id,
        "item_id": first(raw, "item_id", "itemId") or first(item, "item_id", "itemId", "id", "_id"),
        "product_id": first(raw, "product_id", "productId") or first(product, "product_id", "productId", "id"),
        "price": to_price(first(raw, "price", "amount", "cost")),
        "status": str(raw.get("status") or raw.get("state") or "").strip().lower(),
        "user_id": first(raw, "user_id", "userId", "seller_id", "sellerId") or first(seller, "user_id", "id"),
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


def _profile_user(body) -> dict | None:
    if not isinstance(body, dict):
        return None
    data = body.get("data")
    variants = [body, data, body.get("user"), body.get("profile")]
    if isinstance(data, dict):
        variants.append(data.get("user"))
    for v in variants:
        if first(v, "user_id", "userId", "id", "_id", "uid") is not None:
            return v
    return None


def profile_name(body) -> str | None:
    """Ник (или id) из ответа профиля; None — если это не профиль."""
    user = _profile_user(body)
    if user is None:
        return None
    uid = first(user, "user_id", "userId", "id", "_id", "uid")
    return str(first(user, "username", "name", "nickname", "login", "display_name") or uid)


def profile_uid(body) -> str | None:
    user = _profile_user(body)
    uid = first(user, "user_id", "userId", "id", "_id", "uid") if user else None
    return None if uid is None else str(uid)


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
        self._seq = 0
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
        deadline = time.time() + 45
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
            self._seq += 1
            # [метод, адрес, статус, тип ответа, тело запроса, есть ли тело, порядковый номер]
            self._req_meta[rid] = [req.get("method", "GET"), url, None, "", req.get("postData"),
                                   bool(req.get("hasPostData")), self._seq]
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
                if "json" in meta[3] or not meta[3] or meta[0] != "GET":
                    # тело ответа сайта — чтобы увидеть, откуда сайт берёт инвентарь.
                    # Запрашиваем в отдельном потоке: этот поток читает ответы CDP.
                    t = threading.Thread(target=self._grab_body, args=(sess, rid, list(meta)), daemon=True)
                    self._body_threads.append(t)
                    t.start()
        elif method == "Network.loadingFailed":
            # так бывает с ответом 204 без тела, если страница его не дочитала:
            # ответ получен, запрос выполнен — для изменяющих запросов это важно
            meta = self._req_meta.get(rid)
            if meta and meta[2] is not None and meta[0] != "GET":
                self.last_api_event = time.time()
                t = threading.Thread(target=self._grab_body, args=(sess, rid, list(meta)), daemon=True)
                self._body_threads.append(t)
                t.start()

    def _grab_body(self, sess: CDPSession, rid: str, meta: list):
        post = meta[4]
        if post is None and meta[5]:
            try:
                post = sess.call("Network.getRequestPostData", {"requestId": rid}, timeout=10).get("postData")
            except Exception:
                post = None
        try:
            res = sess.call("Network.getResponseBody", {"requestId": rid}, timeout=15)
            body = res.get("body", "")
            if res.get("base64Encoded"):
                body = base64.b64decode(body).decode("utf-8", "replace")
        except Exception:
            body = ""          # например, 204 без тела
            if meta[0] == "GET":
                return
        self._req_meta.pop(rid, None)
        if len(body) <= 5_000_000:
            self.captured.append({"method": meta[0], "url": meta[1], "status": meta[2], "body": body,
                                  "post": post, "seq": meta[6]})
            if len(self.captured) > 1500:        # при долгом слежении не копим бесконечно
                del self.captured[:500]

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

    def reload(self):
        """Перезагружает вкладку сайта — сайт сам обновит вход (новый токен)."""
        sess = self.primary(site_only=True)
        if sess is None:
            self.refresh()
            sess = self.primary(site_only=True)
        if sess is not None:
            try:
                sess.call("Page.reload", {"ignoreCache": True}, timeout=15)
            except Exception as e:
                log(f"Page.reload: {e}")

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
        self.user_id = None
        self.logged_in = False
        self._auth_gen = 0                   # растёт при каждом восстановлении входа
        self._reauth_lock = threading.Lock()
        self._local = threading.local()

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

    def _send(self, method, url, params=None, body=None, timeout=20, raw: str | None = None):
        full = add_params(url, params)
        data = ctype = None
        if body is not None:
            data, ctype = json.dumps(body), "application/json"
        elif raw is not None:
            data = raw
            ctype = "application/json" if raw.lstrip()[:1] in "{[" else "application/x-www-form-urlencoded"

        def attempt_all():
            headers = self.headers_for(url)
            if ctype:
                headers["content-type"] = ctype
            for attempt in range(4):
                status, text = self._transport(method, full, headers, data, timeout)
                retry = status == 429 or (method == "GET" and status in (502, 503, 504))
                if not retry or attempt == 3:
                    return status, text
                time.sleep(1.5 * 2 ** attempt)

        gen = self._auth_gen
        status, text = attempt_all()
        if status in (401, 403) and self.logged_in and not getattr(self._local, "busy", False):
            # возможно, истёк вход — восстанавливаем и повторяем один раз
            if self.reauth(gen):
                status, text = attempt_all()
        if not _log_quiet or method != "GET" or not _ok(status):
            log(f"{method} {full} {data or ''} -> {status} {text[:2000]!r}")
        try:
            return status, json.loads(text) if text else None
        except ValueError:
            return status, text

    def reauth(self, seen_gen: int) -> bool:
        """Сайт разлогинил (истёк токен): обновляем вкладку, а если не помогло — просим войти снова."""
        with self._reauth_lock:
            if self._auth_gen != seen_gen:
                return True                      # пока ждали, другой поток уже восстановил вход
            self._local.busy = True
            try:
                status, body = self._send("GET", self.endpoints["profile_api"])
                if status == 200 and profile_name(body):
                    return False                 # вход на месте — отказ 401/403 был по делу
                print(f"  [{time.strftime('%H:%M:%S')}] Сайт разлогинил — обновляю страницу...")
                log("Вход слетел — перезагружаю вкладку сайта")
                self.browser.reload()
                self.browser.wait_quiet(min_s=3, quiet_s=2, max_s=20)
                name = detect_login(self, self.browser, try_cookies=True)
                if not name:
                    print("Сессия на сайте закончилась — войдите в аккаунт ещё раз в окне браузера.")
                    name = wait_for_login(self, self.browser, self.cfg["auth_timeout_s"])
                if not name:
                    return False
                self._auth_gen += 1
                print(f"  [{time.strftime('%H:%M:%S')}] Вход восстановлен ({name}).")
                return True
            finally:
                self._local.busy = False

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
            # ищем заново, как только сайт сделал новые запросы; если в прошлый раз
            # вариантов не было вовсе (код сайта ещё грузился) — через 5 с, иначе через минуту
            seen = len(self.browser.observed)
            last_at, last_seen, last_found = self._discover_at.get(key, (0, -1, 0))
            if seen == last_seen and time.time() - last_at < (60 if last_found else 5):
                return status, data
            cands = endpoint_alternatives(url, self.known_paths(), self.browser.domain)[:4]
            self._discover_at[key] = (time.time(), seen, len(cands))
            for cand in cands:
                st, dt = self._send(method, cand, params, body)
                if st != 404:
                    self.endpoints[key] = cand
                    print(f"  (адрес API изменился: {url} -> {cand})")
                    log(f"Новый адрес {key}: {cand}")
                    return st, dt
        return status, data

    def reset_discovery(self):
        with self._discover_lock:
            self._discover_at.clear()

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
            api.user_id = profile_uid(body)
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


# ---------- новый маркет: выставление «по образцу» ----------
#
# Адреса и формат запросов выставления/снятия на новом маркете в коде программы
# не зашиты: программа один раз смотрит, как это делает сам сайт, когда вы
# выставляете и снимаете предмет руками, и повторяет те же запросы для других
# предметов и цен. Выученное хранится в mm2pricer_learned.json.

_PH_RE = re.compile(r"\{\{(\w+)\}\}")


def _ok(status) -> bool:
    return isinstance(status, int) and 200 <= status < 300


def _subst_text(text: str, mapping: dict) -> str:
    for name, value in sorted(mapping.items(), key=lambda kv: -len(str(kv[1] or ""))):
        if value not in (None, ""):
            text = text.replace(str(value), "{{%s}}" % name)
    return text


def _template_json(obj, mapping: dict, prices: list):
    """Значения из mapping -> {{имя}}, поле цены -> {{price}} (его исходное значение — в prices)."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if ("price" in k.lower() and not isinstance(v, (bool, dict, list))
                    and to_price(v) is not None):
                prices.append(v)
                out[k] = "{{price}}"
            else:
                out[k] = _template_json(v, mapping, prices)
        return out
    if isinstance(obj, list):
        return [_template_json(v, mapping, prices) for v in obj]
    if isinstance(obj, (str, int)) and not isinstance(obj, bool):
        for name, value in mapping.items():
            if value not in (None, "") and str(obj) == str(value):
                return "{{%s}}" % name
    return obj


def _fill(obj, values: dict):
    if isinstance(obj, dict):
        return {k: _fill(v, values) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_fill(v, values) for v in obj]
    if isinstance(obj, str):
        m = _PH_RE.fullmatch(obj)
        if m and m.group(1) in values:
            return values[m.group(1)]
        return _PH_RE.sub(lambda mm: str(values.get(mm.group(1), mm.group(0))), obj)
    return obj


def _make_template(cap: dict, mapping: dict) -> dict:
    tpl = {"method": cap["method"], "url": _subst_text(cap["url"], mapping), "body": None, "kind": None}
    post = cap.get("post")
    if post:
        prices = []
        try:
            tpl["body"], tpl["kind"] = _template_json(json.loads(post), mapping, prices), "json"
        except ValueError:
            tpl["body"], tpl["kind"] = _subst_text(post, mapping), "text"
        if prices:
            v = prices[0]
            tpl["price_type"] = "str" if isinstance(v, str) else "num"
            tpl["demo_price"] = to_price(v)
    return tpl


def _has(tpl: dict, name: str) -> bool:
    return "{{%s}}" % name in tpl["url"] + json.dumps(tpl["body"], ensure_ascii=False)


def _json_or_none(text):
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


def _sales_in(body) -> list[dict]:
    return [s for lst in dict_lists(body) for s in map(norm_sale, lst) if s and s["price"] is not None]


def learn_market_ops(captured: list[dict], items_by_id: dict, product_ids: set, uid: str | None) -> dict:
    """Ищет в запросах сайта: список лотов товара, выставление и снятие лота."""
    caps = sorted(captured, key=lambda c: c.get("seq", 0))
    ops = {}

    # выставление: первый POST/PUT с id вашего предмета и полем цены
    create_cap = demo = None
    for cap in caps:
        if cap["method"] not in ("POST", "PUT") or not _ok(cap["status"]):
            continue
        text = cap["url"] + "\n" + (cap.get("post") or "")
        found = [i for i in items_by_id if i in text]
        if len(found) != 1:
            # выставили сразу несколько штук — по такому образцу повторять нельзя
            if len(found) > 1:
                log(f"Выставление сразу {len(found)} шт. — для образца нужно выставить одну")
            continue
        iid = found[0]
        it = items_by_id[iid]
        mapping = {"item_id": it["item_id"], "product_id": it["product_id"],
                   "account_id": find_field(it["raw"], "account_id"), "user_id": uid}
        tpl = _make_template(cap, mapping)
        if tpl.get("price_type") and _has(tpl, "item_id"):
            create_cap, demo = cap, it
            # во сколько раз цена в запросе больше цены лота (копейки?)
            tpl["price_scale"] = 1
            demo_sale = next((s for later in caps if later.get("seq", 0) > cap.get("seq", 0)
                              for s in _sales_in(_json_or_none(later["body"]))
                              if str(s["item_id"]) == iid), None)
            if demo_sale and demo_sale["price"] and tpl.get("demo_price"):
                if 90 < tpl["demo_price"] / demo_sale["price"] < 110:
                    tpl["price_scale"] = 100
            ops["create"] = tpl
            break

    # снятие: следующий изменяющий запрос с id этого лота или предмета
    if create_cap:
        iid = str(demo["item_id"])
        sale_ids = set()
        created = _json_or_none(create_cap["body"])
        for d in [created] + [x for lst in dict_lists(created) for x in lst]:
            if isinstance(d, dict):
                for k in ("sale_id", "saleId", "id"):
                    if isinstance(d.get(k), (str, int)) and str(d[k]) != iid:
                        sale_ids.add(str(d[k]))
        for cap in caps:
            for s in _sales_in(_json_or_none(cap["body"])):
                if str(s["item_id"]) == iid:
                    sale_ids.add(str(s["sale_id"]))
        for cap in caps:
            if (cap.get("seq", 0) <= create_cap.get("seq", 0) or cap["method"] == "GET"
                    or not _ok(cap["status"])):
                continue
            text = cap["url"] + "\n" + (cap.get("post") or "")
            sids = [s for s in sale_ids if s in text]
            others = [i for i in items_by_id if i in text and i != iid]
            if len(sids) > 1 or others:
                continue    # сняли сразу несколько лотов — не годится как образец
            sid = sids[0] if sids else None
            if sid or iid in text:
                mapping = {"sale_id": sid, "item_id": demo["item_id"], "product_id": demo["product_id"],
                           "account_id": find_field(demo["raw"], "account_id"), "user_id": uid}
                tpl = _make_template(cap, mapping)
                if _has(tpl, "sale_id") or _has(tpl, "item_id"):
                    ops["cancel"] = tpl
                    break

    # список лотов товара: GET с id товара в адресе, в ответе — лоты с ценами
    for cap in reversed(caps):
        if cap["method"] != "GET" or cap["status"] != 200 or (uid and uid in cap["url"]):
            continue
        pid = next((p for p in product_ids if p in cap["url"]), None)
        if pid and _sales_in(_json_or_none(cap["body"])):
            ops["best"] = {"method": "GET", "url": cap["url"].replace(pid, "{{product_id}}"),
                           "body": None, "kind": None}
            break
    return ops


def _origin(url: str) -> str:
    u = urlparse(url)
    return f"{u.scheme}://{u.netloc}"


def relative_to_market(ops: dict, cfg: dict) -> dict:
    """Адрес сервера нового маркета в выученных запросах -> {{market_origin}}:
    выученное не зависит от того, на каком домене сейчас новый маркет."""
    origin = _origin(cfg["inventory_api"])
    for tpl in ops.values():
        if tpl["url"].startswith(origin + "/"):
            tpl["url"] = "{{market_origin}}" + tpl["url"][len(origin):]
    return ops


def load_learned(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        ops = data.get("ops") or {}
        return ops if all(k in ops for k in ("best", "create", "cancel")) else None
    except Exception:
        return None


def save_learned(path: str, ops: dict):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "saved": time.strftime("%Y-%m-%d %H:%M:%S"), "ops": ops},
                      f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"Не смог сохранить {path}: {e}")


def forget_learned(path: str):
    try:
        os.remove(path)
    except OSError:
        pass


def enter_pressed() -> bool:
    """Нажат ли Enter в окне программы (не блокирует)."""
    try:
        import msvcrt
        hit = False
        while msvcrt.kbhit():
            if msvcrt.getwch() in ("\r", "\n"):
                hit = True
        return hit
    except ImportError:
        pass
    try:
        import select
        if sys.stdin and sys.stdin.isatty() and select.select([sys.stdin], [], [], 0)[0]:
            sys.stdin.readline()
            return True
    except Exception:
        pass
    return False


_TEACH_STEPS = (("best", "вижу лоты товара"), ("create", "вижу выставление"), ("cancel", "вижу снятие"))


def teach_new_market(api: Api, browser: Browser, cfg: dict, new_items: list[dict]) -> dict | None:
    """Ждёт, пока вы один раз выставите и снимете предмет на новом маркете, и запоминает как."""
    items_by_id = {str(it["item_id"]): it for it in new_items}

    def product_ids() -> set:
        ids = {str(it["product_id"]) for it in new_items}
        for cap in list(browser.captured):
            for lst in dict_lists(_json_or_none(cap["body"]) if cap["method"] == "GET" else None):
                ids.update(str(first(d, "product_id", "productId")) for d in lst
                           if first(d, "product_id", "productId") is not None)
        return ids

    def learn() -> dict:
        return learn_market_ops(list(browser.captured), items_by_id, product_ids(), api.user_id)

    ops = learn()
    if all(k in ops for k, _ in _TEACH_STEPS):
        print("Новый маркет: увидел, как сайт выставляет и снимает лоты. Запомнил, как выставлять на новом маркете.")
        return ops
    api.known_paths()   # адреса API из кода сайта — в лог, на случай если что-то пойдёт не так
    minutes = max(1, int(cfg["teach_timeout_s"]) // 60)
    print("\nНовый маркет: я ещё не знаю, как сайт выставляет и снимает лоты. Покажите один раз:\n"
          "  1) в окне браузера откройте страницу любого товара на маркете (где видно лоты продавцов);\n"
          "  2) выставьте на продажу ОДИН любой свой предмет (одну штуку), по любой цене;\n"
          "  3) сразу снимите этот лот с продажи.\n"
          "Я запомню, как это делает сайт, и дальше буду делать сам.\n"
          f"Жду до {minutes} мин. Чтобы пропустить новый маркет — нажмите Enter.")
    shown = set()
    deadline = time.time() + float(cfg["teach_timeout_s"])
    while time.time() < deadline:
        if enter_pressed():
            break
        time.sleep(1)
        browser.refresh()
        ops = learn()
        for key, label in _TEACH_STEPS:
            if key in ops and key not in shown:
                shown.add(key)
                print(f"  ✓ {label}")
        if len(shown) == len(_TEACH_STEPS):
            break
    browser.wait_quiet(min_s=1, quiet_s=1.5, max_s=8)
    ops = learn()
    missing = [label for key, label in _TEACH_STEPS if key not in ops]
    if missing:
        print(f"Не увидел: {', '.join(missing)}. Новый маркет в этот раз пропускаю.")
        log(f"Обучение не завершено, выучено: {json.dumps(ops, ensure_ascii=False)}")
        return None
    print("Запомнил, как выставлять на новом маркете.")
    return ops


class OldMarket:
    """Старый маркет (mm2.dreampets.gg) — адреса известны."""
    tag = " (старый маркет)"

    def __init__(self, api: Api):
        self.api = api

    def best(self, pid, count):
        return self.api.get_best_sales(pid, count)

    def cancel(self, listed):
        self.api.cancel_sales([s["sale_id"] for _, s in listed])

    def create(self, item, price):
        _retry(lambda: self.api.create_sale(item, price))


class NewMarket:
    """Новый маркет — запросы повторяются по образцу, выученному у сайта."""
    tag = ""

    def __init__(self, api: Api, ops: dict, cfg: dict, learned_path: str):
        self.api, self.ops, self.cfg, self.learned_path = api, ops, cfg, learned_path
        self.sales_url = cfg["market_sales_user_api"].replace("{user_id}", api.user_id or "")
        self._first = threading.Lock()
        self.verified = False
        self.stopped = None
        self._worked = set()
        self._missed = Counter()

    def _values(self, item=None, sale=None, pid=None, price=None) -> dict:
        v = {"user_id": self.api.user_id, "market_origin": _origin(self.cfg["inventory_api"])}
        if item is not None:
            v.update(item_id=item["item_id"], product_id=item["product_id"],
                     account_id=find_field(item["raw"], "account_id"))
        if sale is not None:
            v["sale_id"] = sale["sale_id"]
        if pid is not None:
            v["product_id"] = pid
        if price is not None:
            tpl = self.ops["create"]
            amount = price * tpl.get("price_scale", 1)
            if tpl.get("price_scale", 1) != 1:
                amount = int(round(amount))
                v["price"] = str(amount) if tpl.get("price_type") == "str" else amount
            else:
                v["price"] = f"{amount:.2f}" if tpl.get("price_type") == "str" else round(amount, 2)
        return {k: val for k, val in v.items() if val is not None}

    def _call(self, name: str, values: dict, what: str):
        tpl = self.ops[name]
        text_values = {k: str(v) for k, v in values.items()}
        url = _fill(tpl["url"], text_values)
        body = raw = None
        if tpl.get("body") is not None:
            if tpl.get("kind") == "json":
                body = _fill(tpl["body"], values)
            else:
                raw = _fill(tpl["body"], text_values)
        try:
            status, data = self.api._send(tpl["method"], url, body=body, raw=raw)
        except Exception:
            self._miss(name)
            raise
        if _ok(status):
            self._worked.add(name)
        elif status in (404, 405):
            self._miss(name)
        if not _ok(status):
            raise Api._fail(what, status, data)
        return data

    def _miss(self, name: str):
        # запрос ни разу не сработал и трижды «не найден»/недоступен — сайт поменял API;
        # при следующем запуске выучим заново
        if name in self._worked:
            return
        self._missed[name] += 1
        if self._missed[name] >= 3 and not self.stopped:
            forget_learned(self.learned_path)
            self.stopped = ("сайт изменил API нового маркета — при следующем запуске "
                            "покажите выставление ещё раз")

    def best(self, pid, count):
        if self.stopped:
            raise ApiError(self.stopped)
        data = self._call("best", self._values(pid=pid), "лоты товара")
        return sorted(_sales_in(data), key=lambda s: s["price"])

    def cancel(self, listed):
        for item, sale in listed:
            self._call("cancel", self._values(item=item, sale=sale), "снятие лота")

    def create(self, item, price):
        if self.stopped:
            raise ApiError(self.stopped)
        with self._first:
            if self.stopped:
                raise ApiError(self.stopped)
            if not self.verified:
                # первый лот — проверяем на сайте, что цена встала именно та
                _retry(lambda: self._call("create", self._values(item=item, price=price), "выставление"))
                problem = self._verify(item, price)
                if problem:
                    self.stopped = problem
                    raise ApiError(problem)
                self.verified = True
                return
        _retry(lambda: self._call("create", self._values(item=item, price=price), "выставление"))

    def _verify(self, item, price) -> str | None:
        for _ in range(3):
            time.sleep(1)
            status, data = self.api._send("GET", self.sales_url)
            if not _ok(status):
                continue
            sale = next((s for s in _sales_in(data) if str(s["item_id"]) == str(item["item_id"])), None)
            if sale is None:
                continue
            if abs(sale["price"] - price) < 0.005:
                log(f"Проверка первого лота: {item['name']} выставлен по {sale['price']:.2f} — верно")
                return None
            forget_learned(self.learned_path)
            return (f"проверка не прошла: просил {price:.2f}, на сайте {sale['price']:.2f} — "
                    f"новый маркет остановлен")
        return "проверка не прошла: выставленный лот не нашёлся среди ваших — новый маркет остановлен"


def _retry(fn):
    """Повторяет при сбое сети и ошибках сервера 5xx/429; отказ 4xx не повторяем."""
    for attempt in range(3):
        try:
            return fn()
        except ApiError as e:
            if attempt == 2 or (e.status is not None and e.status < 500 and e.status != 429):
                raise
        except Exception:
            if attempt == 2:
                raise
        time.sleep(1)


# ---------- инвентарь ----------

def _user_url(api: Api, cfg: dict, key: str) -> str | None:
    return cfg[key].replace("{user_id}", api.user_id) if api.user_id else None


def _safe_get(api: Api, url: str):
    try:
        return api._send("GET", url)
    except Exception as e:
        log(f"GET {url}: {e}")
        return None, None


def _captured_list(browser: Browser, path_re: str, field: str) -> list | None:
    """Последний список, который сам сайт получил с подходящего адреса."""
    for cap in reversed(list(browser.captured)):
        if cap["method"] == "GET" and cap["status"] == 200 and re.search(path_re, urlparse(cap["url"]).path):
            lst = extract_list(_json_or_none(cap["body"]), field)
            if lst is not None:
                return lst
    return None


def load_inventory(api: Api, browser: Browser, cfg: dict, open_sell_page: bool = True,
                   use_site_items: bool = True, verbose: bool = True):
    """Весь инвентарь и все ваши активные лоты на обоих маркетах.
    Возвращает (предметы, лоты, замороженные аккаунты)."""
    if verbose:
        print("Сканирую инвентарь..." if open_sell_page else "Перечитываю инвентарь и лоты...")
    api.reset_discovery()   # страница уже загрузилась — прежние неудачные поиски не в счёт

    # старый маркет
    old_error = None
    try:
        old_raw = expand_items(api.get_list("items_api", "items", "items"))
    except Exception as e:
        old_error, old_raw = e, []
        log(f"Старый маркет: список предметов не получен: {e}")

    if cfg["learn_from_site"] and open_sell_page:
        if browser.open_sell_page():
            if verbose:
                print("Открыл страницу «Продажа» — сверяю инвентарь с тем, что показывает сайт...")
            browser.wait_quiet(min_s=3, quiet_s=2.5, max_s=20)
            for _ in range(3):
                browser.scroll_down()
                time.sleep(1.5)
            browser.wait_quiet(min_s=1, quiet_s=2, max_s=10)
        else:
            log("Ссылку «Продажа» на странице не нашёл")

    # новый маркет: инвентарь по известному адресу, иначе — то, что сайт показал сам
    new_raw = []
    if cfg["new_market"] and api.user_id:
        url = _user_url(api, cfg, "inventory_api")
        status, body = _safe_get(api, url)
        if status == 200 and extract_list(body, "items") is not None:
            new_raw = expand_items(api.collect_pages(url, "items", body))
        else:
            log(f"Новый маркет: инвентарь по {url} не получен ({status})")
    if cfg["learn_from_site"] and use_site_items:
        known = {str(it["item_id"]) for it in map(norm_item, old_raw + new_raw) if it}
        for url, lst in items_seen_by_site(list(browser.captured)):
            extra = [x for x in lst if str(norm_item(x)["item_id"]) not in known]
            if extra:
                known.update(str(norm_item(x)["item_id"]) for x in extra)
                # служба inventory — это новый маркет; остальное — старый
                (new_raw if "/inventory/" in urlparse(url).path else old_raw).extend(extra)
                log(f"Предметы со страницы сайта: {url} (+{len(extra)})")
    if old_error is not None and not new_raw:
        raise old_error

    items, seen = [], set()
    for market, raw_list in (("old", old_raw), ("new", new_raw)):
        for x in raw_list:
            it = norm_item(x)
            if it and str(it["item_id"]) not in seen:
                seen.add(str(it["item_id"]))
                it["market"] = market
                items.append(it)
    if (old_raw or new_raw) and not items and verbose:
        print("Сайт вернул предметы в незнакомом формате — подробности в логе.")
        log(f"Пример ответа: {json.dumps((old_raw or new_raw)[0], ensure_ascii=False)[:1500]}")

    # ваши лоты
    sales = []
    if old_raw or old_error is None:
        for s in map(norm_sale, api.get_list("sales_user_api", "sales", "sales/user")):
            if s:
                s["market"] = "old"
                sales.append(s)
    frozen = {}
    if any(it["market"] == "new" for it in items):
        url = _user_url(api, cfg, "market_sales_user_api")
        lst = None
        if url:
            status, body = _safe_get(api, url)
            if status == 200 and extract_list(body, "sales") is not None:
                lst = api.collect_pages(url, "sales", body)
        if lst is None:
            lst = _captured_list(browser, r"/market/.*/users/[^/]+/sales$", "sales")
        if lst is None:
            raise RuntimeError("не удалось получить ваши лоты на новом маркете")
        for s in map(norm_sale, lst):
            if s:
                s["market"] = "new"
                sales.append(s)
        # аккаунты Roblox, с которых сейчас нельзя торговать (заморожены)
        url = _user_url(api, cfg, "inventory_gates_api")
        gates = None
        if url:
            status, body = _safe_get(api, url)
            gates = extract_list(body, "gates") if status == 200 else None
        if gates is None:
            gates = _captured_list(browser, r"/inventory/.*/account-inventory-gates$", "gates") or []
        for g in gates:
            if isinstance(g, dict) and g.get("tradable") is False and g.get("account_id"):
                acc = g.get("account") if isinstance(g.get("account"), dict) else {}
                frozen[str(g["account_id"])] = str(first(acc, "roblox_display_name", "roblox_name")
                                                   or g["account_id"])
    return items, [s for s in sales if sale_active(s)], frozen


def plan_products(items: list[dict], my_sales: list[dict], cfg: dict, frozen: dict | None = None):
    """(маркет, product_id) -> {name, items}: что (пере)выставляем; плюс что пропущено и почему."""
    frozen = frozen or {}
    listed = {str(s["item_id"]) for s in my_sales if s["item_id"] is not None}
    products, skipped = {}, Counter()
    for it in items:
        account = find_field(it["raw"], "account_id")
        if account is not None and str(account) in frozen:
            skipped[f"аккаунт {frozen[str(account)]} заморожен"] += 1
            continue
        if str(it["item_id"]) not in listed:
            if it["blocked"] or status_blocked(it["status"], cfg):
                skipped[it["status"] or "заблокирован"] += 1
                continue
            if looks_listed(it["status"]):
                # числится на продаже, но лота среди ваших нет — снять его нечем
                skipped[f"{it['status']} без лота"] += 1
                continue
        key = (it.get("market", "old"), it["product_id"])
        products.setdefault(key, {"name": it["name"], "items": []})["items"].append(it)
    return products, skipped


def target_price(peak: float, cfg: dict) -> float:
    # в копейках, чтобы не было ошибок округления
    peak_cents = round(peak * 100)
    min_cents = round(cfg["min_listing_price"] * 100)
    step = max(1, round(cfg["undercut"] * 100))
    return max(peak_cents - step, min_cents) / 100.0


_floor_lock = threading.Lock()


def apply_floor(target: float, key, floors: dict | None, cfg: dict) -> tuple[float, str]:
    """Защита от ценовой войны: первая цена товара за запуск — точка отсчёта,
    ниже неё на max_drop_percent процентов не опускаемся."""
    pct = cfg.get("max_drop_percent")
    if floors is None or pct is None or pct < 0:
        return target, ""
    with _floor_lock:
        floor = floors.get(key)
        if floor is None:
            floor = floors[key] = math.ceil(round(target * (100 - pct), 4)) / 100.0
    if target < floor:
        return floor, f" (держу нижнюю границу {floor:.2f}: −{pct:g}% от начальной)"
    return target, ""


def reprice_one(market, cfg: dict, pid, name: str, prod_items: list[dict],
                sales_by_item: dict, my_item_ids: set, my_sale_ids: set, my_uid: str | None = None,
                floors: dict | None = None, floor_key=None):
    """Один товар: прочитать рынок -> снять свои лоты -> выставить все штуки
    по цене 5-го лота в топе (самого дорогого из топ-5) − 0.01. Возвращает (отчёт, изменено)."""
    top_n = int(cfg["top_n"])
    listed = [(it, sales_by_item[str(it["item_id"])]) for it in prod_items
              if str(it["item_id"]) in sales_by_item]
    prev_prices = {str(it["item_id"]): s["price"] for it, s in listed}
    removed = len(listed)
    count = len(prod_items)

    # а) рынок без своих лотов: берём с запасом на свои и выкидываем их.
    # Читаем ДО снятия, чтобы при ошибке чтения лоты не остались снятыми.
    best = market.best(pid, count=top_n + removed)
    prices = sorted(s["price"] for s in best
                    if str(s["sale_id"]) not in my_sale_ids and str(s["item_id"]) not in my_item_ids
                    and not (my_uid and str(s.get("user_id")) == my_uid))[:top_n]
    if not prices:
        return f"{name}: на рынке нет других продавцов -> оставил как было", False

    peak = prices[-1]
    target, held = apply_floor(target_price(peak, cfg), floor_key, floors, cfg)
    top = f"{len(prices)}-й в топе {peak:.2f}"

    if len(prev_prices) == count and all(
            p is not None and round(p * 100) == round(target * 100) for p in prev_prices.values()):
        return f"{name}: {top} -> {count} шт. уже стоят по {target:.2f}{held}", False

    if cfg["dry_run"]:
        return (f"{name}: [ТЕСТ] {top} -> снял бы {removed}, выставил бы {count} шт. "
                f"по {target:.2f}{held}"), False

    # б) снимаем свои активные лоты этого товара
    if listed:
        market.cancel(listed)
        time.sleep(0.4)

    # в) выставляем все штуки; ошибка на одной не мешает остальным
    failed = []
    for it in prod_items:
        try:
            market.create(it, target)
        except Exception as e:
            failed.append(e)
    if failed:
        raise RuntimeError(f"выставил {count - len(failed)} из {count} по {target:.2f}; {short(failed[0], 120)}")
    return f"{name}: {top} -> снял {removed}, выставил {count} шт. по {target:.2f}{held}", True


def _summary(items: list[dict], sales: list[dict], market: str) -> str:
    its = [it for it in items if it.get("market") == market]
    n_products = len({it["product_id"] for it in its})
    n_sales = sum(1 for s in sales if s.get("market") == market)
    return f"{len(its)} шт., товаров: {n_products}, ваших лотов: {n_sales}"


def _now() -> str:
    return time.strftime("%H:%M:%S")


def run_pass(api: Api, browser: Browser, cfg: dict, learned_path: str, markets: dict,
             first: bool, floors: dict | None, last_reports: dict, number: int):
    """Одна проверка: инвентарь -> рынок -> перевыставление. Первая — подробно, остальные — только изменения."""
    t0 = time.time()
    items, my_sales, frozen = load_inventory(api, browser, cfg, open_sell_page=first,
                                             use_site_items=first, verbose=first)
    new_items = [it for it in items if it["market"] == "new"]

    # новый маркет: способ выставления — из файла, иначе учимся у вас (только на первой проверке)
    if first and new_items:
        ops = load_learned(learned_path)
        if ops:
            print("Новый маркет: выставляю так, как запомнил раньше (mm2pricer_learned.json).")
        else:
            ops = teach_new_market(api, browser, cfg, new_items)
            if ops:
                ops = relative_to_market(ops, cfg)
                save_learned(learned_path, ops)
                log(f"Выучено для нового маркета: {json.dumps(ops, ensure_ascii=False)}")
                # пока вы показывали, состояние лотов могло поменяться — читаем заново
                items, my_sales, frozen = load_inventory(api, browser, cfg, open_sell_page=False)
        if ops:
            markets["new"] = NewMarket(api, ops, cfg, learned_path)
    if "new" in markets and markets["new"].stopped:
        del markets["new"]          # новый маркет остановлен — дальше его не трогаем

    if first:
        print(f"Старый маркет: {_summary(items, my_sales, 'old')}")
        if new_items:
            print(f"Новый маркет: {_summary(items, my_sales, 'new')}")
        log("Предметы (маркет | id | товар | название | статус):\n  " + "\n  ".join(
            f"{it['market']} | {it['item_id']} | {it['product_id']} | {it['name']} | {it['status']}"
            f"{' | заблокирован' if it['blocked'] else ''}" for it in items))
        if my_sales:
            log(f"Лоты: {[(s['market'], s['sale_id'], s['item_id'], s['price']) for s in my_sales]}")

    products, skipped = plan_products(items, my_sales, cfg, frozen)
    if first and skipped:
        print("Пропускаю: " + ", ".join(f"{k}: {v} шт." for k, v in skipped.items()))
    if "new" not in markets:
        n = sum(len(p["items"]) for (mk, _), p in products.items() if mk == "new")
        if n and first:
            print(f"Новый маркет пропущен ({n} шт.): не знаю, как на нём выставлять.")
        products = {k: p for k, p in products.items() if k[0] in markets}

    sales_by_item = {str(s["item_id"]): s for s in my_sales if s["item_id"] is not None}
    my_item_ids = {str(it["item_id"]) for it in items}
    my_sale_ids = {str(s["sale_id"]) for s in my_sales}

    total = len(products)
    workers = max(1, int(cfg["workers"]))
    if first:
        print(f"Товаров к выставлению: {total} ({sum(len(p['items']) for p in products.values())} шт.). "
              f"Обрабатываю ({workers} параллельно): чтение цен -> снятие лотов -> выставление...\n"
              f"Не трогайте окно браузера до конца работы.\n")

    changed = errors = done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for key, p in products.items():
            market = markets[key[0]]
            name = p["name"] + market.tag
            fut = pool.submit(reprice_one, market, cfg, key[1], name, p["items"],
                              sales_by_item, my_item_ids, my_sale_ids, api.user_id, floors, key)
            futures[fut] = (key, name)
        for fut in as_completed(futures):
            key, name = futures[fut]
            done += 1
            failed = False
            try:
                report, did_change = fut.result()
            except Exception as e:
                errors += 1
                failed = True
                log(f"{name}: {traceback.format_exc()}")
                report, did_change = f"{name}: ошибка ({short(e)})", False
            if did_change:
                changed += 1
            # на повторных проверках показываем только перевыставления и новые ошибки
            # (в тестовом режиме — новые «выставил бы»)
            if first:
                print(f"  [{done}/{total}] {report}")
            elif did_change or ((failed or cfg["dry_run"]) and report != last_reports.get(key)):
                print(f"  [{_now()}] {report}")
            last_reports[key] = report

    stopped = getattr(markets.get("new"), "stopped", None)
    if stopped:
        print(f"\nНовый маркет: {stopped}.")
    if first:
        print(f"\nГотово за {time.time() - t0:.1f} с. Переоценено товаров: {changed}. Ошибок: {errors}.")
    else:
        print(f"[{_now()}] Проверка №{number}: переставлено {changed}, "
              f"без изменений {total - changed - errors}, ошибок {errors}.")


def wait_or_stop(browser: Browser, seconds: float) -> bool:
    """Пауза между проверками. True — пора остановиться (Enter или закрыли браузер)."""
    end = time.time() + seconds
    while time.time() < end:
        if enter_pressed():
            return True
        if browser.proc and browser.proc.poll() is not None:
            print("Окно браузера закрыто.")
            return True
        time.sleep(0.5)
    return False


def run_pricing(api: Api, browser: Browser, cfg: dict, learned_path: str,
                watch: bool = False, max_passes: int = 0):
    """Один проход или слежение за рынком: проверка, пауза, проверка..."""
    global _log_quiet
    markets = {"old": OldMarket(api)}
    floors = {} if watch else None
    last_reports = {}
    number = 0
    while True:
        number += 1
        first = number == 1
        try:
            run_pass(api, browser, cfg, learned_path, markets, first, floors, last_reports, number)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            if first or not watch:
                raise
            # сбой одной проверки (сеть, сайт) не останавливает слежение
            log(traceback.format_exc())
            print(f"[{_now()}] Проверка №{number}: ошибка ({short(e)}) — попробую в следующий раз.")
        if not watch or (max_passes and number >= max_passes):
            return
        if first:
            pct = cfg.get("max_drop_percent")
            guard = (f" Ниже −{pct:g}% от первой цены товара не опускаюсь."
                     if pct is not None and pct >= 0 else "")
            print(f"\nСлежу за рынком: проверяю каждые {cfg['watch_interval_s']:g} с и переставляю, "
                  f"если 5-й лот в топе изменился.{guard}\n"
                  f"Показываю только изменения. Чтобы остановить — нажмите Enter (или Ctrl+C).\n")
            _log_quiet = True       # дальше в лог — только изменения и ошибки
        if wait_or_stop(browser, float(cfg["watch_interval_s"])):
            print("Останавливаю слежение.")
            return


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


def ask_mode() -> str:
    if not (sys.stdin and sys.stdin.isatty()):
        return "once"
    print("Как работать?\n"
          "  1 — переставить цены один раз и выйти\n"
          "  2 — следить за рынком и переставлять цены постоянно (пока не остановите)")
    try:
        answer = input("Введите 1 или 2 и нажмите Enter [2]: ").strip()
    except Exception:
        return "once"
    print()
    return "once" if answer == "1" else "watch"


def main(argv=None):
    parser = argparse.ArgumentParser(description="MM2 Pricer для dreampets.gg")
    parser.add_argument("--dry-run", action="store_true", help="только показать, ничего не менять")
    parser.add_argument("--config", default=CONFIG_PATH, help="путь к mm2pricer_config.json")
    parser.add_argument("--log", default=LOG_PATH, help="куда писать лог")
    parser.add_argument("--learned", default=LEARNED_PATH,
                        help="где хранить, как выставлять на новом маркете")
    parser.add_argument("--no-pause", action="store_true", help="не ждать Enter в конце")
    parser.add_argument("--watch", action="store_true", help="следить за рынком постоянно")
    parser.add_argument("--once", action="store_true", help="переставить цены один раз и выйти")
    parser.add_argument("--passes", type=int, default=0, help=argparse.SUPPRESS)
    args, _ = parser.parse_known_args(argv)

    print(f"=== MM2 Pricer v{VERSION} ===\n")
    cfg = load_config(args.config)
    if args.dry_run:
        cfg["dry_run"] = True
    mode = "watch" if args.watch else "once" if args.once else cfg["mode"]
    if mode not in ("once", "watch"):
        mode = ask_mode()
    if mode == "watch":
        print(f"Режим: слежу за рынком и переставляю цены каждые {cfg['watch_interval_s']:g} с, "
              f"пока не остановите.\n")
    else:
        print("Режим: один раз — вход -> смена цен -> выход.\n")
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

        print(f"Вход подтверждён ({user}).\n")
        api.logged_in = True
        run_pricing(api, browser, cfg, args.learned, watch=(mode == "watch"), max_passes=args.passes)
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
