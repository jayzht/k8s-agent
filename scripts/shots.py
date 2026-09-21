"""用 Playwright 驱动 Web 审批界面并截图，用于验证真实渲染效果。"""
import os, sys, time
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path("/home/ubuntu/O&M-agent")
SHOTS = ROOT / "var" / "shots"
SHOTS.mkdir(parents=True, exist_ok=True)
URL = os.environ.get("OM_WEB", "http://127.0.0.1:8765")


def shot(page, name):
    page.screenshot(path=str(SHOTS / name), full_page=False)
    print(f"  ✓ {name}")


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1720, "height": 1060}, device_scale_factor=1)
    console_errors = []
    page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: console_errors.append(str(e)))

    print("加载页面…")
    page.goto(URL, wait_until="networkidle")
    page.wait_for_timeout(1500)
    shot(page, "01-idle.png")

    # 自然语言入口：先展示能解析的情况
    print("演示自然语言入口…")
    try:
        page.fill("#ask-text", "api-gateway 一直重启，帮我看看")
        page.click("button:has-text('理解并诊断')")
        page.wait_for_selector(".cand", timeout=180000)
        page.wait_for_timeout(1200)
        shot(page, "07-ask-resolved.png")
    except Exception as e:
        print("  !! 自然语言诊断失败：", e)

    # 再展示"解析不出目标时如实拒绝"
    print("演示意图拒识…")
    try:
        page.fill("#ask-text", "订单服务 5xx 飙升了")
        page.click("button:has-text('理解并诊断')")
        page.wait_for_selector(".confirm.blocked", timeout=120000)
        page.wait_for_timeout(900)
        shot(page, "08-ask-refused.png")
    except Exception as e:
        print("  !! 拒识演示失败：", e)

    # 切到策略页
    page.click('button.tab[data-tab="policy"]')
    page.wait_for_timeout(700)
    shot(page, "02-policy.png")
    page.click('button.tab[data-tab="audit"]')

    # 选一个工作负载做诊断
    print("触发诊断…")
    page.click('#wl-list li:has-text("api-gateway")')
    try:
        page.wait_for_selector(".cand", timeout=180000)
    except Exception as e:
        print("  !! 未出现候选动作：", e)
        shot(page, "03-diagnosis-timeout.png")
    page.wait_for_timeout(1200)
    shot(page, "03-diagnosis.png")

    # 生成确认卡片（产品主角）
    print("生成确认卡片…")
    btns = page.query_selector_all(".cand .btn")
    if btns:
        btns[0].click()
        try:
            page.wait_for_selector(".confirm", timeout=120000)
        except Exception as e:
            print("  !! 确认卡片未出现：", e)
        page.wait_for_timeout(1200)
        shot(page, "04-confirm-card.png")

    # 越界请求演示
    print("演示越界请求…")
    page.click('button:has-text("删除 production 命名空间")')
    page.wait_for_timeout(1500)
    shot(page, "05-refusal.png")

    # 知识沉淀
    page.click('button.tab[data-tab="knowledge"]')
    page.wait_for_timeout(900)
    shot(page, "06-knowledge.png")

    if console_errors:
        print("浏览器控制台错误：")
        for e in console_errors[:10]:
            print("   !", e[:200])
    else:
        print("浏览器控制台无错误 ✅")

    browser.close()
print("DONE")
