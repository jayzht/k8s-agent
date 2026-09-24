#!/usr/bin/env python
"""给监控台界面截图，用于文档和渲染回归。

用法：
    python scripts/shots.py                      # 只截监控台（左栏 + 空对话）
    python scripts/shots.py <session_id>         # 额外截该会话的确认卡片与执行结果
    python scripts/shots.py <sid> <inject_sid>   # 再截一张提示注入告警
    OM_WEB=http://127.0.0.1:9000 python scripts/shots.py

前置：
    bash scripts/setup-shots.sh        # 装 playwright 与浏览器
    python -m omagent.cli serve --demo # 另开一个终端把服务跑起来

要截"确认卡片"那张，需要先有一个停在待批状态的会话。可以用
`var/reach_approval.py` 之类的脚本驱动一次，或者手工在页面上问到出卡片为止。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "screenshots"
URL = os.environ.get("OM_WEB", "http://127.0.0.1:8765")
USER = os.environ.get("OM_USER", "zhang.wei")
PASS = os.environ.get("OM_PASS", "ops-pass-2026")


def _sign_in(page) -> None:
    """控制台有登录门，不登录就永远等不到 .wl。

    这段以前是缺的——加上鉴权之后 shots.py 直接超时，说明它没跟上。
    """
    try:
        page.wait_for_selector("#login-mask:not([hidden])", timeout=8000)
    except Exception:
        return  # 已经是登录态（比如复用了 storage_state）
    page.fill("#login-user", USER)
    page.fill("#login-pass", PASS)
    page.click("#login-btn")


def _open(browser, width: int, height: int, sid: str = ""):
    ctx = browser.new_context(viewport={"width": width, "height": height},
                              device_scale_factor=2)
    page = ctx.new_page()
    if sid:
        page.add_init_script(
            f"sessionStorage.setItem('om_sid', {sid!r});"
            f"localStorage.setItem('om_ns', 'demo');"
        )
    page.goto(URL, wait_until="networkidle")
    _sign_in(page)
    page.wait_for_selector(".wl", timeout=30000)
    return page


def main() -> int:
    session_id = sys.argv[1] if len(sys.argv) > 1 else ""
    injection_id = sys.argv[2] if len(sys.argv) > 2 else ""
    OUT.mkdir(parents=True, exist_ok=True)
    # 浏览器若装在仓库内（沙箱下 $HOME 只读时的做法），显式指一下
    bundled = ROOT / "var" / "ms-playwright"
    if bundled.is_dir() and not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(bundled)

    with sync_playwright() as p:
        browser = p.chromium.launch()

        # 1) 全新会话：左栏监控台 + 空对话
        page = _open(browser, 1680, 1000)
        page.wait_for_timeout(1500)
        page.screenshot(path=str(OUT / "01-console.png"))
        print(f"✓ {OUT / '01-console.png'}")

        if not session_id:
            _shot_injection(browser, injection_id)
            browser.close()
            return 0

        # 2) 已有会话：完整对话 + 待确认卡片
        page2 = _open(browser, 1680, 1100, sid=session_id)
        try:
            page2.wait_for_selector(".approval", timeout=30000)
        except Exception:
            print("✗ 这个会话当前没有待确认的卡片，跳过审批截图")
            _shot_injection(browser, injection_id)
            browser.close()
            return 1

        page2.screenshot(path=str(OUT / "02-approval.png"))
        print(f"✓ {OUT / '02-approval.png'}")

        # 3) 点批准，截执行结果
        page2.click(".approval .btn-approve")
        page2.wait_for_selector(".exec", timeout=180000)
        page2.wait_for_timeout(2500)
        page2.eval_on_selector(".exec", "el => el.scrollIntoView({block:'center'})")
        page2.wait_for_timeout(600)
        page2.screenshot(path=str(OUT / "03-executed.png"))
        print(f"✓ {OUT / '03-executed.png'}")

        # 4) 提示注入告警
        _shot_injection(browser, injection_id)

        browser.close()
    return 0


def _shot_injection(browser, injection_id: str) -> None:
    """截"日志里有人塞了指令"这张告警卡。

    这是安全事件的可视化证据：注入文本来自集群数据，模型没有执行它，
    但**人必须看得见**——所以这张图本身就值得进文档。
    """
    if not injection_id:
        return
    page = _open(browser, 1680, 1100, sid=injection_id)
    try:
        page.wait_for_selector(".injection", timeout=30000)
    except Exception:
        print("✗ 这个会话里没有注入告警，跳过")
        return
    page.eval_on_selector(".injection", "el => el.scrollIntoView({block:'center'})")
    page.wait_for_timeout(800)
    page.screenshot(path=str(OUT / "10-injection.png"))
    print(f"✓ {OUT / '10-injection.png'}")


if __name__ == "__main__":
    raise SystemExit(main())
