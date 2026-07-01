---
name: ohmyboss-setup
description: 配置BOSS直聘抓取环境。安装CloakBrowser反检测浏览器、Playwright和Python依赖，验证反检测生效。触发词：配置boss抓取、安装cloakbrowser、boss环境配置、ohmyboss-setup。
---

# BOSS直聘抓取环境配置

一键配置 CloakBrowser + Playwright 环境，用于后续抓取 BOSS直聘职位数据。

## Step 0: 环境检测（每次必先执行）

先检查是否已配置过，避免重复安装。运行以下命令：

```bash
source .venv/bin/activate 2>/dev/null && python -c "
from cloakbrowser import launch
browser = launch(headless=True)
page = browser.new_page()
wd = page.evaluate('navigator.webdriver')
ua = page.evaluate('navigator.userAgent')
browser.close()
print(f'STATUS:OK')
print(f'webdriver: {wd}')
print(f'userAgent: {ua}')
" 2>&1
```

**判断逻辑：**

- 输出包含 `STATUS:OK` 且 `webdriver: False` → **环境已就绪，跳过所有后续步骤**，直接告诉用户：「环境已配置，CloakBrowser 反检测正常，可以直接使用 ohmyboss-scrape 抓取数据。」
- 输出包含 `STATUS:OK` 但 `webdriver: True` → 环境存在但反检测异常，提示用户重新安装 CloakBrowser
- 任何报错（`ModuleNotFoundError`、`No such file`、`activate: No such file` 等）→ 环境未配置，继续 Step 1

## Step 1: 检查前置条件

```bash
python3 --version && node --version
```

- Python >= 3.9，Node.js >= 18
- 不满足则提示用户先安装，不继续

## Step 2: 创建虚拟环境

```bash
python3 -m venv .venv
```

## Step 3: 安装 CloakBrowser

```bash
source .venv/bin/activate && pip install "cloakbrowser[geoip]"
```

验证导入：

```bash
source .venv/bin/activate && python -c "from cloakbrowser import launch; print('CloakBrowser OK')"
```

## Step 4: 安装 requirements.txt

```bash
.venv/bin/pip install -r requirements.txt
```

验证关键依赖：

```bash
source .venv/bin/activate && python -c "import httpx; import dotenv; import jieba; print('Dependencies OK')"
```

## Step 5: 安装 Playwright 浏览器

```bash
source .venv/bin/activate && python -m playwright install chromium
```

## Step 6: 验证反检测

```bash
source .venv/bin/activate && python -c "
from cloakbrowser import launch
browser = launch(headless=True)
page = browser.new_page()
wd = page.evaluate('navigator.webdriver')
ua = page.evaluate('navigator.userAgent')
print(f'webdriver: {wd}')
print(f'userAgent: {ua}')
browser.close()
print('验证通过!' if not wd else '验证失败: webdriver未被隐藏')
"
```

首次运行会自动下载约 200MB 的 CloakBrowser 定制 Chromium 二进制。

## 完成

向用户报告：
- Python 虚拟环境路径
- CloakBrowser 版本
- webdriver 检测结果
- 下一步提示：使用 `ohmyboss-scrape` 开始抓取
