"""Сквозная проверка MM2 Pricer на локальной имитации сайта (tests/mock_site.py).

Запуск:  python tests/run_e2e.py [путь к chrome/chromium]
Нужен Chrome/Chromium; браузер запускается без окна (--headless=new).
"""
import json
import os
import subprocess
import sys
import tempfile

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


def run(name, variant, transport="auto", dry_run=False):
    site = MockSite(variant)
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
        "transport": transport,
        "dry_run": dry_run,
        "browser_path": BROWSER,
        "browser_args": ["--headless=new", "--no-sandbox", "--no-proxy-server",
                         "--host-resolver-rules=MAP *.dreampets.test 127.0.0.1"],
        "auth_timeout_s": 60,
    }
    logs = os.environ.get("MM2_E2E_LOGS")
    if logs:
        os.makedirs(logs, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=f"mm2e2e-{name.replace('/', '-')}-", dir=logs or None)
    cfg_path = os.path.join(tmp, "config.json")
    log_path = os.path.join(tmp, "log.txt")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    print(f"\n===== {name} =====", flush=True)
    proc = subprocess.run(
        [sys.executable, "-I", PRICER, "--config", cfg_path, "--log", log_path, "--no-pause"],
        capture_output=True, text=True, encoding="utf-8", timeout=180,
    )
    print(proc.stdout + proc.stderr)
    site.stop()
    got = site.my_prices()
    want = INITIAL if dry_run else EXPECTED
    ok = got == want and "Ошибок: 0" in proc.stdout
    if not dry_run:
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
    }
    print("\n===== ИТОГ =====")
    for k, v in results.items():
        print(f"{'OK  ' if v else 'FAIL'} {k}")
    sys.exit(0 if all(results.values()) else 1)
