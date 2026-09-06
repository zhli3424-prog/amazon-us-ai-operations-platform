import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request

import pytest


@pytest.mark.browser
def test_login_and_core_navigation_in_real_browser(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    database = tmp_path / "browser.db"
    env = {**os.environ, "DATABASE_PATH": str(database), "MEDIA_ROOT": str(tmp_path / "media"), "PRIVATE_ROOT": str(tmp_path / "private"), "APP_ENV": "testing", "SESSION_SECRET": "browser-session-secret-at-least-32-characters", "ADMIN_PASSWORD": "BrowserAdmin123", "SYNC_INLINE": "true"}
    process = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health/live", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("浏览器测试服务未启动")
        with sqlite3.connect(database) as conn:
            conn.execute("UPDATE users SET must_change_password=0 WHERE username='admin'")
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{port}/login")
            page.locator('input[name="username"]').fill("admin")
            page.locator('input[name="password"]').fill("BrowserAdmin123")
            page.get_by_role("button", name="安全登录").click()
            page.wait_for_url(f"http://127.0.0.1:{port}/dashboard")
            assert page.get_by_text("跨境智营台").first.is_visible()
            page.goto(f"http://127.0.0.1:{port}/guide")
            assert page.get_by_role("heading", name="全流程演示导览").is_visible()
            assert page.get_by_text("增长闭环").is_visible()
            assert page.locator("[data-guide-check]").count() == 12
            page.goto(f"http://127.0.0.1:{port}/products")
            assert page.get_by_role("heading", name="商品主数据").is_visible()
            browser.close()
    finally:
        process.terminate()
        process.wait(timeout=10)
