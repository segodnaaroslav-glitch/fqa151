"""Сквозная проверка MM2 Pricer на локальной имитации сайта (tests/mock_site.py).

Запуск:  python tests/run_e2e.py [путь к chrome/chromium]
Нужен Chrome/Chromium; браузер запускается без окна (--headless=new).
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mock_site import MockSite  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    _s.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRICER = os.path.join(ROOT, "mm2_pricer.py")
BROWSER = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("MM2_BROWSER", "")

# i6 продан, i7 под трейд-локом — их выставлять нельзя
EXPECTED = {"i1": 13.99, "i2": 13.99, "i3": 2.0, "i4": 9.99, "i5": 4.99, "i8": 3.39, "i9": 3.39}
INITIAL = {"i1": 15.0, "i4": 9.99, "i5": 4.99}
# старый маркет выключен — o1 остаётся как был (2.00); на новом n5 — с замороженного
# аккаунта, у n6 нет конкурентов — их не выставляем
EXPECTED_MARKET = {"o1": 2.0, "n1": 23.99, "n2": 23.99, "n3": 43.99, "n7": 43.99, "n4": 5.09}
LEARNED = os.path.join(tempfile.mkdtemp(prefix="mm2e2e-learned-"), "learned.json")


# слежение: после первой проверки конкурент сбивает цены, а сайт «разлогинивает»
EXPECTED_WATCH = {"o1": 2.0, "n1": 19.20, "n2": 19.20, "n3": 43.49, "n7": 43.49, "n4": 5.09}


def competitor_and_logout(site):
    """Ждёт, пока программа выставит Bauble по 23.99, затем: на Bauble обвал (15.00 — ниже
    границы −20%), на Bunnies лот 43.50 (5-й в топе станет 43.50), и все токены сайта истекают."""
    deadline = time.time() + 120
    while time.time() < deadline and site.my_prices().get("n1") != 23.99:
        time.sleep(0.2)
    with site.lock:
        for i in range(5):
            site.state["sales"][f"dump{i}"] = {"product": "N1", "item": f"dumpitem{i}", "price": 15.00, "user": "U7"}
        site.state["sales"]["under"] = {"product": "N2", "item": "underitem", "price": 43.50, "user": "U7"}
        site.tokens.clear()


# по минимальной цене: самый дешёвый чужой лот минус копейка
EXPECTED_MIN = {"o1": 2.0, "n1": 19.99, "n2": 19.99, "n3": 39.99, "n7": 39.99, "n4": 4.99}
# от 3-го лота: Bauble 22.00, Bunnies 42.00; у Candy конкурентов двое — берётся 2-й (5.10)
EXPECTED_LOT3 = {"o1": 2.0, "n1": 21.99, "n2": 21.99, "n3": 41.99, "n7": 41.99, "n4": 5.09}


def more_own_lots(site):
    """Перед «снять всё»: у пользователя три лота на новом маркете (n1, n4 и n3)."""
    site.state["sales"]["own1"] = {"product": "N1", "item": "n1", "price": 30.0, "user": "U1", "account": "acc1"}
    site.state["sales"]["own4"] = {"product": "N3", "item": "n4", "price": 6.0, "user": "U1", "account": "acc1"}


def run(name, variant, transport="auto", dry_run=False, demo=True, expect_text="",
        extra_args=(), during=None, want=None, interval=None, setup=None):
    site = MockSite(variant, demo=demo)
    if setup:
        setup(site)
    p = site.port
    if variant == "old":
        page, api = f"http://127.0.0.1:{p}", f"http://127.0.0.1:{p}"
        domain = "127.0.0.1"
    else:
        page, api = f"http://www.dreampets.test:{p}", f"http://api.dreampets.test:{p}"
        domain = "dreampets.test"
    # адреса — как в старой программе (v1); новый сайт должен найтись сам
    cfg = {
        "login_url": f"{page}/mm2",
        "site_domain": domain,
        "items_api": f"{api}/api/items/v1/items/user",
        "profile_api": f"{api}/api/auth/v1/users/profile",
        "sales_api": f"{api}/api/sales/v1/sales/",
        "sales_user_api": f"{api}/api/sales/v1/sales/user",
        "sales_best_api": f"{api}/api/sales/v1/sales/best",
        # нового маркета в этих вариантах нет — и в настоящий сайт не ходим
        "inventory_api": f"{api}/api/none/{{user_id}}/items",
        "inventory_gates_api": f"{api}/api/none/{{user_id}}/gates",
        "market_sales_user_api": f"{api}/api/none/{{user_id}}/sales",
        # в этих вариантах продажа идёт через старое API — включаем его
        "old_market": True,
        "transport": transport,
        "dry_run": dry_run,
        "browser_path": BROWSER,
        "browser_args": ["--headless=new", "--no-sandbox", "--no-proxy-server",
                         "--host-resolver-rules=MAP *.dreampets.test 127.0.0.1"],
        "auth_timeout_s": 60,
        "teach_timeout_s": 60,
    }
    if interval is not None:
        cfg["watch_interval_s"] = interval
    if variant == "market":
        mkt = f"http://mkt.dreampets.test:{p}"
        cfg.pop("old_market")       # по умолчанию старый маркет выключен — его закрыли
        cfg.update({
            "profile_api": f"{api}/api/auth/v2/users/profile",
            "inventory_api": f"{mkt}/api/inventory/v1/users/{{user_id}}/items",
            "inventory_gates_api": f"{mkt}/api/inventory/v1/users/{{user_id}}/account-inventory-gates",
            "market_sales_user_api": f"{mkt}/api/market/v1/users/{{user_id}}/sales",
        })
    logs = os.environ.get("MM2_E2E_LOGS")
    if logs:
        os.makedirs(logs, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=f"mm2e2e-{name.replace('/', '-')}-", dir=logs or None)
    cfg_path = os.path.join(tmp, "config.json")
    log_path = os.path.join(tmp, "log.txt")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    print(f"\n===== {name} =====", flush=True)
    helper = threading.Thread(target=during, args=(site,), daemon=True) if during else None
    if helper:
        helper.start()
    proc = subprocess.run(
        [sys.executable, "-I", PRICER, "--config", cfg_path, "--log", log_path, "--no-pause",
         "--learned", LEARNED, *extra_args],
        capture_output=True, text=True, encoding="utf-8", timeout=300,
    )
    print(proc.stdout + proc.stderr)
    site.stop()
    got = site.my_prices()
    want = want or (EXPECTED_MARKET if variant == "market" else INITIAL if dry_run else EXPECTED)
    ok = (got == want and ("Ошибок: 0" in proc.stdout or "--cancel-all" in extra_args)
          and expect_text in proc.stdout)
    if variant == "market":
        # старый маркет закрыт: программа не должна его ни показывать, ни трогать
        ok = ok and "старый маркет" not in proc.stdout.lower()
    if not dry_run and variant != "market":
        # лот на P4 уже стоял по нужной цене, у P3 нет конкурентов — их не трогаем
        ok = ok and "s5" in site.state["sales"] and "s4" in site.state["sales"]
    print(f"цены после запуска: {got}")
    print(f"ожидалось:          {want}")
    print(f"лог: {log_path}")
    print("OK" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    results = {
        "старый сайт, запросы через вкладку": run("old/browser", "old"),
        "старый сайт, прямые запросы": run("old/requests", "old", transport="requests"),
        "обновлённый сайт (статусы, страницы, account_id)": run("new/auto", "new"),
        "инвентарь только на странице «Продажа»": run("new2/auto", "new2"),
        "обновлённый сайт, тестовый режим": run("new/dry-run", "new", dry_run=True),
        "два маркета: учусь выставлять у пользователя": run(
            "market/teach", "market", expect_text="Запомнил, как выставлять"),
        "два маркета: по запомненному, без показа": run(
            "market/saved", "market", demo=False, expect_text="как запомнил раньше"),
        "по минимальной цене": run(
            "market/min", "market", demo=False, extra_args=("--once", "--min"),
            want=EXPECTED_MIN, expect_text="минимум на рынке"),
        "от 3-го лота в топе": run(
            "market/lot3", "market", demo=False, extra_args=("--once", "--lot", "3"),
            want=EXPECTED_LOT3, expect_text="3-й в топе 22.00"),
        "снять всё": run(
            "market/cancel", "market", demo=False, extra_args=("--cancel-all",),
            setup=more_own_lots, want={"o1": 2.0}, expect_text="Снято лотов: 3 из 3"),
        "слежение: конкурент сбил цену, граница −20%, сайт разлогинил": run(
            "market/watch", "market", demo=False, extra_args=("--watch", "--passes", "4"),
            during=competitor_and_logout, want=EXPECTED_WATCH, interval=2,
            expect_text="Вход восстановлен"),
    }
    print("\n===== ИТОГ =====")
    for k, v in results.items():
        print(f"{'OK  ' if v else 'FAIL'} {k}")
    sys.exit(0 if all(results.values()) else 1)
