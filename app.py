#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os,re,sys,time,random,requests
try:
    from patchright.sync_api import sync_playwright
    USING_PATCHRIGHT = True
except ImportError:
    from playwright.sync_api import sync_playwright
    USING_PATCHRIGHT = False

# --- 环境变量 (可在Settings里设置secrets或者私库直接填写在双引号里)---
COOKIE_VALUE = os.environ.get('COOKIE_VALUE') or ""    # remember_web cookie 值，必填
EMAIL        = os.environ.get('EMAIL') or ""           # 登录邮箱,可选，作为备用, 必填
PASSWORD     = os.environ.get('PASSWORD') or ""        # 登录密码,可选，作为备用, 必填
TG_CHAT_ID   = os.environ.get('TG_CHAT_ID') or ""      # Telegram Chat ID,可选，通知
TG_BOT_TOKEN = os.environ.get('TG_BOT_TOKEN') or ""    # Telegram Bot Token,可选 

BASE_URL = "https://dash.hidencloud.com"
LOGIN_URL = f"{BASE_URL}/auth/login"

# --- 代理配置（由工作流 shell 脚本写入 $GITHUB_ENV）---
IS_PROXY      = os.environ.get('IS_PROXY', 'false').lower() == 'true'
PROXY_SERVER  = os.environ.get('PROXY_SERVER') or "http://127.0.0.1:1081"
REQUESTS_PROXIES = {"http": PROXY_SERVER, "https": PROXY_SERVER} if IS_PROXY else None

# Cloudflare 整页挑战 / Turnstile 组件共用的 iframe 选择器
CF_IFRAME_SELECTOR = 'iframe[src*="challenges.cloudflare.com"]'

# 按运行平台选择 UA
if sys.platform == 'win32':
    DEFAULT_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
else:
    DEFAULT_UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'

# 日志
def log(message):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)

STEALTH_JS = """
// 注意：使用真实 Chrome（channel="chrome"）时不要覆盖 window.chrome，
// 否则会丢失 app/csi/loadTimes 等真实特征，反而更容易被 Cloudflare 识别。
try {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
} catch (e) {}
"""

def get_current_ip(proxy_server=None):
    """获取当前出口IP"""
    proxies = {"http": proxy_server, "https": proxy_server} if (proxy_server and IS_PROXY) else None
    try:
        resp = requests.get("https://api.ip.sb/ip", proxies=proxies, timeout=15)
        # log(f"请求出口IP完成, status={resp.status_code}")
        if resp.status_code == 200:
            return resp.text.strip()
        return "获取失败"
    except Exception as e:
        log(f"❌ 获取出口IP失败: {e}")
        return "获取失败"

def send_telegram_notification(status, old_due, new_due, current_ip="未知"):
    """发送 Telegram 通知"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log("⚠️ Telegram 未配置，跳过通知")
        return False
    
    # 获取运行时间
    local_time = time.gmtime(time.time() + 8 * 3600)
    now = time.strftime("%Y-%m-%d %H:%M:%S", local_time)
    if '@' in EMAIL:
        name, domain = EMAIL.split('@', 1)
        if len(name) > 4:
            masked_email = f"{name[:2]}****{name[-2:]}@{domain}"
        else:
            masked_email = f"{name}@{domain}"
    else:
        masked_email = EMAIL[:2] + '****'

    text = (
        f"🎉 HidenCloud 续期通知\n\n"
        f"{status}\n"
        f"👤 账号: {masked_email}\n"
        f"📅 续期前到期：{old_due}\n"
        f"📅 续期后到期：{new_due}\n"
        f"🌐 续期使用IP: {current_ip}\n"
        f"🕒 续期时间：{now}"
    )
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        resp = requests.post(url, json=payload, timeout=10, proxies=REQUESTS_PROXIES)
        if resp.status_code == 200:
            log("✅ Telegram 通知发送成功")
            return True
        else:
            log(f"❌ Telegram 通知失败: {resp.text}")
            return False
    except Exception as e:
        log(f"❌ Telegram 通知异常: {e}")
        return False

def _get_turnstile_token(page):
    """读取 Turnstile 验证通过后生成的响应 token，未通过时返回 None"""
    try:
        return page.evaluate(
            """
            () => {
                try {
                    if (window.turnstile && typeof window.turnstile.getResponse === 'function') {
                        let t = null;
                        try { t = window.turnstile.getResponse(); } catch (e) {}
                        if (t && t.length > 30) return t;
                        for (let i = 0; i < 5; i++) {
                            try { t = window.turnstile.getResponse(String(i)); } catch (e) { t = null; }
                            if (t && t.length > 30) return t;
                        }
                    }
                } catch (e) {}
                const inputs = document.querySelectorAll(
                    'input[name="cf-turnstile-response"], input[id$="_response"]'
                );
                for (const el of inputs) {
                    if (el.value && el.value.length > 30) return el.value;
                }
                return null;
            }
            """
        )
    except Exception:
        return None

def _find_visible_cf_frames(page):
    """返回当前可见且有实际尺寸的 Cloudflare 挑战/Turnstile iframe（后出现的排最后，弹窗内的通常在最后）"""
    result = []
    try:
        for handle in page.query_selector_all(CF_IFRAME_SELECTOR):
            try:
                box = handle.bounding_box()
                if box and box['width'] > 10 and box['height'] > 10:
                    result.append(handle)
            except Exception:
                continue
    except Exception:
        pass
    return result

def _cf_checkbox_visible(page):
    """任意 Cloudflare 框架内（含嵌套 iframe）当前是否可见复选框"""
    for frame in page.frames:
        if 'challenges.cloudflare.com' not in (frame.url or ''):
            continue
        try:
            if frame.locator('input[type="checkbox"]:visible').count() > 0:
                return True
        except Exception:
            continue
    return False

def _click_cf_checkbox(page, frame_el=None):
    """点击 Cloudflare/Turnstile 复选框：先试框架内元素（含嵌套 iframe），失败退化为坐标点击"""
    for frame in page.frames:
        if 'challenges.cloudflare.com' not in (frame.url or ''):
            continue
        try:
            frame.locator('input[type="checkbox"]').first.click(timeout=2500)
            return True
        except Exception:
            continue
    # 退化方案：复选框通常位于 iframe 左侧（x≈20~35，垂直居中），用真实鼠标事件点击
    if frame_el is None:
        return False
    try:
        try:
            frame_el.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        box = frame_el.bounding_box()
        if not box:
            return False
        x = box['x'] + random.uniform(18, 34)
        y = box['y'] + box['height'] / 2 + random.uniform(-4, 4)
        page.mouse.move(x - random.uniform(30, 60), y + random.uniform(-8, 8))
        time.sleep(random.uniform(0.15, 0.4))
        page.mouse.move(x, y)
        time.sleep(random.uniform(0.05, 0.2))
        page.mouse.down()
        time.sleep(random.uniform(0.04, 0.1))
        page.mouse.up()
        return True
    except Exception:
        return False

SECURITY_TITLE_HINTS = ('security verification', 'just a moment', 'attention required', 'checking your browser', '请稍候', 'please wait', 'one more step')

def _is_security_check_page(page):
    try:
        title = (page.title() or '').lower()
        if any(h in title for h in SECURITY_TITLE_HINTS):
            return True
    except Exception:
        return True
    try:
        return page.evaluate(
            """
            () => {
                const t = document.body ? document.body.innerText.slice(0, 5000).toLowerCase() : '';
                return t.includes('verify you are human') || t.includes('checking your browser')
                    || t.includes('security verification');
            }
            """
        )
    except Exception:
        return True

def _has_interstitial_iframe(page):
    """是否存在整页挑战 iframe（排除弹窗内 /turnstile/ 组件 iframe，那由 solve_modal_turnstile 处理）"""
    try:
        for handle in page.query_selector_all(CF_IFRAME_SELECTOR):
            if '/turnstile/' not in (handle.get_attribute('src') or ''):
                return True
    except Exception:
        pass
    return False

def _click_security_submit(page):
    """安全验证页 token 已生成但仍停在验证页时，尝试点击确认/继续按钮"""
    for text in ('Verify', 'Continue', 'Submit', 'Proceed', '验证'):
        try:
            btn = page.locator(f'button:has-text("{text}"):visible, input[type="submit"]:visible').first
            if btn.count() > 0:
                btn.click(timeout=3000)
                log(f"🖱️ 已点击安全验证页的确认按钮（{text}）...")
                return True
        except Exception:
            continue
    return False

def _dump_security_page(page):
    """调试：采集安全验证页的关键结构（iframe/按钮/正文），便于排查"""
    try:
        page.screenshot(path="security_page.png")
        srcs = page.evaluate(
            "() => Array.from(document.querySelectorAll('iframe')).map(f => f.src).filter(Boolean)"
        )

        buttons = page.evaluate(
            "() => Array.from(document.querySelectorAll('button, input[type=submit], a.btn')).map(b => (b.innerText || b.value || '').trim()).filter(t => t && t.length < 40)"
        )
        # log(f"🔍 验证页按钮: {buttons}")
        body = page.evaluate("() => document.body ? document.body.innerText.slice(0, 200) : ''")
        # log(f"🔍 验证页正文: {body}")
    except Exception as e:
        log(f"🔍 验证页信息采集失败: {e}")

def handle_cloudflare(page, timeout=240):
    def challenge_active():
        return _has_interstitial_iframe(page) or _is_security_check_page(page)

    if not challenge_active():
        for _ in range(3):
            time.sleep(1)
            if challenge_active():
                break
        else:
            return True

    log("🔒 检测到 Cloudflare 安全验证...")
    time.sleep(5)
    _dump_security_page(page)

    effective_timeout = timeout + (180 if timeout > 60 else 0)
    manual_hinted = False
    start_time = time.time()
    last_click = 0
    clear_rounds = 0
    while time.time() - start_time < effective_timeout:
        if not challenge_active():
            clear_rounds += 1
            if clear_rounds >= 2: 
                # log("✅ Turnstile 安全验证通过！")
                return True
            time.sleep(1)
            continue
        clear_rounds = 0
        if not manual_hinted and timeout > 60 and time.time() - start_time > timeout - 30:
            log("🤝 自动点击未能通过验证，脚本会继续等待...")
            manual_hinted = True
        if time.time() - last_click > 6:
            frames = _find_visible_cf_frames(page)
            if frames:
                log("🖱️ 点击 Turnstile 验证......")
                if _click_cf_checkbox(page, frames[-1]):
                    last_click = time.time()
                    time.sleep(random.uniform(3, 5))
                    continue
            elif _get_turnstile_token(page) and _is_security_check_page(page):
                # token 已生成但页面仍停在验证页：可能需要手动点确认按钮
                if _click_security_submit(page):
                    last_click = time.time()
                    time.sleep(random.uniform(2, 4))
                    continue
        time.sleep(1)
    log("❌ 验证超时。")
    try:
        page.screenshot(path="security_timeout.png")
    except Exception:
        pass
    return False

def solve_modal_turnstile(page, timeout=90):
    log("🛡️ 开始处理 Turnstile 验证...")
    start = time.time()
    last_click = 0
    clicks = 0
    no_frame_seconds = 0
    while time.time() - start < timeout:
        # 1) token 已生成 → 验证通过
        if _get_turnstile_token(page):
            log("✅ Turnstile 验证通过！")
            return True
        frames = _find_visible_cf_frames(page)
        if not frames:
            # 弹窗里还没有 Turnstile（可能没加载出来），等 20 秒后视为无需验证
            no_frame_seconds += 1
            if no_frame_seconds >= 20 and clicks == 0:
                log("ℹ️ 未检测到 Turnstile 验证框，无需验证。")
                return True
            time.sleep(1)
            continue
        no_frame_seconds = 0
        # 2) 已点击过且框内复选框消失（变成对勾）→ 视为通过（token 字段名可能被站点自定义时兜底）
        if clicks > 0 and time.time() - last_click > 4 and not _cf_checkbox_visible(page):
            time.sleep(2)
            if not _cf_checkbox_visible(page):
                log("✅ Turnstile 复选框已消失，视为验证通过！")
                return True
        # 3) 存在可见验证框时，每隔几秒重试点击
        if time.time() - last_click > 6 and _cf_checkbox_visible(page):
            log(f"🖱️ 点击 Turnstile 复选框（第 {clicks + 1} 次）...")
            if _click_cf_checkbox(page, frames[-1]):
                clicks += 1
                last_click = time.time()
                time.sleep(random.uniform(3, 5))
                continue
        # 4) 长时间检测不到可点击的复选框时，直接坐标点击兜底
        if clicks == 0 and time.time() - start > 20 and time.time() - last_click > 6:
            if _click_cf_checkbox(page, frames[-1]):
                clicks += 1
                last_click = time.time()
                time.sleep(random.uniform(3, 5))
                continue
        time.sleep(1)
    if _get_turnstile_token(page):
        log("✅ Turnstile 验证通过！")
        return True
    log("❌ Turnstile 验证超时。")
    return False

def open_browser(p):
    """启动浏览器，返回 (browser, page)。优先 patchright（反检测内核），未安装则退回原生 playwright。"""
    proxy_arg = {"server": PROXY_SERVER} if IS_PROXY else None
    if USING_PATCHRIGHT:
        browser = p.chromium.launch(
            channel="chrome",
            headless=False,
            args=['--disable-infobars'],
            proxy=proxy_arg,
        )
        ctx = browser.new_context(viewport=None, proxy=proxy_arg)
        page = ctx.new_page()
        return browser, page
    log("⚠️ 未安装 patchright（建议 pip install patchright），退回原生 playwright，过 Cloudflare 能力较弱")
    browser = p.chromium.launch(
        channel="chrome",
        headless=False,
        args=['--no-sandbox', '--disable-blink-features=AutomationControlled', '--disable-infobars']
    )
    ctx = browser.new_context(
        viewport={'width': 1920, 'height': 1080},
        user_agent=DEFAULT_UA,
        proxy=proxy_arg,
    )
    page = ctx.new_page()
    page.add_init_script(STEALTH_JS)
    return browser, page

def _is_logged_in(page):
    """是否真正处于登录后的控制台状态（防止把官网首页/验证页/登录页误判为登录成功）"""
    try:
        url = page.url or ''
        if 'dash.hidencloud.com' not in url:
            return False
        if 'auth/login' in url or _is_security_check_page(page):
            return False
        return True
    except Exception:
        return False

def login(page):
    # 1. Cookie 登录尝试
    if COOKIE_VALUE:
        log("📇 尝试 Cookie 登录...")
        try:
            page.context.add_cookies([{
                'name': 'remember_web_59ba36addc2b2f9401580f014c7f58ea4e30989d',
                'value': COOKIE_VALUE,
                'domain': 'dash.hidencloud.com',
                'path': '/',
                'expires': int(time.time()) + 3600 * 24 * 365,
                'httpOnly': True,
                'secure': True,
                'sameSite': 'Lax'
            }])
            page.goto(f"{BASE_URL}/dashboard", wait_until="domcontentloaded", timeout=60000)
            handle_cloudflare(page, timeout=240)
            time.sleep(2)
            handle_cloudflare(page, timeout=60)  # 跳转后可能再次出现验证页
            if _is_logged_in(page):
                log(f"✅ Cookie 登录成功！当前已到达dashboard页面")
                return True
            log("❌ Cookie 失效，请更换")
        except Exception as e:
            log(f"⚠️ Cookie 登录尝试异常: {e}")

    # 2. 账号密码登录
    if not EMAIL or not PASSWORD:
        return False
    log("💣 尝试账号密码登录...")
    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page, timeout=240)
        time.sleep(2)
        
        log("⌨️ 输入账号密码...")
        # 真实表单字段：input[name="username"]（可填邮箱或用户名）/ input[name="password"]
        user_input = page.locator('input[name="username"], input[name="email"], input[type="email"]').first
        pwd_input = page.locator('input[name="password"], input[type="password"]').first
        try:
            # 登录表单要等安全验证页通过后才会渲染
            user_input.wait_for(state="visible", timeout=30000)
        except Exception:
            handle_cloudflare(page, timeout=120)
            user_input.wait_for(state="visible", timeout=30000)
        user_input.fill(EMAIL, timeout=10000)
        pwd_input.fill(PASSWORD, timeout=10000)
        time.sleep(0.5)
        handle_cloudflare(page, timeout=60)

        # 登录表单自带 Turnstile，提交前先等它的 token
        if not solve_modal_turnstile(page, timeout=90):
            log("⚠️ 登录表单的 Turnstile 未确认通过，仍将尝试提交...")
        
        log("🖱️ 点击登录按钮提交...")
        try:
            page.click('button[type="submit"]', timeout=8000)
        except Exception:
            page.locator('button:has-text("Sign in"), button:has-text("登录")').first.click(timeout=10000)
        time.sleep(3)
        handle_cloudflare(page, timeout=240)
        nav_start = time.time()
        navigated = False
        while time.time() - nav_start < 120:
            try:
                if "/auth/login" not in page.url and not _is_security_check_page(page):
                    navigated = True
                    break
                if _is_security_check_page(page) or _has_interstitial_iframe(page):
                    handle_cloudflare(page, timeout=60)
            except Exception:
                pass
            time.sleep(1)
        if not navigated:
            log("❌ 登录提交后未能完成跳转。")
            page.screenshot(path="login_fail.png")
            return False
        page.goto(f"{BASE_URL}/dashboard", wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page, timeout=120)
        page_title = page.title()
        log(f"📝 当前Title: {page_title}")
        if not _is_logged_in(page):
            log("❌ 登录失败。")
            return False
        log(f"✅ 账号密码登录成功！当前已到达dashboard页面")
        return True
    except Exception as e:
        log(f"❌ 登录异常: {e}")
        page.screenshot(path="login_fail.png")
        return False

def get_server_id(page):
    try:
        if 'dash.hidencloud.com' not in page.url:
            log("⚠️ 当前不在控制台页面，先跳转 dashboard...")
            page.goto(f"{BASE_URL}/dashboard", wait_until="domcontentloaded", timeout=60000)
            handle_cloudflare(page)
            time.sleep(2)
        handle_cloudflare(page)
        time.sleep(3)
        html = page.content()
        log(f"📝 页面长度: {len(html)}, URL: {page.url}")

        # 方案1: 从 href 链接中提取 /service/数字/manage
        matches = re.findall(r'/service/(\d+)/manage', html)
        if matches:
            server_id = matches[0]
            log(f"✅ 从链接中获取到 Server ID: {server_id}")
            return server_id

        # 方案2: 从 span 标签中提取 #数字 (如 "Free Server #218079")；过滤全 0 的误匹配
        matches = [m for m in re.findall(r'#(\d{4,})', html) if set(m) != {'0'}]
        if matches:
            server_id = matches[0]
            log(f"✅ 从文本 #号中获取到 Server ID: {server_id}")
            return server_id

        log("❌ 所有 URL 均未找到 Server ID")
        return None
    except Exception as e:
        log(f"❌ 获取 Server ID 失败: {e}")
        page.screenshot(path="server_id_error.png")
        return None

def get_due_date(page):
    try:
        if SERVICE_URL not in page.url:
            page.goto(SERVICE_URL, wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page)
        body_text = page.locator("body").inner_text()
        patterns = [
            r"Due date\s+(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
            r"Due date\s*\n\s*(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
            r"Due date.*?(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
        ]
        for pattern in patterns:
            match = re.search(pattern, body_text, re.IGNORECASE | re.DOTALL)
            if match:
                due_date = match.group(1).strip()
                log(f"📅 获取到Due Date: {due_date}")
                return due_date
    except Exception as e:
        log(f"❌ 获取Due Date失败: {e}")
    return "未知"

def renew_service(page):

    try:
        log("➡ 进入续期流程...")
        if page.url != SERVICE_URL:
            page.goto(SERVICE_URL, wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page)

        log("🖱️ 准备点击 Renew 按钮...")
        renew_btn = page.locator('button:has-text("Renew")')
        create_btn = page.locator('button:has-text("Create Invoice")')

        modal_opened = False
        for i in range(6):
            try:
                renew_btn.wait_for(state="visible", timeout=10000)
                renew_btn.scroll_into_view_if_needed()
                log(f"🖱️ 第 {i+1} 次尝试点击 'Renew'...")
                renew_btn.click()

                # 等待一小段时间，检测是否出现“未到续期时间”弹窗
                time.sleep(3)
                page_text = page.locator("body").inner_text()
                if "Renewal Restricted" in page_text or "can only renew" in page_text.lower():
                    log("⚠️ 未到续期时间，无法续期。")
                    page.screenshot(path="renew_not_allowed.png")
                    return "NOT_TIME"   # 特殊状态

                log("🖲️ 等待弹窗出现...")
                try:
                    create_btn.wait_for(state="visible", timeout=8000)
                    modal_opened = True
                    log("✅ 弹窗已成功弹出！")
                    break
                except:
                    log("⚠️ 弹窗未出现，可能是点击未响应，准备重试...")
                    time.sleep(2)
            except Exception as e:
                log(f"❌ 点击尝试出错: {e}")

        if not modal_opened:
            log("❌ 错误：尝试多次后，续费弹窗仍未出现。")
            page.screenshot(path="renew_modal_failed.png")
            return False

        # 弹窗内先完成 Turnstile 人机验证，再提交
        handle_cloudflare(page, timeout=60)
        if not solve_modal_turnstile(page, timeout=90):
            log("⚠️ Turnstile 未确认通过，仍将尝试提交...")
            page.screenshot(path="turnstile_timeout.png")

        # 点击 Create Invoice；未跳转说明验证未通过/已过期，重做验证后再试
        new_invoice_url = None
        for attempt in range(6):
            log(f"🖱️ 点击 'Create Invoice'（第 {attempt + 1} 次）...")
            try:
                create_btn.wait_for(state="visible", timeout=20000)
                create_btn.click(timeout=20000)
            except Exception as e:
                log(f"⚠️ 点击 Create Invoice 失败: {e}")
                solve_modal_turnstile(page, timeout=45)
                continue

            start_wait = time.time()
            while time.time() - start_wait < 30:
                if "/payment/invoice/" in page.url:
                    new_invoice_url = page.url
                    log(f"🎉 页面已跳转: {new_invoice_url}")
                    break
                # 出现整页 Cloudflare 拦截/安全验证页时先处理（弹窗内的 Turnstile 组件交给 solve_modal_turnstile）
                if _has_interstitial_iframe(page) or _is_security_check_page(page):
                    log("⚠️ 遇到整页拦截，尝试处理...")
                    handle_cloudflare(page, timeout=60)
                time.sleep(1)
            if new_invoice_url:
                break

            log("⚠️ 未跳转到发票页面，尝试重新完成 Turnstile 验证后重试...")
            solve_modal_turnstile(page, timeout=45)

        if not new_invoice_url:
            log("❌ 未能进入发票页面，超时。")
            page.screenshot(path="renew_stuck_invoice.png")
            return False

        if page.url != new_invoice_url:
            page.goto(new_invoice_url)
        handle_cloudflare(page)

        log("🔎 查找 Pay 按钮...")
        pay_btn = page.locator('a:has-text("Pay"):visible, button:has-text("Pay"):visible').first
        pay_btn.wait_for(state="visible", timeout=30000)
        pay_btn.click()
        log("✅ Pay 按钮已点击")

        # 等待支付确认页面或跳转回服务页
        time.sleep(5)
        # 返回服务管理页面以获取新的到期时间
        page.goto(SERVICE_URL, wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page)
        return True

    except Exception as e:
        log(f"❌ 续费异常: {e}")
        page.screenshot(path="renew_error.png")
        return False

def main():
    # 检查必要环境变量
    log(f"🔍 凭证检测: COOKIE_VALUE={'已配置' if COOKIE_VALUE else '未配置'}, "
        f"EMAIL={'已配置' if EMAIL else '未配置'}, PASSWORD={'已配置' if PASSWORD else '未配置'}")
    if not COOKIE_VALUE and not (EMAIL and PASSWORD):
        log("❌ 缺少登录凭证")
        sys.exit(1)

    global SERVICE_URL

    with sync_playwright() as p:
        browser = None
        try:
            if IS_PROXY:
                log(f"⚙️ 代理已启用: {PROXY_SERVER}")
            else:
                log("🌐 直连模式（未使用代理）")
            
            # 获取当前出口ip
            current_ip = get_current_ip(PROXY_SERVER)
            log(f"🎯 当前出口IP: {current_ip}")

            log("🚀 启动反检测内核浏览器...") # 使用patchright 反检测内核
            browser, page = open_browser(p)

            if not login(page):
                sys.exit(1)

            # 登录成功后，自动获取 Server ID
            server_id = get_server_id(page)
            if not server_id:
                log("❌ 无法获取 Server ID，退出。")
                sys.exit(1)
            SERVICE_URL = f"{BASE_URL}/service/{server_id}/manage"

            # 获取旧到期时间
            old_due = get_due_date(page)
            log(f"📆 续费前到期时间：{old_due}")

            # 执行续费
            renew_result = renew_service(page)

            new_due = old_due
            if renew_result == "NOT_TIME":
                log("⏳ 未到续期时间，目前无法续期")
                status = "⏳ 未到续期时间"
            elif renew_result is False:
                log("❌ 续费失败，脚本退出。")
                status = "❌ 续期失败"
            else:  # renew_result is True
                new_due = get_due_date(page)
                log(f"📆 续费后到期时间：{new_due}")
                status = "✅ 续期成功"

            # 发送 Telegram 通知
            send_telegram_notification(status, old_due, new_due, current_ip)

            if renew_result == "NOT_TIME":
                sys.exit(0)
            elif renew_result is False:
                sys.exit(1)
            else:
                sys.exit(0)
        except Exception as e:
            log(f"❌ 浏览器启动出错: {e}")
            sys.exit(1)
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
                
if __name__ == "__main__":
    main()
