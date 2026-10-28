#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Orihost 单服自动续期（Jexactyl 面板），以 Hiden 骨架为基座自研
# 流程（bundle.json 12:59 录制 + trace 抓包实测）：
#   进 /server/<短ID> → 关中央广告(要關閉) → 点 Renew → 点 Read Article（新标签读文章 dwell 秒）
#   → 回面板等倒计时 → 过 Turnstile → 点 Claim Renewal → complete 完成（+7 天）
# 右侧悬浮广告不影响，直接忽略；中央广告是外部广告商(ldrws)动态下发，只按关闭按钮匹配

import os
import re
import sys
import time
import random
import requests
from playwright.sync_api import sync_playwright

# --- 环境变量 ---
ORIHOST_REMEMBER = (
    os.environ.get('ORIHOST_REMEMBER')
    or os.environ.get('ORIHOST_COOKIE')      # 兼容旧变量名
    or os.environ.get('ORI_COOKIE')
    or os.environ.get('COOKIE_VALUE')
    or ""
).strip()
ORIHOST_SERVER_IDS = os.environ.get('ORIHOST_SERVER_IDS') or ""  # 单服：短ID 或 完整 UUID
EMAIL = os.environ.get('EMAIL') or os.environ.get('ORIHOST_EMAIL') or ""  # 仅用于 TG 通知脱敏展示
TG_BOT_TOKEN = os.environ.get('TG_BOT_TOKEN') or ""
TG_CHAT_ID = os.environ.get('TG_CHAT_ID') or ""
# 兼容 TG_BOT="chat_id,bot_token" 写法
_TG_BOT = os.environ.get('TG_BOT') or ""
if not (TG_BOT_TOKEN and TG_CHAT_ID) and ',' in _TG_BOT:
    _a, _b = _TG_BOT.split(',', 1)
    TG_CHAT_ID, TG_BOT_TOKEN = _a.strip(), _b.strip()

BASE_URL = "https://panel.orihost.com"
SERVER_SHORT_ID = "a6695785"  # 地址栏 /server/ 后面那段
SERVER_UUID = "a6695785-5b4b-4e49-b98f-b4f2eace7851"  # 抓包实测：API 要用全量 UUID
SERVER_URL = f"{BASE_URL}/server/{SERVER_SHORT_ID}"
API_COOLDOWN = f"{BASE_URL}/api/client/servers/{SERVER_UUID}/renew/cooldown"
API_BEGIN = f"{BASE_URL}/api/client/servers/{SERVER_UUID}/renew/begin"

# 文章页停留秒数（begin 返回 dwell_seconds=15，默认 15，可用环境变量覆盖）
ARTICLE_WAIT = int(os.environ.get('ARTICLE_WAIT') or "15")
# Claim 阶段总超时秒数（轮询总时长）
CLAIM_TIMEOUT = int(os.environ.get('CLAIM_TIMEOUT') or "60")

# --- 代理配置（由工作流 sing-box 步骤写入 $GITHUB_ENV，本地可用 ORIHOST_PROXY）---
_MANUAL_PROXY = os.environ.get('ORIHOST_PROXY') or os.environ.get('ORIHOST_GOST_PROXY') or ""
IS_PROXY = (os.environ.get('IS_PROXY', 'false').lower() == 'true') or bool(_MANUAL_PROXY)
PROXY_SERVER = os.environ.get('PROXY_SERVER') or _MANUAL_PROXY or "socks5://127.0.0.1:1080"
REQUESTS_PROXIES = {"http": PROXY_SERVER, "https": PROXY_SERVER} if IS_PROXY else None

# remember_web cookie 名（bundle.json 12:59 录制快照实测）
REMEMBER_COOKIE_NAME = "remember_web_59ba36addc2b2f9401580f014c7f58ea4e30989d"


# --- 日志 ---
def log(message):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = { runtime: {} };
"""


def extract_remember_token(raw):
    """从裸 token 或整段 Cookie 字符串中提取 remember_web 值"""
    raw = (raw or "").strip().strip('"').strip("'")
    if not raw:
        return ""
    # 整段 Cookie：找 remember_web_xxx=yyy
    m = re.search(r'remember_web_[0-9a-f]+=([^;\s]+)', raw)
    if m:
        return m.group(1).strip()
    # Cookie 头里直接给了值（eyJ 开头几百字符）
    if raw.startswith('eyJ') and len(raw) > 100 and ' ' not in raw and '\n' not in raw:
        return raw
    # 短 ID 场景兜底：去掉前缀后返回
    if '=' in raw and 'remember_web' in raw:
        return raw.split('=', 1)[1].split(';')[0].strip()
    return raw


def mask_server(text):
    """完整 UUID 脱敏，日志只留短 ID"""
    if not text:
        return text
    return text.replace(SERVER_UUID, f"{SERVER_SHORT_ID}(已脱敏)")


def get_current_ip(proxy_server=None):
    """获取当前出口IP"""
    proxies = {"http": proxy_server, "https": proxy_server} if (proxy_server and IS_PROXY) else None
    try:
        resp = requests.get("https://api.ip.sb/ip", proxies=proxies, timeout=15)
        if resp.status_code == 200:
            return resp.text.strip()
        return "获取失败"
    except Exception as e:
        log(f"❌ 获取出口IP失败: {e}")
        return "获取失败"


def send_telegram_notification(status, old_due, new_due, current_ip="未知"):
    """发送 Telegram 通知（对齐 eooce/Auto-Renew-HidenCloud 风格）"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log("⚠️ Telegram 未配置，跳过通知")
        return False

    local_time = time.gmtime(time.time() + 8 * 3600)
    now = time.strftime("%Y-%m-%d %H:%M:%S", local_time)
    if '@' in EMAIL:
        name, domain = EMAIL.split('@', 1)
        if len(name) > 4:
            masked_email = f"{name[:2]}****{name[-2:]}@{domain}"
        else:
            masked_email = f"{name}@{domain}"
    elif EMAIL:
        masked_email = EMAIL[:2] + '****'
    else:
        masked_email = f"服务器 {SERVER_SHORT_ID}"

    text = (
        f"🎉 Orihost 续期通知\n\n"
        f"{status}\n"
        f"👤 账号: {masked_email}\n"
        f"📅 续期前到期：{old_due}\n"
        f"📅 续期后到期：{new_due}\n"
        f"🌐 续期使用IP: {current_ip}\n"
        f"🕒 续期时间：{now}"
    )
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        resp = requests.post(url, json=payload, timeout=10, proxies=REQUESTS_PROXIES)
        if resp.status_code == 200:
            log("✅ Telegram 通知发送成功")
            return True
        log(f"❌ Telegram 通知失败: {resp.text[:200]}")
        return False
    except Exception as e:
        log(f"❌ Telegram 通知异常: {e}")
        return False


def turnstile_token(page):
    """读 Turnstile token（input 在 light DOM，控件本身在 closed shadow DOM）"""
    try:
        return page.evaluate(
            '() => document.querySelector("[name=cf-turnstile-response]")?.value || ""'
        )
    except Exception:
        return ""


def turnstile_box(page):
    """取 Turnstile 控件矩形：iframe 在 closed shadow DOM，选择器进不去；
    用 light DOM 里 cf-turnstile-response input 的可见祖先定位"""
    try:
        return page.evaluate(
            """() => {
                const inp = document.querySelector('[name=cf-turnstile-response]');
                if (!inp) return null;
                let n = inp.parentElement;
                while (n && n !== document.body) {
                    const r = n.getBoundingClientRect();
                    if (r.width > 50 && r.height > 20) {
                        return {x: r.x, y: r.y, w: r.width, h: r.height};
                    }
                    n = n.parentElement;
                }
                return null;
            }"""
        )
    except Exception:
        return None


def click_turnstile_box(page, box):
    """真实鼠标轨迹点控件左侧的复选框（shadow DOM 内无法用 locator 点）"""
    tx = box["x"] + 30 + random.uniform(-2, 2)
    ty = box["y"] + box["h"] / 2 + random.uniform(-2, 2)
    try:
        page.mouse.move(tx - random.uniform(120, 220), ty - random.uniform(60, 120), steps=8)
        time.sleep(random.uniform(0.15, 0.3))
        page.mouse.move(tx, ty, steps=15)
        time.sleep(random.uniform(0.15, 0.3))
        page.mouse.down()
        time.sleep(random.uniform(0.05, 0.12))
        page.mouse.up()
        return True
    except Exception as e:
        log(f"⚠️ Turnstile 点击失败: {str(e)[:120]}")
        return False


def handle_cloudflare(page, timeout=90):
    """处理 Cloudflare Turnstile 验证
    timeout：本轮最多等待秒数；轮询中请传小值（如 15），避免一轮卡死
    注意：Turnstile 控件渲染在 closed shadow DOM 里，Playwright 选择器看不到 iframe，
    旧写法 count 永远为 0、从不点击。这里按控件矩形用坐标点复选框，再轮询 token。"""
    box = turnstile_box(page)
    if not box:
        return True
    log(f"⚠️ 检测到 Turnstile 验证框（{int(box['w'])}x{int(box['h'])}）...")
    start_time = time.time()
    clicks = 0
    last_click = 0.0
    while time.time() - start_time < timeout:
        if len(turnstile_token(page)) > 20:
            log("✅ Turnstile token 已生成")
            return True
        box = turnstile_box(page)
        if box:
            if box["h"] > 200:
                log(f"⚠️ Turnstile 升级为图像验证（{int(box['w'])}x{int(box['h'])}），脚本无法完成")
                return False
            # 两次点击至少隔 6s，避免在「验证中」反复打断
            if clicks < 3 and time.time() - last_click > 6:
                log("🖱️ 点击 Turnstile 复选框...")
                if click_turnstile_box(page, box):
                    clicks += 1
                    last_click = time.time()
        time.sleep(1)
    if len(turnstile_token(page)) > 20:
        log("✅ Turnstile token 已生成（超时前命中）")
        return True
    log("❌ 验证超时。")
    return False


def wait_turnstile_token(page, timeout=90):
    """等待 Turnstile token 生成（Claim 前置条件）"""
    log("⏳ 等待 Turnstile token...")
    start = time.time()
    while time.time() - start < timeout:
        if len(turnstile_token(page)) > 20:
            log("✅ Turnstile token 已生成")
            return True
        time.sleep(1)
    log("⚠️ Turnstile token 未生成（可能免验证或验证失败）")
    return False


def close_center_ad(page, rounds=3):
    """关闭屏幕中央广告弹窗（steps/S000008、S000015 实测：繁体『要關閉』按钮）
    右侧悬浮广告不影响，直接忽略。广告内容每次动态变化，只按关闭按钮匹配。"""
    closed = 0
    for i in range(rounds):
        try:
            btn = page.locator('button:has-text("要關閉")')
            if btn.count() == 0:
                btn = page.locator('button:has-text("關閉")')
            visible = False
            for idx in range(btn.count()):
                try:
                    if btn.nth(idx).is_visible():
                        log(f"🖱️ 关闭中央广告（第 {i+1} 轮）...")
                        btn.nth(idx).click()
                        closed += 1
                        visible = True
                        time.sleep(1.5)
                        break
                except Exception:
                    continue
            if not visible:
                break
        except Exception:
            break
    if closed:
        log(f"✅ 已关闭中央广告 {closed} 次")
    return closed


def dismiss_cookie_banner(page):
    """点掉底部 cookie 横幅（We use cookies → Got it），免得遮挡对话框"""
    try:
        btn = page.locator('button:has-text("Got it")')
        for idx in range(btn.count()):
            try:
                if btn.nth(idx).is_visible():
                    btn.nth(idx).click()
                    time.sleep(1)
                    break
            except Exception:
                continue
    except Exception:
        pass


def login(page):
    """remember_web Cookie 登录（单服 MVP 不做账密兜底）"""
    token = extract_remember_token(ORIHOST_REMEMBER)
    if not token:
        log("❌ 缺少 ORIHOST_REMEMBER（remember_web token 值）")
        return False
    log("📇 尝试 Cookie 登录...")
    try:
        page.context.add_cookies([{
            'name': REMEMBER_COOKIE_NAME,
            'value': token,
            'domain': 'panel.orihost.com',
            'path': '/',
            'expires': int(time.time()) + 3600 * 24 * 365,
            'httpOnly': True,
            'secure': True,
            'sameSite': 'Lax'
        }])
        page.goto(SERVER_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(2)
        close_center_ad(page)
        handle_cloudflare(page)
        log(f"📝 当前URL: {mask_server(page.url)} | Title: {page.title()}")
        if "auth/login" in page.url:
            log("❌ Cookie 失效（被踢回登录页），请重新获取 remember_web token")
            page.screenshot(path="login_fail.png")
            return False
        if SERVER_SHORT_ID not in page.url:
            log(f"⚠️ 未进入服务器页：{mask_server(page.url)}")
            page.screenshot(path="login_fail.png")
            return False
        log("✅ Cookie 登录成功，已到达服务器页")
        return True
    except Exception as e:
        log(f"❌ 登录异常: {e}")
        try:
            page.screenshot(path="login_fail.png")
        except Exception:
            pass
        return False


def api_cooldown(page):
    """读续期冷却（bundle.json：GET .../renew/cooldown → {"seconds":0}），页面上下文内请求自动带 Cookie+XSRF"""
    try:
        data = page.evaluate(
            """async (url) => {
                const r = await fetch(url, {credentials: 'same-origin', headers: {'Accept': 'application/json'}});
                if (!r.ok) return {http: r.status};
                return await r.json();
            }""",
            API_COOLDOWN,
        )
        log(f"📝 cooldown 接口返回: {data}")
        if isinstance(data, dict) and "seconds" in data:
            return int(data["seconds"])
        return None
    except Exception as e:
        log(f"⚠️ cooldown 接口读取失败（走 UI 流程）: {e}")
        return None


def get_renewal_days(page):
    """读取面板『Current renewal in: N days』/『RENEWAL IN N Days』，返回天数（int）或 None"""
    try:
        body_text = page.locator("body").inner_text(timeout=10000)
    except Exception as e:
        log(f"❌ 读取页面文本失败: {e}")
        return None
    patterns = [
        r"Current renewal in:\s*(\d+)\s*days?",
        r"RENEWAL IN\s*(\d+)\s*Days?",
    ]
    for pattern in patterns:
        m = re.search(pattern, body_text, re.IGNORECASE)
        if m:
            days = int(m.group(1))
            log(f"📅 当前剩余：{days} 天")
            return days
    log("⚠️ 未找到剩余天数文本")
    return None


def renew_service(page):
    """Orihost 续期芯：Renew → Read Article（新标签 dwell）→ Turnstile → Claim Renewal
    返回 True / False / "NOT_TIME"（冷却中或未到续期条件）"""

    # 0. 先查冷却：seconds>0 直接跳过
    seconds = api_cooldown(page)
    if seconds is not None and seconds > 0:
        log(f"⏳ 冷却中，剩余 {seconds}s，本轮跳过")
        return "NOT_TIME"

    try:
        log("➡ 进入续期流程...")
        if SERVER_SHORT_ID not in page.url:
            page.goto(SERVER_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(2)
        close_center_ad(page)
        handle_cloudflare(page)

        # 1. 点 Renew（bundle 选择器按文本兜底，避免 styled-components 类名漂移）
        # 未到续期时间时按钮为 disabled 置灰（文案 "Renew Limit Reached"），直接跳过不报错
        log("🖱️ 点击 'Renew'...")
        renew_btn = page.locator('button:has-text("Renew")').first
        try:
            renew_btn.wait_for(state="visible", timeout=15000)
        except Exception:
            log("❌ 找不到 Renew 按钮")
            page.screenshot(path="renew_no_button.png")
            return False
        try:
            body_pre = page.locator("body").inner_text(timeout=5000)
        except Exception:
            body_pre = ""
        if "Renew Limit Reached" in body_pre or "renewal limit" in body_pre.lower():
            log("⏳ 未到续期时间（Renew Limit Reached），本轮跳过")
            return "NOT_TIME"
        try:
            if renew_btn.is_disabled() or not renew_btn.is_enabled():
                log("⏳ Renew 按钮置灰（disabled），未到续期时间，本轮跳过")
                return "NOT_TIME"
        except Exception:
            pass
        renew_btn.scroll_into_view_if_needed()
        try:
            renew_btn.click(timeout=10000)
        except Exception as e:
            msg = str(e)
            if "not enabled" in msg.lower() or "disabled" in msg.lower():
                log("⏳ Renew 按钮不可点（置灰），未到续期时间，本轮跳过")
                return "NOT_TIME"
            # 点击超时后复查一次：可能是刚变成置灰
            try:
                body_retry = page.locator("body").inner_text(timeout=5000)
            except Exception:
                body_retry = ""
            if "Renew Limit Reached" in body_retry or "renewal limit" in body_retry.lower():
                log("⏳ 未到续期时间（Renew Limit Reached），本轮跳过")
                return "NOT_TIME"
            try:
                if renew_btn.is_disabled():
                    log("⏳ Renew 按钮置灰（disabled），未到续期时间，本轮跳过")
                    return "NOT_TIME"
            except Exception:
                pass
            log(f"❌ 点击 Renew 失败: {msg[:200]}")
            page.screenshot(path="renew_click_fail.png")
            return False
        time.sleep(2)
        close_center_ad(page)

        # 2. 等续期对话框出现
        dlg_text = page.locator('div.fixed.inset-0')
        try:
            dlg_text.first.wait_for(state="visible", timeout=15000)
        except Exception:
            log("❌ 续期对话框未弹出（可能未到续期条件）")
            page.screenshot(path="renew_no_dialog.png")
            return "NOT_TIME"
        body = page.locator("body").inner_text()
        if "Current renewal in" in body:
            m = re.search(r"Current renewal in:\s*(\d+)", body)
            if m:
                log(f"📅 对话框显示剩余 {m.group(1)} 天")

        # 3. 点 Read Article（新标签打开文章，begin 接口此时触发）
        log("🖱️ 点击 'Read Article'...")
        read_btn = page.locator('button:has-text("Read Article")').first
        try:
            read_btn.wait_for(state="visible", timeout=15000)
        except Exception:
            # 可能已在倒计时/可 Claim 状态
            if "Claim Renewal" in body or "second(s)" in body:
                log("➡ 已在倒计时/待 Claim 状态，跳过 Read Article")
            else:
                log("❌ 找不到 Read Article 按钮")
                page.screenshot(path="renew_no_read.png")
                return False
        else:
            read_btn.scroll_into_view_if_needed()
            try:
                with page.expect_popup(timeout=15000) as pop:
                    read_btn.click()
                article = pop.value
                # popup 先是 about:blank，等它导航到真实文章页再 dwell
                for _ in range(20):
                    try:
                        cur = article.url
                    except Exception:
                        cur = ""
                    if cur and cur != "about:blank":
                        break
                    time.sleep(1)
                try:
                    article.wait_for_load_state("domcontentloaded", timeout=30000)
                except Exception:
                    pass
                try:
                    log(f"📖 文章页已打开: {(article.url or '')[:80]}...")
                except Exception:
                    log("📖 文章页已打开")
                if (article.url or "") in ("", "about:blank"):
                    log("⚠️ 文章页仍是空白页（begin 已在点击时触发，继续倒计时）")
                dwell = max(ARTICLE_WAIT, 15)
                log(f"⏳ 模拟阅读 {dwell}s...")
                for _ in range(dwell):
                    time.sleep(1)
                    try:
                        article.evaluate("() => window.scrollBy(0, 200)")
                    except Exception:
                        pass
                    # 文章页也可能弹广告/验证，只做最小处理
                    try:
                        if article.locator('iframe[src*="challenges.cloudflare.com"]').count() > 0:
                            pass
                    except Exception:
                        pass
                try:
                    article.close()
                except Exception:
                    pass
                log("📖 文章页已关闭，回到面板")
            except Exception as e:
                log(f"⚠️ 新标签未捕获（{e}），改用等待倒计时继续")

        # 4. 回面板：等倒计时走完（"Thanks for reading"），轮询 Claim 可点
        try:
            page.bring_to_front()
        except Exception:
            pass
        if SERVER_SHORT_ID not in page.url:
            page.goto(SERVER_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(2)
        close_center_ad(page)
        dismiss_cookie_banner(page)
        handle_cloudflare(page)

        log("⏳ 等待倒计时结束（Thanks for reading）...")
        claimed = False
        start_wait = time.time()
        poll_timeout = CLAIM_TIMEOUT
        while time.time() - start_wait < poll_timeout:
            try:
                body = page.locator("body").inner_text(timeout=5000)
            except Exception:
                time.sleep(2)
                continue
            # 未到条件/上限的几种文案直接判跳过
            if "Renew Limit Reached" in body or "renewal limit" in body.lower():
                log("⚠️ 面板显示续期次数已达上限，本轮跳过")
                page.screenshot(path="renew_limit.png")
                return "NOT_TIME"
            if "Thanks for reading" in body or "Claim Renewal" in body:
                # 广告可能挡住验证框，先清；cookie 横幅也顺手点掉
                close_center_ad(page, rounds=1)
                dismiss_cookie_banner(page)
                # 盾必须主动点击才会出 token，每轮都试一次（小超时，不卡死轮询）
                handle_cloudflare(page, timeout=15)
                # 免验证场景可能直接可点；否则等一小会儿 token
                wait_turnstile_token(page, timeout=10)
                claim_btn = page.locator('button:has-text("Claim Renewal")').first
                try:
                    if claim_btn.count() and claim_btn.is_visible() and claim_btn.is_enabled():
                        log("🖱️ 点击 'Claim Renewal'...")  # 前端随即调 GET /api/client/renewal/complete?cf-turnstile-response=
                        claim_btn.click()
                        claimed = True
                        break
                    else:
                        log("⏳ Claim 按钮仍不可点（Turnstile 未通过），继续等待...")
                except Exception:
                    pass
            else:
                # 倒计时还没走完
                m = re.search(r"claim your renewal in\s*(\d+)\s*second", body, re.IGNORECASE)
                if m:
                    log(f"⏳ 倒计时中…{m.group(1)}s")
            time.sleep(5)

        if not claimed:
            log("❌ 超时未点到 Claim Renewal")
            page.screenshot(path="renew_claim_timeout.png")
            return False

        # 5. 等 complete 生效：轮询剩余天数变大或成功文案
        log("⏳ 等待续期生效...")
        time.sleep(5)
        close_center_ad(page)
        handle_cloudflare(page)
        try:
            page.goto(SERVER_URL, wait_until="domcontentloaded", timeout=60000)
            time.sleep(3)
        except Exception:
            pass
        log("✅ Claim 已点击，续期请求已提交")
        return True

    except Exception as e:
        log(f"❌ 续费异常: {e}")
        try:
            page.screenshot(path="renew_error.png")
        except Exception:
            pass
        return False


def main():
    if not ORIHOST_REMEMBER:
        log("❌ 缺少登录凭证：请设置 ORIHOST_REMEMBER（remember_web token 值）")
        sys.exit(1)

    with sync_playwright() as p:
        try:
            if IS_PROXY:
                log(f"⚙️ 代理已启用: {PROXY_SERVER}")
            else:
                log("🌐 直连模式（未使用代理）")

            current_ip = get_current_ip(PROXY_SERVER)
            log(f"🎯 当前出口IP: {current_ip}")

            log("🚀 启动浏览器...")
            browser = p.chromium.launch(
                channel="chrome",
                headless=False,
                args=['--no-sandbox', '--disable-blink-features=AutomationControlled', '--disable-infobars']
            )
            context = browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                # 不要覆盖 UA：Playwright 只改 UA 字符串，UA-CH 仍报真实内核版本，
                # 两者不一致会被 Turnstile 判为机器人（300010），盾直接不给 token
                proxy={"server": PROXY_SERVER} if IS_PROXY else None
            )
            page = context.new_page()
            page.add_init_script(STEALTH_JS)

            if not login(page):
                send_telegram_notification("❌ 登录失败（Cookie 失效）", "未知", "未知", current_ip)
                sys.exit(1)

            # 续期前剩余天数
            old_days = get_renewal_days(page)
            old_due = f"剩余 {old_days} 天" if old_days is not None else "未知"

            # 执行续期
            renew_result = renew_service(page)

            new_due = old_due
            if renew_result == "NOT_TIME":
                log("⏳ 未到续期条件，本轮跳过")
                status = "⏳ 未到续期条件，本轮跳过"
            elif renew_result is False:
                log("❌ 续期失败")
                status = "❌ 续期失败（详见 Actions 日志截图）"
            else:
                new_days = get_renewal_days(page)
                new_due = f"剩余 {new_days} 天" if new_days is not None else "已提交待确认"
                log(f"📆 续期后：{new_due}")
                status = "✅ 续期成功"

            send_telegram_notification(status, old_due, new_due, current_ip)

            if renew_result is False:
                sys.exit(1)
            sys.exit(0)
        except Exception as e:
            log(f"❌ 浏览器启动出错: {e}")
            sys.exit(1)
        finally:
            if 'browser' in locals() and browser:
                try:
                    browser.close()
                except Exception:
                    pass


if __name__ == "__main__":
    main()
