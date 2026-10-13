"""
MM2 Pricer v2 — разовый репрайсер для dreampets.gg (версия для одного запуска).

Что делает при запуске:
  1) Поднимает Chrome (или Edge) на ВРЕМЕННОМ, одноразовом профиле (ваши
     куки/сессии не используются и не сохраняются).
  2) Открывает окно — вы входите в свой аккаунт вручную.
  3) Делает ОДИН проход по вашим товарам. Для каждого товара по порядку:
       а) считывает цены на рынке (топ-5 самых дешёвых, без ваших лотов);
       б) снимает все ваши активные лоты этого товара;
       в) выставляет все ваши штуки по цене (самая высокая из топ-5) − 1 копейка.
  4) Разлогинивается (полностью удаляет временный профиль) и закрывается.

Никакого цикла: один запуск = одна смена цен.

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
import shutil
import socket
import argparse
import tempfile
import threading
import traceback
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, urlencode, unquote

import requests
import websocket


for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


VERSION = "2.0"

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

    # статусы предметов: какие считаются «моими» и какие — «выставлен на продажу»
    "inventory_statuses": ["in_inventory", "on_sale"],
    "on_sale_statuses": ["on_sale"],

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


def norm_item(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    product = raw.get("product") if isinstance(raw.get("product"), dict) else {}
    item_id = first(raw, "item_id", "itemId", "id", "_id")
    pid = first(product, "product_id", "productId", "id", "_id") or first(raw, "product_id", "productId")
    if item_id is None or pid is None:
        return None
    name = first(product, "name", "title", "display_name") or first(raw, "name", "title") or pid
    return {
        "item_id": item_id,
        "product_id": pid,
        "name": str(name),
        "status": str(raw.get("status") or "").strip().lower(),
    }


def norm_sale(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    item = raw.get("item") if isinstance(raw.get("item"), dict) else {}
    sale_id = first(raw, "sale_id", "saleId", "id", "_id")
    if sale_id is None:
        return None
    return {
        "sale_id": sale_id,
        "item_id": first(raw, "item_id", "itemId") or first(item, "item_id", "itemId", "id", "_id"),
        "price": to_price(first(raw, "price", "amount", "cost")),
    }


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
                        self.on_event(msg.get("method"), msg.get("params") or {})
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

    def _on_event(self, method: str, p: dict):
        if method == "Network.requestWillBeSent":
            req = p.get("request") or {}
            url = req.get("url", "")
            if not host_in_domain(urlparse(url).hostname, self.domain):
                return
            if req.get("method") == "OPTIONS" or self._from_us(p.get("initiator")):
                return
            if p.get("type") not in ("XHR", "Fetch") and "/api/" not in url:
                return
            self._req_meta[p.get("requestId")] = (req.get("method", "GET"), url)
            self._remember_headers(url, req.get("headers") or {})
        elif method == "Network.requestWillBeSentExtraInfo":
            meta = self._req_meta.get(p.get("requestId"))
            if meta:
                self._remember_headers(meta[1], p.get("headers") or {})
        elif method == "Network.responseReceived":
            meta = self._req_meta.get(p.get("requestId"))
            if meta:
                status = (p.get("response") or {}).get("status")
                self.observed[(meta[0], meta[1].split("?")[0])] = status

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
        full = url + ("?" + urlencode(params) if params else "")
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
        log(f"{method} {full} {data or ''} -> {status} {text[:600]!r}")
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
    def _fail(what: str, status, data) -> RuntimeError:
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        return RuntimeError(f"{what} вернул {status}: {short(text, 100)}")

    # --- методы ---

    def get_list(self, key: str, field: str, what: str) -> list:
        status, data = self.request("GET", key)
        if status != 200:
            raise self._fail(what, status, data)
        lst = extract_list(data, field)
        if lst is None:
            raise RuntimeError(f"{what}: не понял формат ответа ({short(json.dumps(data, ensure_ascii=False), 100)})")
        return lst

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

    def create_sale(self, item_id, product_id, price: float):
        status, data = self.request("POST", "sales_api", body={"items": [{
            "item_id": item_id, "product_id": product_id,
            "price": round(price, 2), "currency": self.cfg["currency"],
        }]})
        if status not in (200, 201, 204):
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

def owned_by_product(items: list[dict], statuses: set) -> dict:
    # product_id -> {name, qty}: сколько штук каждого товара у вас есть
    result = {}
    for item in items:
        if item["status"] not in statuses:
            continue
        entry = result.setdefault(item["product_id"], {"name": item["name"], "qty": 0})
        entry["qty"] += 1
    return result


def _create_retry(api: Api, item_id, pid, price):
    for attempt in range(3):
        try:
            api.create_sale(item_id, pid, price)
            return
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


def reprice_one(api: Api, cfg: dict, pid, name, items, my_sales):
    """Один товар: прочитать рынок -> снять свои лоты -> выставить все штуки
    по (максимум из топ-5) − 0.01. Возвращает (отчёт, изменено) или None."""
    inv_statuses = {s.lower() for s in cfg["inventory_statuses"]}
    sale_statuses = {s.lower() for s in cfg["on_sale_statuses"]}
    top_n = int(cfg["top_n"])

    my_sales_by_item = {s["item_id"]: s for s in my_sales if s["item_id"] is not None}
    my_items = [i for i in items if i["product_id"] == pid and i["status"] in inv_statuses]
    if not my_items:
        return None
    all_item_ids = [i["item_id"] for i in my_items]

    my_sale_ids_for_product = []
    prev_prices = {}
    for i in my_items:
        if i["status"] in sale_statuses and i["item_id"] in my_sales_by_item:
            sale = my_sales_by_item[i["item_id"]]
            my_sale_ids_for_product.append(sale["sale_id"])
            prev_prices[i["item_id"]] = sale["price"]
    removed = len(my_sale_ids_for_product)

    # а) рынок без своих лотов: берём с запасом на свои и выкидываем их.
    # Читаем ДО снятия, чтобы при ошибке чтения лоты не остались снятыми.
    old_sale_ids = {s["sale_id"] for s in my_sales}
    best = api.get_best_sales(pid, count=top_n + removed)
    market_prices = sorted(s["price"] for s in best if s["sale_id"] not in old_sale_ids)[:top_n]

    if not market_prices:
        return f"{name}: рынок пуст (конкурентов нет) -> оставил как было", False

    peak = market_prices[-1]
    target = target_price(peak, cfg)

    if (len(prev_prices) == len(all_item_ids)
            and all(p is not None and round(p * 100) == round(target * 100) for p in prev_prices.values())):
        return f"{name}: рынок топ-{len(market_prices)} max {peak:.2f} -> цена {target:.2f} уже стоит", False

    if cfg["dry_run"]:
        return (f"{name}: [ТЕСТ] снял бы {removed} -> рынок топ-{len(market_prices)} max {peak:.2f} "
                f"-> выставил бы {len(all_item_ids)} шт по {target:.2f}"), False

    # б) снимаем свои активные лоты этого товара
    if my_sale_ids_for_product:
        api.cancel_sales(my_sale_ids_for_product)
        time.sleep(0.4)

    # в) выставляем все штуки; ошибка на одной не мешает остальным
    failed = []
    for item_id in all_item_ids:
        try:
            _create_retry(api, item_id, pid, target)
        except Exception as e:
            failed.append(e)
    if failed:
        raise RuntimeError(f"выставил {len(all_item_ids) - len(failed)} из {len(all_item_ids)} "
                           f"по {target:.2f}; {short(failed[0], 100)}")

    return (f"{name}: снял {removed} -> рынок топ-{len(market_prices)} max {peak:.2f} -> выставил "
            f"{len(all_item_ids)} шт по {target:.2f} (на {cfg['undercut']:.2f} ниже)"), True


def run_pricing(api: Api, cfg: dict):
    t0 = time.time()
    print("Загружаю ваши товары...")
    items_raw = api.get_list("items_api", "items", "items")
    sales_raw = api.get_list("sales_user_api", "sales", "sales/user")
    items = [i for i in map(norm_item, items_raw) if i]
    my_sales = [s for s in map(norm_sale, sales_raw) if s]
    log(f"Предметов: {len(items_raw)} (разобрано {len(items)}), лотов: {len(sales_raw)} (разобрано {len(my_sales)})")
    if items_raw:
        log(f"Пример предмета: {json.dumps(items_raw[0], ensure_ascii=False)[:800]}")
    if sales_raw:
        log(f"Пример лота: {json.dumps(sales_raw[0], ensure_ascii=False)[:800]}")
    if items_raw and not items:
        print("Сайт вернул предметы в незнакомом формате — подробности в логе.")

    statuses = {s.lower() for s in cfg["inventory_statuses"]}
    eligible = owned_by_product(items, statuses)
    total = len(eligible)
    if items and not total:
        seen = sorted({i["status"] for i in items})
        print(f"Предметов: {len(items)}, но ни один не в статусах {sorted(statuses)}.\n"
              f"Статусы на сайте: {seen}. Их можно указать в inventory_statuses / "
              f"on_sale_statuses в mm2pricer_config.json.")
    workers = max(1, int(cfg["workers"]))
    print(f"Товаров в работе: {total}. Обрабатываю ({workers} параллельно): "
          f"чтение цен -> снятие лотов -> выставление...\n")

    changed = 0
    errors = 0
    done = 0

    def worker(pid, info):
        return reprice_one(api, cfg, pid, info["name"], items, my_sales)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(worker, pid, info): info for pid, info in eligible.items()}
        for fut in as_completed(futures):
            info = futures[fut]
            done += 1
            try:
                result = fut.result()
            except Exception as e:
                errors += 1
                log(f"{info['name']}: {traceback.format_exc()}")
                print(f"  [{done}/{total}] {info['name']}: ошибка ({short(e)})")
                continue
            if result is None:
                continue
            report, did_change = result
            if did_change:
                changed += 1
            print(f"  [{done}/{total}] {report}")

    print(f"\nГотово за {time.time() - t0:.1f} с. Переоценено: {changed}. Ошибок: {errors}.")


def log_diagnostics(browser: Browser, api: Api | None):
    with browser._sniff_lock:
        headers = {h: sorted(v) for h, v in browser.sniffed.items()}
    log(f"Заголовки, которые сайт сам слал в API: {headers}")
    log("Запросы сайта к API:\n  " + "\n  ".join(
        f"{m} {u} -> {s}" for (m, u), s in sorted(list(browser.observed.items()), key=str)))
    if api:
        log(f"Итоговые адреса API: {api.endpoints}")
        log(f"Авторизация: {api.auth[0]}, fetch из вкладки: {browser.cred_mode or 'нет'}")


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
        run_pricing(api, cfg)
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
