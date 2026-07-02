"""BOSS直聘 自动打招呼 (CloakBrowser + LLM)

流程：
1. 加载 match_decisions.json + match_result_llm.json，筛选 accept 的岗位
2. 确保登录状态（Cookie 复用 → 扫码回退）
3. 逐岗位：打开详情页 → 检查风控信号 → 点击"立即沟通" → LLM生成招呼语 → 发送
4. 速率控制：每次间隔 5-15s，每 10 个后休息 1-3min，单次上限 20
"""

import os
import json
import time
import random
import re
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import httpx
from cloakbrowser import launch

# ============================================================
# 配置
# ============================================================

COOKIE_FILE = Path.home() / ".config" / "boss_zhipin_cookies.json"
DATA_DIR = Path("data")
SCREENSHOT_DIR = DATA_DIR / "screenshots"
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR = Path("log")
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / f"{datetime.now().strftime('%Y%m%d')}.log"

# 速率控制
DELAY_BETWEEN_GREETS = [5000, 15000]     # 每次打招呼后随机等待 (ms)
DELAY_AFTER_BATCH = [60000, 180000]      # 每 BATCH_SIZE 个后休息 (ms)
BATCH_SIZE = 10
MAX_GREET_PER_RUN = 20

# LLM 配置
DS_ENDPOINT = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1/chat/completions")
DS_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
DS_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

GREETING_SYSTEM_PROMPT = (
    "你是求职者本人，在BOSS直聘给HR发招呼语。"
    "回复会原样发给HR，严禁任何注释、说明、括号备注、字数统计或引导语。\n"
    "【格式】1.开头前15字必须是“您好，我熟悉XXX、XXX”（填该JD要求且你简历具备的核心技能1-2个）。"
    "2.紧接“做过XXX”说明简历里与该岗位相关的具体项目/经历。"
    "3.结尾是“对xx岗位很感兴趣，期待您的回复“全文80-120字，真诚自然。"
)

# ============================================================
# Selectors (ported from tmp/utils.js)
# ============================================================

SELECTORS = {
    "login": {
        "avatar": [
            'img[alt*="头像"]', '.nav-figure img', '.user-nav img', '[class*="avatar"] img',
        ],
        "nav_my": [
            '.user-nav:has-text("我的")', '#header:has-text("我的")',
            'nav:has-text("我的")', 'a:has-text("我的")',
        ],
        "logged_in_markers": [
            "我的在线简历", "个人中心",
        ],
    },
    "job_detail": {
        "container": [
            ".job-detail-box", ".job-detail-body", ".job-detail-card",
        ],
        "chat_now": [
            'button:has-text("继续沟通")', 'a:has-text("继续沟通")',
            'span:has-text("继续沟通")', '[role="button"]:has-text("继续沟通")',
            '.btn:has-text("继续沟通")', '[class*="btn"]:has-text("继续沟通")',
            'text=继续沟通',
            'button:has-text("立即沟通")', 'a:has-text("立即沟通")',
            'span:has-text("立即沟通")', '[role="button"]:has-text("立即沟通")',
            '.btn:has-text("立即沟通")', '[class*="btn"]:has-text("立即沟通")',
            'text=立即沟通',
            '.op-btn-chat', '.job-detail-box .op-btn-chat',
            '[ka*="btn_communicate"]', '[ka*="communicate"]', '[ka*="chat"]',
        ],
        # 详情页上的"继续沟通"按钮（已打过招呼的岗位）
        "continue_chat": [
            'button:has-text("继续沟通")', 'a:has-text("继续沟通")',
            'span:has-text("继续沟通")', '[role="button"]:has-text("继续沟通")',
            '.btn:has-text("继续沟通")', 'text=继续沟通',
        ],
        # 点击"立即沟通"后浮窗中的"继续沟通"按钮
        # BOSS流程: 点"立即沟通"→发默认招呼→浮窗"留在此页，继续沟通"→点此按钮跳转聊天页
        "continue_chat_popup": [
            '.dialog-container button:has-text("继续沟通")',
            '.dialog-container span:has-text("继续沟通")',
            '.dialog-container .btn-sure',
            '.boss-dialog__wrapper button:has-text("继续沟通")',
            '.boss-dialog__wrapper span:has-text("继续沟通")',
            '[class*="dialog"] button:has-text("继续沟通")',
            '[class*="dialog"] span:has-text("继续沟通")',
            '[class*="popup"] button:has-text("继续沟通")',
            '[class*="modal"] button:has-text("继续沟通")',
            '[ka="dialog_confirm"]',
            'button:has-text("继续沟通")',
            'a:has-text("继续沟通")',
            'span:has-text("继续沟通")',
            '[role="button"]:has-text("继续沟通")',
            'text=继续沟通',
        ],
        "resume_option": [
            '.resume-online', 'text=默认简历', '[class*="resume"]:has-text("默认")',
        ],
        "resume_confirm": [
            'button:has-text("确定")', 'button:has-text("确认")', 'button:has-text("完成")',
        ],
        "daily_limit": [
            '.dialog-container:has-text("今日沟通已达上限")',
            'text=今日沟通已达上限', 'text=沟通已达上限',
        ],
        "dialog_container": [
            ".dialog-container", ".boss-dialog__wrapper", ".dialog-wrap",
        ],
        "risk_notice": [
            'text=安全验证', 'text=请完成验证', 'text=验证码', 'text=异常操作',
        ],
        "job_closed": [
            'text=职位已关闭', 'text=职位关闭', 'text=该职位已关闭',
        ],
        "job_open": [
            'text=招聘中', 'text=正在招聘', 'text=急聘',
        ],
    },
    # 聊天页面 (https://www.zhipin.com/web/geek/chat) 的 selectors
    # 来源: tmp/web_script.js SELECTORS.ZHIPIN.CHAT
    "chat_page": {
        "message_input": "#chat-input",       # 聊天输入框 (contenteditable div)
        "send_btn": ".btn-send",              # 发送按钮
        "history_ctn": ".chat-message",       # 聊天记录容器
        "message_items": ".item-friend,.item-myself",  # 消息项
        "message_content": ".message-content .text",   # 消息正文
    },
}

# ============================================================
# 工具函数
# ============================================================

def rand(a: int, b: int) -> int:
    """闭区间随机整数"""
    return random.randint(a, b)


def sleep_ms(ms: int):
    time.sleep(ms / 1000.0)


def sleep_range(rng: list) -> int:
    """从 [min, max] 范围随机取毫秒数并 sleep，返回实际毫秒"""
    ms = rand(rng[0], rng[1])
    sleep_ms(ms)
    return ms


def ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(action: str, subject: str = "-", status: str = "INFO", details: str = ""):
    suffix = f" {details}" if details else ""
    line = f"[{ts()}] [{action}] [{subject}] [{status}]{suffix}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def normalize(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def build_company_aliases(company: str) -> list[str]:
    """生成公司名匹配别名，处理有限公司/科技/括号等后缀。"""
    base = normalize(company)
    aliases = []

    def add(val: str):
        val = normalize(val)
        if val and len(val) >= 2 and val not in aliases:
            aliases.append(val)

    add(base)
    plain = re.sub(r"[()（）\[\]【】]", "", base)
    add(plain)

    suffixes = [
        "股份有限公司", "有限责任公司", "科技有限公司", "信息技术有限公司",
        "技术有限公司", "有限公司", "研究院", "集团", "科技", "技术", "公司",
    ]
    changed = True
    while changed:
        changed = False
        for suffix in suffixes:
            if plain.endswith(suffix) and len(plain) - len(suffix) >= 2:
                plain = plain[:-len(suffix)]
                add(plain)
                changed = True
    return aliases


def build_chat_match_keywords(company: str, job_name: str) -> list[str]:
    """聊天列表匹配关键词：公司名优先，岗位名兜底。"""
    keywords = []
    for alias in build_company_aliases(company):
        if alias not in keywords:
            keywords.append(alias)

    job_name = normalize(job_name)
    if job_name:
        for n in [10, 8, 6, 4]:
            if len(job_name) >= n:
                key = job_name[:n]
                if key not in keywords:
                    keywords.append(key)
        if job_name not in keywords:
            keywords.append(job_name)
    return keywords


def score_chat_item_text(text: str, company: str, job_name: str) -> int:
    """给聊天列表项打分：公司名优先，岗位名次之。"""
    text = normalize(text)
    if not text:
        return 0

    score = 0
    for alias in build_company_aliases(company):
        if alias in text:
            score = max(score, 100 + len(alias) * 3)

    job_name = normalize(job_name)
    if job_name and job_name in text:
        score = max(score, 70 + len(job_name))

    for n in [10, 8, 6, 4]:
        if len(job_name) >= n and job_name[:n] in text:
            score = max(score, 40 + n)

    if any(token in text for token in ["HR", "招聘", "人事", "猎头", "Boss", "BOSS"]):
        score += 3
    return score


# ============================================================
# 简历摘要
# ============================================================

def summarize_resume(resume_path: str = "resume/resume.md") -> str:
    """提取简历关键信息：个人总结 + 项目经验 + 技能关键词"""
    with open(resume_path, "r", encoding="utf-8") as f:
        content = f.read()

    # 提取个人总结
    m = re.search(r"个人总结</h5>\s*<hr\s*/?>\s*(.*?)(?=<h5|$)", content, re.DOTALL)
    summary = m.group(1).strip() if m else ""

    # 提取项目经验
    m = re.search(r"项目经验</h5>\s*<hr\s*/?>\s*(.*?)(?=<h5|$)", content, re.DOTALL)
    projects_raw = m.group(1).strip() if m else ""

    # 拆分成项目列表
    projects = []
    for block in re.split(r"（\d+）", projects_raw):
        block = block.strip()
        if block:
            # 只取标题行（第一行）
            title_line = block.split("\n")[0].strip()
            projects.append(title_line)

    # 提取技能关键词（从项目描述中）
    skills = set()
    skill_patterns = [
        "Agent", "RAG", "LLM", "LangChain", "LangGraph", "FastAPI", "Vue",
        "Python", "TypeScript", "React", "Docker", "K8s", "Redis", "Celery",
        "SQLAlchemy", "ONNX", "TensorRT", "Whisper", "Embedding", "Milvus",
        "Prompt", "微服务", "多模态", "NLP", "CV",
    ]
    for s in skill_patterns:
        if s.lower() in content.lower():
            skills.add(s)

    # 拼接简历摘要
    lines = []
    if summary:
        lines.append(f"个人总结：{summary[:200]}")
    if skills:
        lines.append(f"核心技能：{'、'.join(sorted(skills))}")
    if projects:
        lines.append(f"项目经验：")
        for i, p in enumerate(projects[:5], 1):
            lines.append(f"  {i}. {p[:120]}")

    return "\n".join(lines)


# ============================================================
# LLM 招呼语生成
# ============================================================

def build_greeting_prompt(resume_summary: str, job: dict) -> str:
    """构造招呼语生成的 user prompt"""
    name = job.get("name", "")
    company = job.get("company", "") or job.get("companyName", "")
    desc = job.get("description", "")
    salary = job.get("salary", "")
    location = job.get("location", "")

    return f"""【我的简历】
{resume_summary}

【目标岗位】
岗位名：{name}
公司：{company}
薪资：{salary}
地点：{location}
岗位描述：
{desc}"""


def call_llm(system_prompt: str, user_prompt: str, max_tokens: int = 300) -> str:
    """调用 DeepSeek API 生成招呼语"""
    if not DS_API_KEY:
        raise RuntimeError("未设置 DEEPSEEK_API_KEY 环境变量")

    payload = {
        "model": DS_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.7,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {DS_API_KEY}",
    }

    try:
        resp = httpx.post(DS_ENDPOINT, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        content = (data.get("choices", [{}])[0]
                   .get("message", {})
                   .get("content", ""))
        return content.strip()
    except Exception as e:
        log("LLM", "call_llm", "ERROR", str(e))
        raise


def generate_greeting(resume_summary: str, job: dict, greeting_file: Path) -> str:
    """生成招呼语并保存到 data 目录"""
    user_prompt = build_greeting_prompt(resume_summary, job)
    greeting = call_llm(GREETING_SYSTEM_PROMPT, user_prompt, max_tokens=300)

    # 保存到文件
    record = {
        "timestamp": datetime.now().isoformat(),
        "job_name": job.get("name", ""),
        "company": job.get("company", "") or job.get("companyName", ""),
        "greeting": greeting,
    }
    # 追加到 JSONL 文件
    with open(greeting_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    return greeting


# ============================================================
# 人类行为模拟 (ported from tmp/utils.js)
# ============================================================

def human_move(page, target_x: int, target_y: int):
    """4 段贝塞尔曲线模拟鼠标移动"""
    vp = page.viewport_size or {"width": 1440, "height": 900}
    w, h = vp.get("width", 1440), vp.get("height", 900)

    start_x = rand(20, max(30, w - 20))
    start_y = rand(20, max(30, h - 20))
    ctrl_x = round((start_x + target_x) / 2 + rand(-90, 90))
    ctrl_y = round((start_y + target_y) / 2 + rand(-60, 60))
    mid_x = round((start_x + ctrl_x) / 2)
    mid_y = round((start_y + ctrl_y) / 2)
    late_x = round((ctrl_x + target_x) / 2)
    late_y = round((ctrl_y + target_y) / 2)

    page.mouse.move(start_x, start_y, steps=rand(4, 8))
    sleep_ms(rand(60, 160))
    page.mouse.move(mid_x, mid_y, steps=rand(8, 14))
    sleep_ms(rand(12, 28))
    page.mouse.move(late_x, late_y, steps=rand(8, 14))
    sleep_ms(rand(12, 28))
    page.mouse.move(target_x, target_y, steps=rand(10, 18))


def human_move_and_click(page, locator):
    """滚动到可见 → 计算随机偏移 → 贝塞尔移动 → 点击"""
    try:
        locator.scroll_into_view_if_needed()
    except Exception:
        pass
    sleep_ms(rand(120, 320))

    try:
        box = locator.bounding_box()
    except Exception:
        box = None

    if not box:
        locator.click(delay=rand(60, 150))
        return

    target_x = round(box["x"] + box["width"] / 2 + rand(-6, 6))
    target_y = round(box["y"] + box["height"] / 2 + rand(-5, 5))
    human_move(page, target_x, target_y)
    sleep_ms(rand(80, 180))
    page.mouse.down()
    sleep_ms(rand(40, 120))
    page.mouse.up()


def human_type(page, locator, text: str):
    """Ctrl+A 全选 → 逐字输入"""
    human_move_and_click(page, locator)
    modifier = "Meta" if sys.platform == "darwin" else "Control"
    page.keyboard.press(f"{modifier}+A")
    sleep_ms(rand(80, 160))
    page.keyboard.type(text, delay=rand(70, 150))


def human_scroll(page, total_distance: int = None):
    """分多段滚动，模拟阅读行为"""
    if total_distance is None:
        total_distance = rand(500, 1200)
    scrolled = 0
    while scrolled < total_distance:
        delta = rand(80, 220)
        page.mouse.wheel(0, delta)
        scrolled += delta
        sleep_ms(rand(120, 260))


# ============================================================
# Selector 解析
# ============================================================

def resolve_visible(page, selector_list: list, timeout: int = 1200):
    """从候选 selector 列表中返回第一个可见的 locator"""
    for sel in selector_list:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=timeout):
                return loc
        except Exception:
            continue
    return None


def has_visible(page, selector_list: list, timeout: int = 800) -> bool:
    return resolve_visible(page, selector_list, timeout) is not None


def page_contains_text(page, texts: list) -> bool:
    """检查页面 body 是否包含任一文本"""
    try:
        body = normalize(page.locator("body").inner_text())
    except Exception:
        return False
    return any(t in body for t in texts)


# ============================================================
# Cookie 管理 (aligned with scraper.py)
# ============================================================

def load_cookies_from_file(context) -> bool:
    """从文件读取 cookies 并注入到上下文（与 scraper.py 保持一致）"""
    if not COOKIE_FILE.exists():
        log("Cookie", "load", "INFO", "未找到本地 cookies 文件")
        return False
    try:
        with open(COOKIE_FILE, "r", encoding="utf-8") as f:
            cookies = json.load(f)
        if not cookies:
            return False
        context.add_cookies(cookies)
        log("Cookie", "load", "OK", f"已从本地加载 {len(cookies)} 个 cookie")
        return True
    except Exception as e:
        log("Cookie", "load", "ERROR", str(e))
        return False


def save_cookies_to_file(context):
    """保存当前上下文的所有 cookies 到文件（与 scraper.py 保持一致）"""
    try:
        COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cookies = context.cookies()
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)
        log("Cookie", "save", "OK", f"已保存 {len(cookies)} 个 cookie 到 {COOKIE_FILE}")
    except Exception as e:
        log("Cookie", "save", "ERROR", str(e))


# ============================================================
# 登录流程 (aligned with scraper.py)
# ============================================================

def is_logged_in(page) -> bool:
    """判断当前页面是否处于登录状态（与 scraper.py 保持一致）

    双重检查：
    1. URL 不在登录页
    2. cookie 中有 BOSS 直聘 auth token
    3. 页面文字含登录态特征词（兜底）
    """
    try:
        # 信号1：URL 已跳离登录页
        current_url = page.url
        if "login" in current_url:
            return False

        # 信号2：cookie 中有 auth token（最可靠）
        cookies = page.context.cookies()
        has_token = any(c["name"] in ("wt2", "token", "bst", "zp_token", "zp_uid")
                        for c in cookies)
        if has_token:
            return True

        # 信号3：页面文字含登录态特征词（兜底，处理 cookie 名变化的情况）
        body = normalize(page.locator("body").inner_text())
        logged_in_markers = [
            "我的在线简历", "个人中心", "退出登录",
            "我的简历", "在线简历", "附件简历",
        ]
        for m in logged_in_markers:
            if m in body:
                return True
        # 反查: 有 "登录/注册" 按钮且无 "退出登录" → 未登录
        if "登录/注册" in body and "退出登录" not in body:
            return False
    except Exception:
        pass
    return False


def login_wait(page) -> bool:
    """
    登录流程（与 scraper.py 保持一致）：
    1. 尝试加载本地 Cookie
    2. 访问用户页检查是否自动登录成功
    3. 如果未登录，等待用户手动扫码操作（最多 120s）
    4. 登录成功后，立即保存最新 Cookie

    boss_greeter 增强：自动切换扫码 Tab + 周期性首页导航触发登录跳转
    """
    load_cookies_from_file(page.context)
    target_url = "https://www.zhipin.com/web/user/?ka=header-login"
    log("Login", "goto", "INFO", target_url)
    try:
        page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        pass
    sleep_ms(3000)

    if is_logged_in(page):
        log("Login", "check", "OK", "已通过本地 Cookies 自动登录")
        save_cookies_to_file(page.context)
        return True


    # 尝试切换到扫码登录 tab（boss_greeter 增强功能）
    scan_tabs = [
        'text=扫码登录', '[role="tab"]:has-text("扫码登录")',
        '.login-tab:has-text("扫码登录")', 'text=微信登录',
    ]
    for sel in scan_tabs:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=2000):
                el.click()
                log("Login", "scan_tab", "OK", "已切换到扫码登录")
                sleep_ms(1000)
                break
        except Exception:
            continue

    # 等待用户扫码（最多 120s）
    # 每 6s 导航到首页检查一次 — 扫码成功后 BOSS 会跳转，主动导航能触发跳转完成
    checked_count = 0
    for i in range(40):  # 40 * 3s = 120s
        sleep_ms(3000)
        checked_count += 1

        try:
            if checked_count >= 2:
                checked_count = 0
                try:
                    page.goto("https://www.zhipin.com/",
                              wait_until="domcontentloaded", timeout=15000)
                except Exception:
                    pass
                sleep_ms(1500)

            if is_logged_in(page):
                log("Login", "wait", "OK", f"第{i+1}轮检测到登录状态！")
                save_cookies_to_file(page.context)
                return True
        except Exception as e:
            log("Login", "wait", "WARN", f"本轮检查异常: {e}")

        if (i + 1) % 10 == 0:
            log("Login", "wait", "INFO", f"等待中... ({(i+1)*3}s/120s)")

    log("Login", "wait", "ERROR", "登录超时 (120s)")
    return False


# ============================================================
# 风控检查
# ============================================================

def capture_screenshot(page, name: str = "error") -> str:
    """截图保存到 data/screenshots/"""
    fname = f"{name}-{int(time.time()*1000)}.png"
    path = SCREENSHOT_DIR / fname
    try:
        page.screenshot(path=path, full_page=True)
        log("Screenshot", name, "OK", str(path))
    except Exception as e:
        log("Screenshot", name, "ERROR", str(e))
    return str(path)


def dump_chat_dom(page, label: str = "") -> dict:
    """Dump 聊天页 DOM 结构用于调试对话定位问题。

    Returns dict 含:
      - url: 当前页面 URL
      - title: 页面标题
      - body_text: body 可见文本（前 3000 字）
      - sidebar_html: 疑似侧边栏容器的 outerHTML（前 5000 字）
      - candidate_selectors: 各候选 selector 匹配数量
    """
    result = {"url": page.url, "title": "", "body_text": "", "sidebar_html": "",
              "candidate_selectors": {}, "iframes": [], "body_class": ""}
    try:
        result["title"] = page.title()
    except Exception:
        pass

    # 提取 body 可见文本
    try:
        result["body_text"] = normalize(page.locator("body").inner_text())[:3000]
    except Exception:
        pass

    # 提取 iframe 信息 + body class
    try:
        frame_info = page.evaluate("""() => {
            const iframes = document.querySelectorAll('iframe');
            const info = [];
            for (const f of iframes) {
                info.push({src: f.src, id: f.id, class: f.className, visible: f.offsetWidth > 0});
            }
            return JSON.stringify({iframes: info, bodyClass: document.body.className});
        }""")
        parsed = json.loads(frame_info)
        result["iframes"] = parsed.get("iframes", [])
        result["body_class"] = parsed.get("bodyClass", "")
    except Exception:
        pass

    # 提取左侧侧边栏 DOM 结构
    try:
        result["sidebar_html"] = page.evaluate("""() => {
            const selectors = [
                '.chat-list', '[class*="chat-list"]', '[class*="conversation"]',
                '.user-list', '[class*="sidebar"]', '[class*="left-panel"]',
                '[class*="recent-contact"]', '[class*="contact-list"]',
                'nav', '.nav', '[role="navigation"]',
                'aside', '[class*="aside"]',
                '#wrap', '#app', '[class*="app"]', '[class*="main"]',
                '[class*="container"]', '[class*="wrapper"]',
            ];
            let html = '';
            for (const sel of selectors) {
                try {
                    const els = document.querySelectorAll(sel);
                    for (let i = 0; i < Math.min(els.length, 3); i++) {
                        const el = els[i];
                        html += `\n--- ${sel}[${i}] <${el.tagName}> class="${el.className}" ---\n${el.outerHTML.substring(0, 2000)}\n`;
                    }
                } catch(e) {}
            }
            // 如果上面都没找到，dump body 的直接子元素结构
            if (!html.trim()) {
                const bodyChildren = document.body.children;
                for (let i = 0; i < Math.min(bodyChildren.length, 15); i++) {
                    const el = bodyChildren[i];
                    html += `\n--- body>children[${i}] <${el.tagName}> class="${el.className}" ---\n${el.outerHTML.substring(0, 1500)}\n`;
                }
            }
            return html.substring(0, 8000);
        }""")
    except Exception as e:
        result["sidebar_html"] = f"(error: {e})"

    # 统计各候选 selector 匹配数
    candidate_selectors = [
        '.chat-list-item', '[class*="chat-item"]', '[class*="conversation-item"]',
        '.user-list-item', '.recent-contact-item', '[class*="contact-item"]',
        '.chat-list a', '.chat-list li', '[class*="chat-list"] > div',
        '.chat-list > *', 'a[href*="chat"]',
        '[class*="message-item"]', '[class*="chat-card"]',
    ]
    for sel in candidate_selectors:
        try:
            count = page.locator(sel).count()
            if count > 0:
                result["candidate_selectors"][sel] = count
        except Exception:
            pass

    # 输出调试日志
    log("DEBUG", label, "DOM", f"URL: {result['url']}")
    log("DEBUG", label, "DOM", f"Title: {result['title']}")
    log("DEBUG", label, "DOM", f"body_class: {result.get('body_class', '?')}")
    if result.get("iframes"):
        log("DEBUG", label, "DOM", f"iframes({len(result['iframes'])}): {result['iframes']}")
    else:
        log("DEBUG", label, "DOM", "iframes: 无")
    if result["candidate_selectors"]:
        log("DEBUG", label, "DOM", f"候选selector匹配: {result['candidate_selectors']}")
    else:
        log("DEBUG", label, "DOM", "WARN: 所有候选selector匹配数均为0！")

    # 保存完整 dump 到文件
    dump_file = DATA_DIR / f"chat_dom_{label.replace(' ', '_').replace('/', '_')}_{int(time.time()*1000)}.json"
    dump_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log("DEBUG", label, "DOM", f"完整dump -> {dump_file}")

    return result


def dump_job_detail_debug(page, label: str = "", stage: str = "job-detail") -> dict:
    """Dump 职位详情页 CTA / 弹窗相关 DOM，用于定位按钮缺失问题。"""
    result = {
        "url": page.url,
        "title": "",
        "stage": stage,
        "body_text": "",
        "body_class": "",
        "candidate_selectors": {},
        "visible_candidates": {},
        "job_detail_html": "",
        "action_area_html": "",
        "dialog_html": "",
        "buttons": [],
    }
    try:
        result["title"] = page.title()
    except Exception:
        pass

    try:
        result["body_text"] = normalize(page.locator("body").inner_text())[:5000]
    except Exception:
        pass

    try:
        result["body_class"] = page.evaluate("""() => document.body.className""")
    except Exception:
        pass

    selectors_to_probe = {
        "container": SELECTORS["job_detail"]["container"],
        "chat_now": SELECTORS["job_detail"]["chat_now"],
        "continue_chat": SELECTORS["job_detail"]["continue_chat"],
        "continue_chat_popup": SELECTORS["job_detail"]["continue_chat_popup"],
        "dialog_container": SELECTORS["job_detail"]["dialog_container"],
        "risk_notice": SELECTORS["job_detail"]["risk_notice"],
    }
    for group, sels in selectors_to_probe.items():
        for sel in sels:
            key = f"{group}::{sel}"
            try:
                loc = page.locator(sel)
                count = loc.count()
                result["candidate_selectors"][key] = count
                if count > 0:
                    visible = 0
                    max_probe = min(count, 3)
                    for i in range(max_probe):
                        try:
                            if loc.nth(i).is_visible(timeout=300):
                                visible += 1
                        except Exception:
                            pass
                    result["visible_candidates"][key] = visible
            except Exception as e:
                result["candidate_selectors"][key] = f"error: {e}"

    try:
        result["job_detail_html"] = page.evaluate("""() => {
            const selectors = ['.job-detail-box', '.job-detail-body', '.job-detail-card', '#main', '#wrap'];
            for (const sel of selectors) {
                const el = document.querySelector(sel);
                if (el) return el.outerHTML.substring(0, 6000);
            }
            return document.body ? document.body.outerHTML.substring(0, 6000) : '';
        }""")
    except Exception as e:
        result["job_detail_html"] = f"(error: {e})"

    try:
        result["action_area_html"] = page.evaluate("""() => {
            const candidates = Array.from(document.querySelectorAll('button, a, span, div'));
            for (const el of candidates) {
                const text = (el.textContent || '').replace(/\s+/g, ' ').trim();
                const ka = el.getAttribute('ka') || '';
                const cls = el.className || '';
                if (text.includes('立即沟通') || text.includes('继续沟通') ||
                    ka.includes('communicate') || ka.includes('chat') ||
                    String(cls).includes('op-btn-chat')) {
                    return (el.outerHTML || '').substring(0, 4000);
                }
            }
            return '';
        }""")
    except Exception as e:
        result["action_area_html"] = f"(error: {e})"

    try:
        result["dialog_html"] = page.evaluate("""() => {
            const selectors = ['.dialog-wrap', '.dialog-container', '.boss-dialog__wrapper', '[class*="dialog"]', '[class*="popup"]'];
            for (const sel of selectors) {
                const el = document.querySelector(sel);
                if (el) return el.outerHTML.substring(0, 5000);
            }
            return '';
        }""")
    except Exception as e:
        result["dialog_html"] = f"(error: {e})"

    try:
        result["buttons"] = page.evaluate("""() => {
            const nodes = Array.from(document.querySelectorAll('button, a, span[role="button"], .btn, [ka], [role="button"]'));
            return nodes.slice(0, 80).map((el, idx) => ({
                idx,
                tag: el.tagName,
                text: (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 80),
                class: String(el.className || '').slice(0, 120),
                id: el.id || '',
                role: el.getAttribute('role') || '',
                ka: el.getAttribute('ka') || '',
                href: el.getAttribute('href') || '',
                visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
            }));
        }""")
    except Exception as e:
        result["buttons"] = [{"error": str(e)}]

    log("DEBUG", label, "JOBDOM", f"stage={stage} url={result['url']}")
    log("DEBUG", label, "JOBDOM", f"title={result['title']}")
    log("DEBUG", label, "JOBDOM", f"body_class={result.get('body_class', '')}")
    log("DEBUG", label, "JOBDOM", f"selector命中={result['candidate_selectors']}")
    log("DEBUG", label, "JOBDOM", f"selector可见={result['visible_candidates']}")
    if result.get("action_area_html"):
        log("DEBUG", label, "JOBDOM", f"action_area_html={result['action_area_html'][:500]}")
    if result.get("dialog_html"):
        log("DEBUG", label, "JOBDOM", f"dialog_html={result['dialog_html'][:500]}")
    if result.get("buttons"):
        log("DEBUG", label, "JOBDOM", f"buttons样本={result['buttons'][:12]}")

    dump_file = DATA_DIR / f"job_detail_dom_{stage}_{label.replace(' ', '_').replace('/', '_')}_{int(time.time()*1000)}.json"
    dump_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log("DEBUG", label, "JOBDOM", f"完整dump -> {dump_file}")
    return result


def inspect_stop_signals(page) -> Optional[dict]:
    """
    检查两类风险信号：
    1. 每日限额: "今日沟通已达上限" | .dialog-container + 文本匹配
    2. 风险验证: "安全验证" | "验证码" | "异常操作"

    Returns:
        {"stop": True, "reason": "daily-limit"|"risk-detected"} or None
    """
    # 检查每日限额
    hit_daily = has_visible(page, SELECTORS["job_detail"]["daily_limit"], timeout=800)
    if not hit_daily:
        dialogs = SELECTORS["job_detail"]["dialog_container"]
        if has_visible(page, dialogs, timeout=800):
            hit_daily = page_contains_text(page, [
                "今日沟通已达上限", "今日沟通次数已达上限", "沟通已达上限",
            ])

    if hit_daily:
        log("STOP", "inspect", "WARN", "命中每日限额")
        return {"stop": True, "reason": "daily-limit"}

    # 检查风险验证
    hit_risk = has_visible(page, SELECTORS["job_detail"]["risk_notice"], timeout=600)
    if not hit_risk:
        hit_risk = page_contains_text(page, ["安全验证", "请完成验证", "验证码", "异常操作"])

    if hit_risk:
        log("STOP", "inspect", "WARN", "命中风险验证")
        capture_screenshot(page, "risk-detected")
        return {"stop": True, "reason": "risk-detected"}

    return None


# ============================================================
# 单岗位打招呼流程
# ============================================================

def process_single_job(page, job: dict, resume_summary: str,
                       greeting_file: Path) -> dict:
    """
    处理单个岗位：
    1. 打开详情页 → 风控检查
    2. 检查是否已沟通过
    3. 点"立即沟通" → 浮窗"继续沟通"跳转聊天页
    4. 在聊天页找 #chat-input，填入 LLM 招呼语 → .btn-send 发送
    5. 查 .chat-message 中最新消息验证
    """
    label = f"{job.get('company', '-')} / {job.get('name', '-')}"
    job_url = job.get("link", "")
    if not job_url:
        log("SKIP", label, "WARN", "缺少岗位链接")
        return {"status": "skip", "reason": "missing-link"}

    if not job_url.startswith("http"):
        job_url = f"https://www.zhipin.com{job_url}"

    log("JOB", label, "START", job_url)

    # Step 1: 打开详情页
    try:
        page.goto(job_url, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        log("JOB", label, "ERROR", f"导航失败: {e}")
        return {"status": "failed", "reason": str(e)[:80]}

    try:
        page.wait_for_load_state("networkidle")
    except Exception:
        pass
    sleep_ms(rand(1000, 2000))

    detail_container = resolve_visible(page, SELECTORS["job_detail"]["container"], timeout=5000)
    if detail_container:
        log("JOB", label, "INFO", "职位详情容器已出现")
    else:
        log("JOB", label, "WARN", "职位详情容器未出现")
    human_scroll(page, rand(200, 500))
    dump_job_detail_debug(page, label, stage="after-load")

    # Step 2: 职位状态检查（招聘中 / 职位已关闭）
    hit_open = has_visible(page, SELECTORS["job_detail"]["job_open"], timeout=1000)
    if not hit_open:
        hit_open = page_contains_text(page, ["招聘中", "正在招聘", "急聘"])

    hit_closed = has_visible(page, SELECTORS["job_detail"]["job_closed"], timeout=1000)
    if not hit_closed:
        hit_closed = page_contains_text(page, ["职位已关闭", "职位关闭", "该职位已关闭"])

    if hit_closed and not hit_open:
        log("SKIP", label, "INFO", "职位已关闭，跳过后续流程")
        dump_job_detail_debug(page, label, stage="job-closed")
        return {"status": "job-closed"}

    if hit_open:
        log("JOB", label, "INFO", "检测到招聘中，继续后续流程")
    elif hit_closed:
        log("JOB", label, "WARN", "同时命中招聘中/关闭态邻近文案，暂不跳过")

    # Step 2: 风控检查
    stop = inspect_stop_signals(page)
    if stop:
        dump_job_detail_debug(page, label, stage=f"stop-{stop['reason']}")
        return {"status": stop["reason"], "stop": True}

    # Step 3: 检查是否已经沟通过
    if has_visible(page, SELECTORS["job_detail"]["continue_chat"], timeout=1500):
        log("SKIP", label, "INFO", '页面显示「继续沟通」，此前已打过招呼')
        dump_job_detail_debug(page, label, stage="already-contacted")
        return {"status": "already-contacted"}

    # Step 5: 提取 encryptId → 设置网络拦截 → 点"立即沟通"
    # 从 job link 提取 encryptId（格式: /job_detail/{encryptId}.html）
    encrypt_id = ""
    link = job.get("link", "")
    m = re.search(r"/job_detail/([a-zA-Z0-9_-]+)", link)
    if m:
        encrypt_id = m.group(1)
        log("CHAT", label, "INFO", f"encryptId={encrypt_id}")
    else:
        log("CHAT", label, "WARN", f"无法从link提取encryptId: {link}")

    # 设置网络响应拦截 — 全量捕获，写入 debug 文件
    captured_responses = []

    def on_response(response):
        url = response.url
        # 只拦截 XHR/Fetch API 请求（跳过静态资源）
        if any(kw in url for kw in ["zhipin.com", "/wapi/", "/api/", ".json", "bosszp"]):
            if not any(url.endswith(ext) for ext in [".js", ".css", ".png", ".jpg", ".gif", ".woff", ".svg", ".ico"]):
                try:
                    body = response.text()
                    captured_responses.append({"url": url, "status": response.status, "body": body[:5000]})
                except Exception:
                    captured_responses.append({"url": url, "status": response.status, "body": "(binary/error)"})

    page.on("response", on_response)

    chat_btn = None
    chat_btn_selector = ""
    for attempt in range(4):
        per_try_timeout = 2500 if attempt < 3 else 4000
        if attempt > 0:
            human_scroll(page, rand(250, 700))
            sleep_ms(rand(800, 1800))
        log("CHAT", label, "INFO", f"查找详情页沟通按钮（继续沟通/立即沟通） ({attempt+1}/4, timeout={per_try_timeout}ms)")
        for sel in SELECTORS["job_detail"]["chat_now"]:
            try:
                loc = page.locator(sel)
                count = loc.count()
                if count == 0:
                    continue
                candidate = loc.first
                if candidate.is_visible(timeout=per_try_timeout):
                    chat_btn = candidate
                    chat_btn_selector = sel
                    break
                log("DEBUG", label, "CHATBTN", f"selector未可见: {sel} count={count}")
            except Exception as e:
                log("DEBUG", label, "CHATBTN", f"selector异常: {sel} err={str(e)[:120]}")
        if chat_btn:
            log("CHAT", label, "INFO", f"命中详情页沟通按钮 selector: {chat_btn_selector}")
            break
        dump_job_detail_debug(page, label, stage=f"chat-now-miss-attempt-{attempt+1}")

    if not chat_btn:
        page.remove_listener("response", on_response)
        log("SKIP", label, "WARN", "未找到详情页沟通按钮（继续沟通/立即沟通）")
        capture_screenshot(page, f"missing-chat-button-{label.replace('/', '_')[:40]}")
        dump_job_detail_debug(page, label, stage="missing-chat-button")
        return {"status": "missing-chat-button"}

    human_move_and_click(page, chat_btn)
    log("CHAT", label, "INFO", "已点击详情页沟通按钮，等待浮窗...")

    # Step 5b: 主动等待浮窗弹出（BOSS 发送默认招呼语后弹出浮窗）
    popup_appeared = False
    popup_selector = ""
    for attempt in range(4):
        per_try_timeout = 2500 if attempt < 3 else 4000
        log("CHAT", label, "INFO", f"等待浮窗 ({attempt+1}/4, timeout={per_try_timeout}ms)")
        for sel in SELECTORS["job_detail"]["continue_chat_popup"]:
            try:
                popup = page.locator(sel).first
                if popup.is_visible(timeout=per_try_timeout):
                    popup_appeared = True
                    popup_selector = sel
                    break
            except Exception:
                continue
        if popup_appeared:
            log("CHAT", label, "INFO", f"浮窗已弹出: {popup_selector}")
            break
        sleep_ms(1200)
        dump_job_detail_debug(page, label, stage=f"popup-wait-{attempt+1}")
    if not popup_appeared:
        page.remove_listener("response", on_response)
        log("SKIP", label, "WARN", "浮窗未弹出（可能直接跳转或风控）")
        capture_screenshot(page, "no-popup")
        dump_job_detail_debug(page, label, stage="no-popup")
        return {"status": "no-popup"}

    # Step 6: 从网络响应提取 chat URL 并跳转聊天页
    page.remove_listener("response", on_response)

    # 保存网络 dump 供调试
    dump_file = DATA_DIR / f"network_dump_{int(time.time()*1000)}.json"
    dump_data = []
    for r in captured_responses:
        dump_data.append({"url": r["url"], "status": r["status"], "body": r["body"][:2000]})
    dump_file.write_text(json.dumps(dump_data, ensure_ascii=False, indent=2), encoding="utf-8")
    log("DEBUG", label, "INFO", f"全量网络响应({len(captured_responses)}条) -> {dump_file}")

    chat_url = None

    # 6a: 从 friend/add.json 响应提取 securityId / encBossId
    for resp in captured_responses:
        body = resp.get("body", "")
        url = resp.get("url", "")
        if "friend/add.json" in url or "friend/add" in url:
            try:
                data = json.loads(body)
                zp = data.get("zpData", {})
                sid = zp.get("securityId", "")
                bid = zp.get("encBossId", "")
                if sid:
                    log("CHAT", label, "INFO", f"friend/add.json -> securityId={sid[:40]}...")
                    chat_url = f"https://www.zhipin.com/web/geek/chat?securityId={sid}&jobId={encrypt_id}"
                    break
                elif bid:
                    log("CHAT", label, "INFO", f"friend/add.json -> encBossId={bid}")
                    chat_url = f"https://www.zhipin.com/web/geek/chat?id={bid}&jobId={encrypt_id}"
                    break
            except json.JSONDecodeError:
                pass

        # 通用正则兜底
        for pattern in [r'"securityId"\s*:\s*"([^"]+~~)"',
                        r'"encBossId"\s*:\s*"([a-zA-Z0-9_-]+)"',
                        r'"encryptChatId"\s*:\s*"([a-zA-Z0-9_-]+)"']:
            m2 = re.search(pattern, body)
            if m2:
                cid = m2.group(1)
                if cid != encrypt_id:
                    log("CHAT", label, "INFO", f"网络响应 -> id={cid[:40]} ({url[:100]})")
                    chat_url = f"https://www.zhipin.com/web/geek/chat?id={cid}&jobId={encrypt_id}"
                    break
        if chat_url:
            break

    # 6b: URL 标准化 + 导航（带重试）
    nav_ok = False
    if chat_url:
        if chat_url.startswith("//"):
            chat_url = f"https:{chat_url}"
        elif chat_url.startswith("/"):
            chat_url = f"https://www.zhipin.com{chat_url}"
        elif not chat_url.startswith("http"):
            chat_url = f"https://www.zhipin.com/{chat_url}"
        log("CHAT", label, "INFO", f"聊天URL: {chat_url[:100]}...")
        for nav_attempt in range(3):
            try:
                page.goto(chat_url, wait_until="load", timeout=45000)
                nav_ok = True
                log("CHAT", label, "INFO", "已跳转到聊天页")
                break
            except Exception as e:
                log("CHAT", label, "WARN", f"导航 {nav_attempt+1}/3 失败: {str(e)[:80]}")
                if nav_attempt < 2:
                    sleep_ms(3000)
        if not nav_ok:
            log("CHAT", label, "ERROR", "网络URL跳转失败（3次重试均超时），尝试点击兜底")

    if not nav_ok:
        # 6c: 兜底 — 点击浮窗"继续沟通"，由 BOSS 原生处理跳转
        continue_btn = resolve_visible(page, SELECTORS["job_detail"]["continue_chat_popup"], timeout=3000)
        if not continue_btn:
            log("SKIP", label, "WARN", "浮窗未找到「继续沟通」按钮")
            capture_screenshot(page, "missing-continue-chat-popup")
            dump_job_detail_debug(page, label, stage="missing-popup-continue")
            return {"status": "missing-popup-continue"}

        new_page = None
        def handle_popup(p):
            nonlocal new_page
            new_page = p

        page.context.on("page", handle_popup)
        url_before = page.url

        human_move_and_click(page, continue_btn)
        log("CHAT", label, "INFO", "已点击浮窗「继续沟通」，等待跳转...")

        chat_url = None
        for i in range(15):  # 最多等 15s
            sleep_ms(1000)
            if new_page:
                chat_url = new_page.url
                log("CHAT", label, "INFO", "检测到新标签页")
                break
            if page.url != url_before and "geek/chat" in page.url:
                chat_url = page.url
                log("CHAT", label, "INFO", "当前页已跳转到聊天页")
                break

        page.context.remove_listener("page", handle_popup)

        if chat_url:
            if chat_url.startswith("//"):
                chat_url = f"https:{chat_url}"
            elif chat_url.startswith("/"):
                chat_url = f"https://www.zhipin.com{chat_url}"
            elif not chat_url.startswith("http"):
                chat_url = f"https://www.zhipin.com/{chat_url}"

            if new_page:
                try:
                    new_page.close()
                except Exception:
                    pass

            if "geek/chat" not in page.url:
                try:
                    page.goto(chat_url, wait_until="load", timeout=45000)
                    log("CHAT", label, "INFO", "兜底点击跳转成功")
                except Exception as e:
                    log("CHAT", label, "ERROR", f"兜底点击跳转也失败: {str(e)[:80]}")
                    capture_screenshot(page, "goto-chat-failed")
                    return {"status": "goto-chat-failed", "reason": str(e)[:80]}
            else:
                log("CHAT", label, "INFO", "已在聊天页，无需跳转")
        else:
            log("SKIP", label, "WARN", "点击无跳转且无网络URL，跳过")
            capture_screenshot(page, "no-chat-url")
            dump_job_detail_debug(page, label, stage="popup-click-no-nav")
            return {"status": "no-chat-url"}

    # Step 7: 在聊天页左侧对话列表中定位正确对话
    log("CHAT", label, "INFO", "等待聊天 SPA 渲染...")

    # 7a: 主动等待聊天页面内容出现（SPA 渲染比 domcontentloaded/networkidle 晚很多）
    chat_page_ready = False
    # 等待标志：聊天容器 / 对话列表 / 消息区域 任一出现
    chat_page_markers = [
        '.chat-list', '[class*="chat-list"]', '[class*="conversation-list"]',
        '.chat-container', '[class*="chat-container"]', '[class*="chat-wrapper"]',
        '#chat-input', '.chat-message',
        '[class*="message-list"]', '[class*="contact"]',
        '#wrap', '#app', '[class*="main-content"]',
    ]
    for wait_round in range(15):  # 最多等 ~30s
        sleep_ms(2000)
        for marker in chat_page_markers:
            try:
                if page.locator(marker).first.is_visible(timeout=500):
                    log("CHAT", label, "INFO", f"SPA 已渲染（{wait_round+1}轮, marker={marker}）")
                    chat_page_ready = True
                    break
            except Exception:
                pass
        if chat_page_ready:
            break
        # 检查 iframe
        try:
            frame_count = len(page.frames)
            if frame_count > 1:
                log("CHAT", label, "INFO", f"检测到 {frame_count} 个 frame（含iframe）")
                for f in page.frames:
                    log("DEBUG", label, "FRAME", f"name={f.name} url={f.url[:100]}")
        except Exception:
            pass
        if wait_round % 3 == 2:
            log("CHAT", label, "INFO", f"等待 SPA 渲染... ({wait_round+1}/15)")

    if not chat_page_ready:
        log("CHAT", label, "WARN", "SPA 渲染超时，继续尝试...")

    # ---- DEBUG: dump 聊天页 DOM 结构 ----
    dom_info = dump_chat_dom(page, label)
    capture_screenshot(page, f"chat-debug-{label.replace('/', '_')[:40]}")
    log("DEBUG", label, "DOM", f"body_text前500字: {dom_info.get('body_text', '')[:500]}")

    job_name = job.get("name", "")
    company = job.get("company", "") or job.get("companyName", "")
    match_keywords = build_chat_match_keywords(company, job_name)
    log("DEBUG", label, "MATCH", f"目标: company='{company}' job_name='{job_name}'")
    log("DEBUG", label, "MATCH", f"关键词: {match_keywords}")

    # 7b: 如果有 iframe，尝试切换到 iframe 内查找对话列表
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        try:
            frame_url = frame.url
            log("DEBUG", label, "IFRAME", f"检查iframe: {frame_url[:120]}")
            # 在 iframe 内搜索候选 selector
            for sel in ['.chat-list-item', '[class*="chat-item"]', '[class*="conversation"]']:
                try:
                    count = frame.locator(sel).count()
                    if count > 0:
                        log("CHAT", label, "INFO", f"iframe 内找到 {sel}: {count}个元素，切换操作到iframe...")
                        # 注意：Playwright 切换 iframe 复杂，先在主页面继续
                except Exception:
                    pass
        except Exception:
            pass

    # 先检查 #chat-input 是否已可见（对话可能已自动选中）
    chat_active = False
    try:
        if page.locator(SELECTORS["chat_page"]["message_input"]).first.is_visible(timeout=2000):
            chat_active = True
            log("CHAT", label, "INFO", "对话已自动选中")
    except Exception:
        pass

    if not chat_active:
        log("CHAT", label, "INFO", f"在对话列表中定位: {company} / {job_name[:20]}")

        conversation_selectors = [
            '.chat-list-item', '[class*="chat-item"]', '[class*="conversation-item"]',
            '.user-list-item', '.recent-contact-item', '[class*="contact-item"]',
            '.chat-list a', '.chat-list li', '[class*="chat-list"] > div',
            '.chat-list > *', 'a[href*="chat"]',
            '[class*="message-item"]', '[class*="chat-card"]',
            '.user-list [role="listitem"]', '.user-list li', '.friend-content-warp', '.friend-content',
        ]

        best_item = None
        best_score = 0
        best_text = ""
        best_selector = ""
        for sel in conversation_selectors:
            try:
                items = page.locator(sel)
                count = items.count()
                if count == 0:
                    continue
                log("DEBUG", label, "SELECTOR", f"'{sel}' 匹配 {count} 个元素")
                for j in range(min(count, 40)):
                    try:
                        item = items.nth(j)
                        text = normalize(item.inner_text())
                    except Exception:
                        continue
                    score = score_chat_item_text(text, company, job_name)
                    matched_keywords = [kw for kw in match_keywords if kw and kw in text]
                    if j < 8 or score > 0:
                        log("DEBUG", label, "ITEM", f"[{j}] score={score} keywords={matched_keywords[:4]} text='{text[:120]}'")
                    if score > best_score:
                        best_item = item
                        best_score = score
                        best_text = text
                        best_selector = sel
            except Exception as e:
                log("DEBUG", label, "SELECTOR", f"'{sel}' 异常: {e}")

        if best_item and best_score > 0:
            try:
                human_move_and_click(page, best_item)
                log("CHAT", label, "INFO", f"已点击最佳对话 score={best_score} selector={best_selector} text={best_text[:80]}")
                sleep_ms(rand(1500, 2500))
            except Exception as e:
                log("CHAT", label, "WARN", f"点击最佳对话失败: {e}")
        else:
            log("CHAT", label, "WARN", "未匹配到目标对话，尝试点击最新对话（兜底）")
            for sel in ['.chat-list-item:first-child', '[class*="chat-item"]:first-child',
                        '.chat-list > :first-child', 'a[href*="chat"]:first-child',
                        '.user-list [role="listitem"]:first-child', '.user-list li:first-child']:
                try:
                    first = page.locator(sel).first
                    if first.is_visible(timeout=1000):
                        first_text = normalize(first.inner_text())
                        human_move_and_click(page, first)
                        log("CHAT", label, "INFO", f"已点击兜底对话 {sel}: {first_text[:80]}")
                        sleep_ms(rand(1500, 2500))
                        break
                except Exception as e:
                    log("DEBUG", label, "FALLBACK", f"'{sel}' 失败: {e}")

    # Step 8: 等待聊天输入框 #chat-input 就绪
    chat_input_ready = False
    for attempt in range(10):  # 最多等 ~25s
        sleep_ms(2500)
        try:
            if page.locator(SELECTORS["chat_page"]["message_input"]).first.is_visible(timeout=2000):
                chat_input_ready = True
                break
        except Exception:
            pass
        if attempt in (2, 5, 8):
            dump_chat_dom(page, label)
        log("CHAT", label, "INFO", f"等待聊天输入框... ({attempt+1}/10)")

    if not chat_input_ready:
        log("SKIP", label, "WARN", "聊天页加载超时，未找到 #chat-input")
        capture_screenshot(page, "chat-page-timeout")
        dump_chat_dom(page, label)
        return {"status": "chat-page-timeout"}

    # Step 9: 填入 LLM 定制招呼语到聊天输入框
    log("CHAT", label, "INFO", "填入LLM定制招呼语")

    try:
        greeting = generate_greeting(resume_summary, job, greeting_file)
        log("LLM", label, "OK", f"招呼语({len(greeting)}字): {greeting[:60]}...")
    except Exception as e:
        log("LLM", label, "ERROR", str(e))
        greeting = f"您好，我对{job.get('name', '这个岗位')}很感兴趣，我的背景和项目经验与岗位要求很匹配，期待与您交流！"

    # 9a: 先点击输入框获取焦点 → 清空 → 键盘输入（兼容 React/Vue 响应式框架）
    sleep_ms(rand(200, 400))
    chat_input = page.locator(SELECTORS["chat_page"]["message_input"]).first
    human_move_and_click(page, chat_input)
    sleep_ms(rand(200, 400))

    # 全选清空（模拟 Ctrl+A → 键盘输入，触发框架数据绑定）
    modifier = "Meta" if sys.platform == "darwin" else "Control"
    page.keyboard.press(f"{modifier}+A")
    sleep_ms(rand(100, 200))
    page.keyboard.press("Backspace")
    sleep_ms(rand(100, 200))

    # 方式1: 键盘逐字输入（最可靠地触发 Vue/React 响应式）
    try:
        page.keyboard.type(greeting, delay=rand(30, 80))
        log("CHAT", label, "INFO", f"已键盘输入 {len(greeting)} 字")
    except Exception as e:
        log("CHAT", label, "WARN", f"键盘输入失败，尝试JS填入: {e}")
        # 方式2: JS 填入 + 多种事件触发
        greeting_escaped = greeting.replace("`", "\\`").replace("$", "\\$").replace("\n", "\\n")
        page.evaluate(f"""(text) => {{
            const el = document.querySelector('{SELECTORS["chat_page"]["message_input"]}');
            if (el) {{
                el.focus();
                el.innerText = text;
                el.textContent = text;
                // 触发多种事件让框架感知变化
                ['input', 'change', 'keyup', 'keydown', 'compositionend'].forEach(name => {{
                    el.dispatchEvent(new Event(name, {{ bubbles: true, cancelable: true }}));
                    el.dispatchEvent(new InputEvent(name, {{ bubbles: true, cancelable: true, inputType: 'insertText', data: text }}));
                }});
            }}
        }}""", greeting_escaped)

    sleep_ms(rand(500, 800))

    # 9b: DEBUG — dump 输入框区域 DOM（含发送按钮）
    try:
        toolbar_html = page.evaluate("""() => {
            // 找到 #chat-input 的父容器中的工具栏
            const input = document.querySelector('#chat-input');
            if (!input) return '(未找到 #chat-input)';
            let parent = input.parentElement;
            let html = '<' + parent.tagName + ' class="' + parent.className + '">';
            // 上溯2层找工具栏
            for (let i = 0; i < 3 && parent; i++) {
                parent = parent.parentElement;
                if (parent) html += '\\n<' + parent.tagName + ' class="' + parent.className + '">';
            }
            // 找所有 button
            const buttons = document.querySelectorAll('button');
            html += '\\n页面所有button (' + buttons.length + '个):';
            for (let i = 0; i < Math.min(buttons.length, 15); i++) {
                const b = buttons[i];
                html += '\\n  [' + i + '] <' + b.tagName + ' class="' + b.className +
                        '" disabled=' + b.disabled + ' text="' + (b.textContent || '').substring(0, 30) + '">';
            }
            return html.substring(0, 3000);
        }""")
        log("DEBUG", label, "TOOLBAR", toolbar_html[:500])
    except Exception as e:
        log("DEBUG", label, "TOOLBAR", f"dump失败: {e}")
    capture_screenshot(page, f"before-send-{label.replace('/', '_')[:40]}")

    # Step 10: 发送招呼语
    log("CHAT", label, "INFO", "输入完成，自动触发发送按钮")
    send_ok = False

    # 10a: 尝试点击发送按钮（多组候选 selector）
    send_btn_selectors = [
        SELECTORS["chat_page"]["send_btn"],      # .btn-send
        'button:has-text("发送")',                # button文本
        '[class*="send"]',                       # class含send
        'button[class*="send"]',                 # button class含send
        '.chat-footer button',                   # 聊天底部按钮
        '[class*="chat-input"] + button',        # 输入框相邻button
        '[class*="chat"] button:last-child',     # 聊天区最后button
        'button:has-text("Send")',
        '#chat-input + button',
        '#chat-input ~ button',
        '.toolbar button:last-child',
        '[class*="toolbar"] button',
    ]
    send_btn = None
    for btn_sel in send_btn_selectors:
        try:
            btn = page.locator(btn_sel).first
            if btn.is_visible(timeout=800):
                send_btn = btn
                log("CHAT", label, "INFO", f"找到发送按钮: {btn_sel}")
                break
        except Exception:
            continue

    if send_btn:
        # 检查按钮是否 disabled
        try:
            is_disabled = send_btn.is_disabled()
            log("DEBUG", label, "SEND", f"发送按钮 disabled={is_disabled}")
            if is_disabled:
                # 尝试点击输入框触发框架启用按钮
                page.keyboard.press("Space")
                sleep_ms(200)
                page.keyboard.press("Backspace")
                sleep_ms(300)
        except Exception:
            pass
        human_move_and_click(page, send_btn)
        send_ok = True
        log("CHAT", label, "INFO", "已点击发送按钮")
    else:
        log("CHAT", label, "WARN", "未找到发送按钮，尝试键盘发送")

    if not send_ok:
        # 10b: 键盘发送
        # 确保焦点在输入框
        try:
            page.locator(SELECTORS["chat_page"]["message_input"]).first.click()
            sleep_ms(rand(200, 400))
        except Exception:
            pass
        page.keyboard.press("Enter")
        log("CHAT", label, "INFO", "已按 Enter 发送")
        sleep_ms(500)
        # 如果 Enter 不行，试试 Ctrl+Enter
        try:
            sent_check = page.evaluate("""() => {
                const items = document.querySelectorAll('.chat-message .item-myself');
                return items.length;
            }""")
            if sent_check == 0:
                log("CHAT", label, "INFO", "Enter未生效，尝试Ctrl+Enter")
                page.keyboard.press(f"{modifier}+Enter")
                log("CHAT", label, "INFO", "已按 Ctrl+Enter")
        except Exception:
            pass

    sleep_ms(rand(800, 1400))

    # Step 11: 验证 — 查 .chat-message 中最新的 .item-myself
    sleep_ms(rand(1000, 2000))
    sent = False
    try:
        last_msg_text = page.evaluate("""() => {
            const items = document.querySelectorAll('.chat-message .item-myself');
            if (!items.length) return '';
            const last = items[items.length - 1];
            const content = last.querySelector('.message-content .text');
            return content ? content.textContent.trim() : '';
        }""")
        if last_msg_text and len(last_msg_text) > 2:
            sent = True
            log("CHAT", label, "INFO", f"验证: 已发送 ({last_msg_text[:40]}...)")
        else:
            log("CHAT", label, "WARN", "验证: 未检测到已发送消息")
            # DEBUG: dump 聊天记录区域
            try:
                msg_count = page.evaluate("""() => {
                    const items = document.querySelectorAll('.chat-message .item-myself,.chat-message .item-friend');
                    return items.length;
                }""")
                log("DEBUG", label, "VERIFY", f"聊天记录item总数: {msg_count}")
                capture_screenshot(page, f"verify-fail-{label.replace('/', '_')[:40]}")
            except Exception:
                pass
    except Exception as e:
        log("CHAT", label, "WARN", f"验证失败: {e}")
        sent = page_contains_text(page, [greeting[:15]])
        log("DEBUG", label, "VERIFY", f"降级验证: 页面含问候语={sent}")

    status = "greeted" if sent else "submitted"
    log("JOB", label, "DONE", status)

    return {"status": status, "stop": False}


# ============================================================
# 数据加载
# ============================================================

def load_accepted_jobs() -> list[dict]:
    """
    从 match_decisions.json + match_result_llm.json 加载 accept 的岗位。
    返回按 _salary_min_k 降序排列的岗位列表。
    """
    decisions_path = DATA_DIR / "match_decisions.json"
    match_path = DATA_DIR / "match_result_llm.json"

    if not decisions_path.exists():
        log("Load", "decisions", "ERROR", f"文件不存在: {decisions_path}")
        return []
    if not match_path.exists():
        log("Load", "match", "ERROR", f"文件不存在: {match_path}")
        return []

    with open(decisions_path, "r", encoding="utf-8") as f:
        decisions = json.load(f)
    with open(match_path, "r", encoding="utf-8") as f:
        match_data = json.load(f)

    # accepted 的索引
    accepted_indices = {int(k) for k, v in decisions.items() if v == "accept"}

    # 合并 full + partial 列表（按原顺序）
    full_jobs = match_data.get("full", [])
    partial_jobs = match_data.get("partial", [])
    all_reviewable = full_jobs + partial_jobs

    accepted = []
    for i, job in enumerate(all_reviewable):
        if i in accepted_indices:
            job_copy = dict(job)
            job_copy["_match_index"] = i
            accepted.append(job_copy)

    # 按最低月薪降序
    accepted.sort(key=lambda j: j.get("_salary_min_k") or 0, reverse=True)

    log("Load", "accepted", "OK", f"加载 {len(accepted)} 个 accept 岗位 (共 {len(decisions)} 决策)")
    return accepted


def load_history() -> list:
    """加载打招呼历史"""
    hist_path = DATA_DIR / "greet_history.json"
    if hist_path.exists():
        with open(hist_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_history(history: list):
    """保存打招呼历史"""
    hist_path = DATA_DIR / "greet_history.json"
    with open(hist_path, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def find_greeted_links(history: list) -> set:
    """从历史记录中提取已打招呼的岗位链接"""
    return {h.get("link", "") for h in history
            if h.get("status") in ("greeted", "submitted", "already-contacted")}


# ============================================================
# 主流程
# ============================================================

def main():
    print("=" * 55)
    print("  BOSS直聘 自动打招呼 (LLM + CloakBrowser)")
    print(f"  单次上限: {MAX_GREET_PER_RUN} | 批次: {BATCH_SIZE}")
    print(f"  间隔: {DELAY_BETWEEN_GREETS[0]//1000}-{DELAY_BETWEEN_GREETS[1]//1000}s")
    print(f"  批次休息: {DELAY_AFTER_BATCH[0]//1000}-{DELAY_AFTER_BATCH[1]//1000}s")
    print("=" * 55)

    # 加载岗位
    jobs = load_accepted_jobs()
    if not jobs:
        print("没有需要打招呼的岗位，请先运行 boss_matcher_llm.py 完成匹配和确认。")
        return

    # 加载简历
    resume_summary = summarize_resume()
    print(f"\n简历摘要:\n{resume_summary[:300]}...\n")

    # 加载历史
    history = load_history()
    greeted_links = find_greeted_links(history)
    greeted_by_status = {}
    for h in history:
        link = h.get("link", "")
        status = h.get("status", "unknown")
        if status in ("greeted", "submitted", "already-contacted"):
            greeted_by_status[link] = status

    if greeted_links:
        print(f"\n已打招呼 {len(greeted_links)} 条，自动跳过:")
        for h in history:
            if h.get("link") in greeted_links:
                s = h.get("status", "?")
                print(f"  [{s}] {h.get('company', '-')} / {h.get('name', '-')}")
                greeted_links.discard(h.get("link"))  # 只打印一次
        print()

    # 重新加载 greeted_links（上面被消耗掉了）
    greeted_links = set(greeted_by_status.keys())

    # 过滤已打过的
    pending = [j for j in jobs if j.get("link", "") not in greeted_links]
    skipped = len(jobs) - len(pending)
    if skipped > 0:
        print(f"本次跳过 {skipped} 个已打招呼的岗位")
    if not pending:
        print("所有 accept 岗位均已打过招呼，无需操作。")
        return

    print(f"待打招呼: {len(pending)} 个岗位\n")

    # 启动浏览器
    browser = launch(headless=False, humanize=True)
    page = browser.new_page()

    try:
        # 登录
        if not login_wait(page):
            print("登录失败，退出。")
            browser.close()
            return

        # 打招呼文件
        greeting_file = DATA_DIR / f"greetings_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"

        # 逐岗位处理
        greeted_count = 0
        for i, job in enumerate(pending):
            if greeted_count >= MAX_GREET_PER_RUN:
                log("STOP", "run", "WARN", f"达到单次上限 {MAX_GREET_PER_RUN}")
                break

            result = process_single_job(page, job, resume_summary, greeting_file)

            # 记录历史
            record = {
                "timestamp": datetime.now().isoformat(),
                "link": job.get("link", ""),
                "name": job.get("name", ""),
                "company": job.get("company", "") or job.get("companyName", ""),
                "status": result.get("status", "unknown"),
            }
            if result.get("reason"):
                record["reason"] = result["reason"]
            history.append(record)

            # 检查是否触发停止信号
            if result.get("stop"):
                log("STOP", "run", "WARN", result.get("status", "unknown"))
                save_history(history)
                break

            if result["status"] in ("greeted", "submitted"):
                greeted_count += 1
                waited = sleep_range(DELAY_BETWEEN_GREETS)
                log("WAIT", f"next-{greeted_count}", "INFO", f"{waited}ms")

                # 批次休息
                if greeted_count % BATCH_SIZE == 0:
                    rested = sleep_range(DELAY_AFTER_BATCH)
                    log("REST", f"batch-{greeted_count}", "INFO", f"{rested}ms")
            else:
                # 跳过/失败也短暂等待
                sleep_ms(rand(2000, 4000))

            # 每 5 个保存一次历史
            if greeted_count % 5 == 0:
                save_history(history)

    except KeyboardInterrupt:
        log("STOP", "user", "INFO", "用户中断")
    except Exception as e:
        log("FATAL", "main", "ERROR", str(e))
        capture_screenshot(page, "fatal-error")
    finally:
        save_history(history)
        save_cookies_to_file(page.context)
        browser.close()

    # 汇总
    print(f"\n{'='*55}")
    print(f"  打招呼完成!")
    print(f"  本次发送: {greeted_count} 个")
    total_greeted = len([h for h in history if h.get("status") in ("greeted", "submitted")])
    total_skipped = len([h for h in history if h.get("status") == "already-contacted"])
    total_failed = len([h for h in history if h.get("status") == "failed"])
    print(f"  累计打招呼: {total_greeted} | 跳过: {total_skipped} | 失败: {total_failed}")
    print(f"  招呼语文件: {greeting_file}")
    print(f"  历史记录: data/greet_history.json")
    print(f"{'='*55}")


if __name__ == "__main__":
    main()


