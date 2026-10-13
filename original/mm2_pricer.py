"""
MM2 Pricer — разовый репрайсер для dreampets.gg (версия для одного запуска).

Что делает при запуске:
  1) Поднимает Chrome на ВРЕМЕННОМ, одноразовом профиле (ваши куки/сессии
     не используются и не сохраняются).
  2) Открывает окно — вы входите в свой аккаунт вручную.
  3) Делает ОДИН проход по вашим товарам. Для каждого товара по порядку:
       а) снимает все ваши активные лоты этого товара;
       б) считывает цены на рынке (топ-5 самых дешёвых, уже без ваших лотов);
       в) выставляет все ваши штуки по цене (самая высокая из топ-5) − 1 копейка.
  4) Разлогинивается (полностью удаляет временный профиль) и закрывается.

Никакого цикла: один запуск = одна смена цен.
"""
import os
import sys
import json
import time
import shutil
import tempfile
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests
import websocket


for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


LOGIN_URL = "https://dreampets.gg/mm2"

ITEMS_API = "https://mm2.dreampets.gg/api/items/v1/items/user"
PROFILE_API = "https://auth.dreampets.gg/api/auth/v1/users/profile"
SALES_API = "https://mm2.dreampets.gg/api/sales/v1/sales/"
SALES_USER_API = "https://mm2.dreampets.gg/api/sales/v1/sales/user"
SALES_BEST_API = "https://mm2.dreampets.gg/api/sales/v1/sales/best"

MIN_LISTING_PRICE = 2.0
WORKERS = 10
CDP_PORT = 9333
CDP_BASE = f"http://127.0.0.1:{CDP_PORT}"
AUTH_TIMEOUT_S = 900

CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]


def find_chrome() -> str | None:
    for p in CHROME_PATHS:
        if os.path.isfile(p):
            return p
    return None


# ---------- CDP ----------

def cdp_page_target() -> dict | None:
    try:
        targets = requests.get(f"{CDP_BASE}/json", timeout=5).json()
    except Exception:
        return None
    pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    if not pages:
        return None
    for t in pages:
        if "dreampets.gg" in t.get("url", ""):
            return t
    return pages[0]


def cdp_eval(ws_url: str, expression: str):
    ws = websocket.create_connection(ws_url, timeout=10, suppress_origin=True)
    try:
        ws.send(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True},
        }))
        deadline = time.time() + 10
        while time.time() < deadline:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                return msg.get("result", {}).get("result", {}).get("value")
    finally:
        try:
            ws.close()
        except Exception:
            pass
    return None


def read_access_token() -> str | None:
    # Токен лежит в localStorage['auth'] -> JSON {data: "<JSON-строка>"} -> accessToken
    target = cdp_page_target()
    if not target:
        return None
    try:
        raw = cdp_eval(target["webSocketDebuggerUrl"], "window.localStorage.getItem('auth')")
        if not raw:
            return None
        auth_outer = json.loads(raw)
        data = json.loads(auth_outer["data"])
        return data.get("accessToken")
    except Exception:
        return None


# ---------- Chrome ----------

def launch_chrome(profile_dir: str) -> subprocess.Popen | None:
    chrome = find_chrome()
    if not chrome:
        print("Не нашёл Chrome. Установите Google Chrome и запустите снова.")
        return None
    args = [
        chrome,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={profile_dir}",
        # отдельный профиль = чистый вход, ничего не сохраняется
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        LOGIN_URL,
    ]
    proc = subprocess.Popen(args)
    deadline = time.time() + 25
    while time.time() < deadline:
        try:
            requests.get(f"{CDP_BASE}/json/version", timeout=2)
            return proc
        except Exception:
            time.sleep(1)
    print("Не дождался запуска Chrome.")
    return proc


def close_chrome(proc: subprocess.Popen | None, profile_dir: str):
    # Разлогин: чистим localStorage (там токен), затем закрываем Chrome
    # и удаляем временный профиль целиком.
    try:
        target = cdp_page_target()
        if target:
            cdp_eval(target["webSocketDebuggerUrl"], "window.localStorage.clear()")
    except Exception:
        pass
    try:
        if proc:
            proc.terminate()
    except Exception:
        pass
    time.sleep(2)
    # Добиваем оставшиеся процессы Chrome именно этого профиля
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{profile_dir}*' }} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=15)
    except Exception:
        pass
    time.sleep(1)
    for _ in range(5):
        try:
            shutil.rmtree(profile_dir, ignore_errors=True)
            if not os.path.exists(profile_dir):
                return
        except Exception:
            pass
        time.sleep(1)


# ---------- API ----------

def api_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def check_auth(token: str) -> bool:
    try:
        r = requests.get(PROFILE_API, headers=api_headers(token), timeout=10)
        if r.status_code != 200:
            return False
        body = r.json()
        return bool(body.get("user_id")) and bool(body.get("username"))
    except Exception:
        return False


def get_all_items(token: str) -> list[dict]:
    r = requests.get(ITEMS_API, headers=api_headers(token), timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"items вернул {r.status_code}")
    return r.json().get("items", [])


def get_my_sales(token: str) -> list[dict]:
    r = requests.get(SALES_USER_API, headers=api_headers(token), timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"sales/user вернул {r.status_code}")
    return r.json().get("sales", [])


def get_best_sales(token: str, product_id: str, count: int = 5) -> list[dict]:
    r = requests.get(
        SALES_BEST_API,
        params={"product_id": product_id, "currency": "rub", "count": count},
        headers=api_headers(token),
        timeout=15,
    )
    if r.status_code != 200:
        raise RuntimeError(f"sales/best вернул {r.status_code}")
    return sorted(r.json().get("sales", []), key=lambda s: s["price"])


def create_sale(token: str, item_id: str, product_id: str, price: float):
    r = requests.post(
        SALES_API,
        headers={**api_headers(token), "Content-Type": "application/json"},
        data=json.dumps({"items": [{
            "item_id": item_id, "product_id": product_id,
            "price": round(price, 2), "currency": "rub",
        }]}),
        timeout=15,
    )
    if r.status_code not in (200, 201, 204):
        raise RuntimeError(f"create_sale вернул {r.status_code}")


def cancel_sales(token: str, sale_ids: list[str]):
    if not sale_ids:
        return
    r = requests.delete(
        SALES_API,
        headers={**api_headers(token), "Content-Type": "application/json"},
        data=json.dumps({"sale_ids": sale_ids}),
        timeout=15,
    )
    if r.status_code not in (200, 204):
        raise RuntimeError(f"cancel_sales вернул {r.status_code}")


# ---------- Логика ----------

def owned_by_product(all_items: list[dict]) -> dict[str, dict]:
    # product_id -> {name, qty}: сколько штук каждого товара у вас есть
    result = {}
    for item in all_items:
        if item.get("status") not in ("in_inventory", "on_sale"):
            continue
        product = item.get("product", {})
        pid = product.get("product_id")
        if not pid:
            continue
        entry = result.setdefault(pid, {"name": product.get("name", ""), "qty": 0})
        entry["qty"] += 1
    return result


def _create_retry(token, item_id, pid, price):
    for attempt in range(3):
        try:
            create_sale(token, item_id, pid, price)
            return
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1)


def reprice_one(token, pid, name, all_items, my_sales) -> str | None:
    # Один товар: снять свои лоты -> прочитать рынок -> выставить все штуки
    # по (максимум из топ-5) − 0.01.

    my_sales_by_item = {s["item_id"]: s for s in my_sales}

    my_items = [
        i for i in all_items
        if i.get("product", {}).get("product_id") == pid
        and i.get("status") in ("in_inventory", "on_sale")
    ]
    if not my_items:
        return None
    all_item_ids = [i["item_id"] for i in my_items]

    # а) снимаем все свои активные лоты этого товара
    my_sale_ids_for_product = []
    prev_prices = {}
    for i in my_items:
        if i.get("status") == "on_sale" and i["item_id"] in my_sales_by_item:
            sale = my_sales_by_item[i["item_id"]]
            my_sale_ids_for_product.append(sale["sale_id"])
            prev_prices[i["item_id"]] = sale["price"]
    if my_sale_ids_for_product:
        cancel_sales(token, my_sale_ids_for_product)
        time.sleep(0.4)

    # б) читаем рынок (без своих лотов)
    old_sale_ids = {s["sale_id"] for s in my_sales}
    best5 = get_best_sales(token, pid, count=5)
    market_prices = sorted(s["price"] for s in best5 if s["sale_id"] not in old_sale_ids)

    removed = len(my_sale_ids_for_product)

    # рынок пуст — возвращаем как было
    if not market_prices:
        for item_id, price in prev_prices.items():
            _create_retry(token, item_id, pid, price)
        return f"{name}: снял {removed} -> рынок пуст (конкурентов нет) -> вернул как было", False

    peak = market_prices[-1]
    # в) считаем в копейках, чтобы не было ошибок округления
    peak_cents = round(peak * 100)
    min_cents = round(MIN_LISTING_PRICE * 100)
    if peak_cents <= min_cents:
        target = min_cents / 100.0
    else:
        target = (peak_cents - 1) / 100.0

    for item_id in all_item_ids:
        _create_retry(token, item_id, pid, target)

    return (f"{name}: снял {removed} -> рынок топ-5 max {peak:.2f} -> выставил "
            f"{len(all_item_ids)} шт по {target:.2f} (на 0.01 ниже)"), True


def run_pricing(token: str):
    t0 = time.time()
    print("Загружаю ваши товары...")
    all_items = get_all_items(token)
    my_sales = get_my_sales(token)
    owned = owned_by_product(all_items)
    eligible = {pid: info for pid, info in owned.items() if info["qty"] >= 1}
    total = len(eligible)
    print(f"Товаров в работе: {total}. Обрабатываю ({WORKERS} параллельно): "
          f"снятие лотов -> чтение цен -> выставление...\n")

    changed = 0
    errors = 0
    done = 0

    def worker(pid, info):
        return reprice_one(token, pid, info["name"], all_items, my_sales)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(worker, pid, info): info for pid, info in eligible.items()}
        for fut in as_completed(futures):
            info = futures[fut]
            done += 1
            try:
                result = fut.result()
            except Exception as e:
                errors += 1
                short = str(e).splitlines()[0][:120]
                print(f"  [{done}/{total}] {info['name']}: ошибка ({short})")
                continue
            if result is None:
                continue
            report, did_change = result
            if did_change:
                changed += 1
            print(f"  [{done}/{total}] {report}")

    print(f"\nГотово за {time.time() - t0:.1f} с. Переоценено: {changed}. Ошибок: {errors}.")


def main():
    print("=== MM2 Pricer ===")
    print("Одноразовый запуск: вход -> смена цен -> выход.\n")

    profile_dir = tempfile.mkdtemp(prefix="mm2pricer-")
    proc = None
    try:
        proc = launch_chrome(profile_dir)
        if proc is None:
            return

        print(f"\nОткрыл {LOGIN_URL} — войдите в свой аккаунт в окне браузера.")
        print("Жду вход в аккаунт...")
        token = None
        deadline = time.time() + AUTH_TIMEOUT_S
        while time.time() < deadline:
            t = read_access_token()
            if t and check_auth(t):
                token = t
                break
            time.sleep(3)
        if not token:
            print("Не дождался входа в аккаунт. Завершаюсь.")
            return

        print("Вход подтверждён.\n")
        run_pricing(token)
    finally:
        print("\nРазлогиниваюсь и закрываю браузер...")
        close_chrome(proc, profile_dir)
        print("Завершено.")
        try:
            input("\nНажмите Enter, чтобы закрыть окно...")
        except Exception:
            pass


if __name__ == "__main__":
    main()
