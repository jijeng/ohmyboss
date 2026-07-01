"""BOSS直聘职位抓取 (CloakBrowser)"""
import os
import json, csv, time, random, re
from pathlib import Path
from cloakbrowser import launch
COOKIE_FILE = Path.home() / ".config" / "boss_zhipin_cookies.json"

# ===== 用户参数 =====
KEYWORDS = {{KEYWORDS_LIST}}  # 如 ["Java后端", "Java开发", "Java工程师"]
CITIES = {{CITIES_DICT}}      # 如 {"北京": 101010100}
PAGES_PER_SEARCH = 5          # 每个关键词最多滚动轮数（每轮约15条）
# ====================

OUTPUT_DIR = Path("data")
OUTPUT_DIR.mkdir(exist_ok=True)

JS_LIST = """
() => {
    const cards = document.querySelectorAll('li.job-card-box');
    return Array.from(cards).map(card => {
        const name = card.querySelector('.job-name');
        const tags = card.querySelectorAll('.tag-list li');
        const company = card.querySelector('.boss-name');
        const location = card.querySelector('.company-location');
        const tagTexts = Array.from(tags).map(t => t.textContent.trim());
        let experience = '', education = '';
        for (const t of tagTexts) {
            if (t.includes('年') || t === '应届生' || t === '在校生' || t.includes('经验'))
                experience = t;
            else if (['本科','硕士','博士','大专','学历不限','中专/中技','高中'].some(k => t.includes(k)))
                education = t;
        }
        return {
            name: name ? name.textContent.trim() : '',
            link: name ? name.getAttribute('href') : '',
            experience, education,
            company: company ? company.textContent.trim() : '',
            location: location ? location.textContent.trim() : '',
        };
    }).filter(j => j.name);
}
"""

JS_DETAIL = """
() => {
    const r = {};

    // 1. 薪资
    const sal = document.querySelector('.salary, .info-primary .salary');
    r.salary = sal ? sal.textContent.trim() : '';

    // 2. 职位描述
    const desc = document.querySelector('.job-sec-text, .job-detail-section .text, .text.fold-text');
    r.description = desc ? desc.textContent.trim() : '';

    // 3. 公司信息（行业、规模、融资）—— 通过图标直接定位
    const getInfoByIcon = (iconSelector) => {
        const icon = document.querySelector(iconSelector);
        if (!icon) return '';
        const parent = icon.closest('p') || icon.parentElement;
        if (!parent) return '';
        return parent.textContent.trim();
    };

    r.financing = getInfoByIcon('.sider-company .icon-stage');
    r.scale = getInfoByIcon('.sider-company .icon-scale');
    r.industry = getInfoByIcon('.sider-company .icon-industry');

    // 4. 工商信息（限定在 .business-info-box 下，避免命中相似职位区的 .company-name）
    const getBusinessText = (selector) => {
        const el = document.querySelector('.business-info-box ' + selector);
        if (!el) return '';
        const span = el.querySelector('span');
        if (span) {
            return el.textContent.replace(span.textContent, '').trim();
        }
        return el.textContent.trim();
    };

    r.companyName = getBusinessText('.company-name');
    r.legalRepresentative = getBusinessText('.company-user');
    r.establishDate = getBusinessText('.res-time');
    r.companyType = getBusinessText('.company-type');
    r.manageStatus = getBusinessText('.manage-state');
    r.registeredCapital = getBusinessText('.company-fund');

    // 5. 公司介绍 & 工作地址
    const intro = document.querySelector('.job-detail-company .company-info-box .job-sec-text, .job-detail-company .company-info-box .fold-text');
    r.companyIntro = intro ? intro.textContent.trim() : '';
    const addr = document.querySelector('.job-detail-company .location-address');
    r.companyAddress = addr ? addr.textContent.trim() : '';

    return r;
}
"""

def delay(a=3, b=7):
    time.sleep(random.uniform(a, b))

def wait_jobs(page, sec=25):
    for _ in range(sec):
        time.sleep(1)
        try:
            if page.evaluate("document.querySelectorAll('li.job-card-box').length") > 0:
                return True
        except: pass
    return False



def wait_new_cards(page, old_count, timeout=8):
    """等待页面卡片数量超过 old_count，最多等 timeout 秒"""
    for _ in range(timeout * 2):
        time.sleep(0.5)
        try:
            n = page.evaluate("document.querySelectorAll('li.job-card-box').length")
            if n > old_count:
                return n
        except:
            pass
    return old_count

def collect_list(page, keyword, city_name, city_code):
    """通过触底滚动加载抓取列表页，最多滚动 PAGES_PER_SEARCH 轮"""
    url = f"https://www.zhipin.com/web/geek/job?query={keyword}&city={city_code}"
    print(f"  [加载首页] {url}")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        print(f"  [导航错误] {e}")
        return []
    if not wait_jobs(page):
        print(f"  [无结果]")
        return []

    # 先采集首屏已有的卡片
    time.sleep(2)
    jobs = []
    batch = page.evaluate(JS_LIST)
    for j in batch:
        j["keyword"] = keyword
        j["city"] = city_name
    jobs.extend(batch)
    print(f"  [首屏] {len(jobs)} 条")

    # 滚动加载更多
    no_new_count = 0
    for round_i in range(1, PAGES_PER_SEARCH + 1):
        old_count = page.evaluate("document.querySelectorAll('li.job-card-box').length")
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        new_count = wait_new_cards(page, old_count)

        batch = page.evaluate(JS_LIST)
        existing = {j["link"] for j in jobs}
        new = [j for j in batch if j["link"] not in existing]
        for j in new:
            j["keyword"] = keyword
            j["city"] = city_name
        jobs.extend(new)
        print(f"  [第{round_i}轮滚动] 卡片 {old_count}→{new_count}, +{len(new)} 条新 (累计 {len(jobs)})")

        if len(new) == 0:
            no_new_count += 1
            if no_new_count >= 2:
                print(f"  [连续{no_new_count}轮无新数据，停止]")
                break
        else:
            no_new_count = 0
        delay(2, 4)
    return jobs

def collect_details(page, jobs):
    total = len(jobs)
    consecutive_errors = 0
    for i, job in enumerate(jobs, 1):
        link = job.get("link", "")
        if not link:
            continue
        print(f"  [{i}/{total}] {job['name'][:25]}...", end=" ", flush=True)
        url = f"https://www.zhipin.com{link}" if link.startswith("/") else link
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=15000)
            time.sleep(random.uniform(1, 2))
            d = page.evaluate(JS_DETAIL)
            for key, value in d.items():
                job[key] = value
            print(f"{job.get('salary', '')} | {job.get('companyName', '')[:10]}")
            consecutive_errors = 0
            delay(1, 3)
        except Exception as e:
            err = str(e)[:60]
            print(f"错误: {err}")
            consecutive_errors += 1
            if consecutive_errors >= 3 or "DISCONNECTED" in err.upper():
                wait = min(30 + consecutive_errors * 10, 90)
                print(f"  [限流/连续错误] 等待{wait}秒...")
                time.sleep(wait)
                try:
                    page.goto("https://www.zhipin.com/", wait_until="domcontentloaded", timeout=20000)
                    time.sleep(3)
                except:
                    pass
            else:
                delay(2, 4)


def save(jobs, prefix):
    # 保存完整 JSON（包含所有字段）
    with open(OUTPUT_DIR / f"{prefix}_jobs.json", "w", encoding="utf-8") as f:
        json.dump(jobs, f, ensure_ascii=False, indent=2)
    # 定义 CSV 字段（按逻辑分组，方便查看）
    fields = [
        # 基础信息（列表页获取）
        "keyword", "city", "name", "salary", "experience", "education",
        "company", "location", "link",
        # 详情页补充信息
        "description", "industry", "scale", "financing",
        # 工商信息
        "companyName", "legalRepresentative", "establishDate",
        "companyType", "manageStatus", "registeredCapital",
        # 公司介绍与地址
        "companyIntro", "companyAddress"
    ]

    with open(OUTPUT_DIR / f"{prefix}_jobs.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(jobs)


def save_cookies_to_file(context):
    """保存当前上下文的所有 cookies 到文件"""
    try:
        cookies = context.cookies()
        # 确保目录存在
        COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=4)
        print(f"[Cookies] 已保存到: {COOKIE_FILE}")
    except Exception as e:
        print(f"[错误] 保存 cookies 失败: {e}")

def load_cookies_from_file(context):
    """从文件读取 cookies 并注入到上下文"""
    if not COOKIE_FILE.exists():
        print(f"[提示] 未找到本地 cookies 文件")
        return False
    try:
        with open(COOKIE_FILE, "r", encoding="utf-8") as f:
            cookies = json.load(f)
        if not cookies:
            return False
        context.add_cookies(cookies)
        print(f"[Cookies] 已从本地加载 {len(cookies)} 个 cookie")
        return True
    except Exception as e:
        print(f"[错误] 读取 cookies 失败: {e}")
        return False

def is_logged_in(page):
    """判断当前页面是否处于登录状态"""
    try:
        # 访问一个必须登录后才能看到的页面，或检查特定的 Token/元素
        # 这里以个人中心或首页跳转判断
        current_url = page.url
        # 如果 URL 包含 login，或者没有跳转到预期页面，则认为未登录
        if "login" in current_url:
            return False

        # 也可以通过检查 Cookie 中是否有关键字段判断
        cookies = page.context.cookies()
        has_token = any(c["name"] in ("wt2", "token", "bst") for c in cookies)
        return has_token
    except:
        return False

def login_wait(page):
    """
    核心逻辑：
    1. 尝试加载本地 Cookie
    2. 访问页面检查是否自动登录成功
    3. 如果未登录，等待用户手动操作
    4. 登录成功后，立即保存最新 Cookie
    """
    # 1. 尝试从文件加载
    has_local_cookies = load_cookies_from_file(page.context)

    # 2. 访问目标页面（BOSS直聘）
    target_url = "https://www.zhipin.com/web/user/?ka=header-login"
    print(f"[访问] {target_url}")
    page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
    time.sleep(3) # 等待页面跳转/重定向完成

    # 3. 检查是否直接登录成功
    if has_local_cookies and is_logged_in(page):
        print("[登录成功] 已通过本地 Cookies 自动登录")
        save_cookies_to_file(page.context)
        return True

    # 4. 如果未自动登录，则进入手动登录等待循环
    print("[等待登录] 本地 Cookies 无效或不存在，请在浏览器中完成手动登录...")
    for i in range(120):  # 等待时间延长，方便扫码
        time.sleep(3)
        try:
            if is_logged_in(page):
                print("\n[登录成功] 检测到登录状态！")
                # 5. 登录成功后立即保存 Cookie，下次直接使用
                save_cookies_to_file(page.context)
                return True
            print(f"  [{i+1}/120] 等待手动登录中...", end="\r")
        except Exception:
            pass

    print("\n[超时] 用户未在规定时间内完成登录")
    return False


def main():
    print("=" * 50)
    print("  BOSS直聘职位抓取")
    print(f"  关键词: {KEYWORDS}")
    print(f"  城市: {list(CITIES.keys())}")
    print("=" * 50)
    browser = launch(headless=False, humanize=True)
    page = browser.new_page()
    login_wait(page)
    try:
        page.goto("https://www.zhipin.com/", wait_until="domcontentloaded", timeout=60000)
    except Exception:
        time.sleep(5)
    time.sleep(3)
    all_jobs = []
    seen_links = set()
    for kw in KEYWORDS:
        for city, code in CITIES.items():
            print(f"\n--- {kw} | {city} ---")
            jobs = collect_list(page, kw, city, code)
            new_jobs = [j for j in jobs if j["link"] not in seen_links]
            for j in new_jobs:
                seen_links.add(j["link"])
            all_jobs.extend(new_jobs)
            print(f"  [去重后] +{len(new_jobs)} 条新岗位 (总计 {len(all_jobs)})")
            delay(8, 15)
    prefix = KEYWORDS[0].replace(" ","_").lower()
    save(all_jobs, prefix)
    print(f"\n[列表完成] {len(all_jobs)} 条（去重后），开始获取详情...")
    collect_details(page, all_jobs)
    save_cookies_to_file(page.context)
    save(all_jobs, prefix)
    browser.close()
    filled = sum(1 for j in all_jobs if j.get("salary"))
    print(f"\n{'='*50}")
    print(f"  完成! {len(all_jobs)} 个岗位 | 薪资获取 {filled}/{len(all_jobs)}")
    print(f"  CSV: data/{prefix}_jobs.csv")
    print(f"  JSON: data/{prefix}_jobs.json")
    print(f"{'='*50}")

if __name__ == "__main__":
    main()
