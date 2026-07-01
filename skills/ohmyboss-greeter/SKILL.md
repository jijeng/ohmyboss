---
name: ohmyboss-greeter
description: BOSS直聘自动打招呼。加载已accept的岗位，逐岗位打开详情页→LLM生成招呼语→发送。包含登录态检测、风控识别、历史去重、速率控制。触发词：打招呼、自动打招呼、boss打招呼、批量打招呼、boss_greeter。
---

# BOSS直聘 自动打招呼

使用 CloakBrowser + LLM 自动给 accept 的岗位发送定制招呼语。

核心脚本：`.claude/skills/ohmyboss-greeter/boss_greeter.py`

## 前置条件检测

检查必需文件是否存在：

```bash
ls data/match_decisions.json data/match_result_llm.json resume/resume.md 2>&1
```

- 三个文件都存在 → 继续
- `match_decisions.json` 缺失 → 告诉用户：「请先运行 ohmyboss-match 完成岗位匹配和决策。」
- `match_result_llm.json` 缺失 → 告诉用户：「请先运行 ohmyboss-match 完成岗位匹配。」
- `resume/resume.md` 缺失 → 告诉用户：「请先在 resume/ 目录下放置简历文件 resume.md。」

**历史去重**：检查 `data/greet_history.json`，已打过招呼的岗位（status 为 `greeted`/`submitted`/`already-contacted`）自动跳过。如果所有 accept 岗位均已打过招呼 → 告诉用户：「所有 accept 岗位均已打过招呼，无需操作。如需重打，请删除 data/greet_history.json 中对应记录。」

## 参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| 无 CLI 参数 | 零配置运行，所有配置在脚本内 | - |

**脚本内可调配置**（修改 `.claude/skills/ohmyboss-greeter/boss_greeter.py` 顶部常量）：
- `MAX_GREET_PER_RUN` — 单次上限（默认 20）
- `BATCH_SIZE` — 批次大小（默认 10），每批次后休息 1-3 分钟
- `DELAY_BETWEEN_GREETS` — 每次间隔 5-15 秒
- `COOKIE_FILE` — Cookie 路径（默认 `~/.config/boss_zhipin_cookies.json`）

## 执行流程

### Step 1: 运行打招呼脚本

```bash
.venv/bin/python .claude/skills/ohmyboss-greeter/boss_greeter.py
```

脚本自动完成：
1. 加载 `match_decisions.json` + `match_result_llm.json`，筛选 accept 岗位
2. 按最低月薪降序排列
3. 加载简历摘要 + 历史记录去重
4. 启动 CloakBrowser（可视化浏览器，需人工扫码登录）
5. 逐岗位处理：
   - 打开详情页 → 风控检查（今日上限/安全验证）
   - 检查是否已沟通（「继续沟通」按钮）
   - LLM 生成定制招呼语（结合简历 + JD）
   - 点击「立即沟通」→ 浮窗跳转聊天页
   - 填入招呼语 → 发送
   - 验证发送成功
6. 速率控制：间隔 5-15s，每 10 个休息 1-3min，单次上限 20

### Step 2: 查看结果

脚本结束时自动输出汇总：

```
============================================
  打招呼完成!
  本次发送: N 个
  累计打招呼: X | 跳过: Y | 失败: Z
  招呼语文件: data/greetings_YYYYMMDD_HHMMSS.jsonl
  历史记录: data/greet_history.json
============================================
```

### Step 3: 处理异常

**登录态失效**：脚本自动检测，无 Cookie 时弹出浏览器窗口等待扫码登录。

**风控触发**：
- 「今日沟通已达上限」→ 立即停止，输出已发送数
- 「安全验证/验证码」→ 人工介入，浏览器保持打开

**单个岗位失败**：记录到 `greet_history.json`（status=failed + reason），继续处理下一个。

## 输出文件

| 文件 | 说明 |
|------|------|
| `data/greetings_*.jsonl` | 每次运行的招呼语文案（含 LLM raw response） |
| `data/greet_history.json` | 打招呼历史（link/name/company/status） |
| `data/screenshots/` | 异常截图（风控/失败/验证） |
| `data/chat_dom_*.json` | 聊天页 DOM dump（调试用） |
| `data/network_dump_*.json` | 网络响应 dump（调试用） |
| `~/.config/boss_zhipin_cookies.json` | 持久化 Cookie（自动复用） |

## 故障排查

| 问题 | 排查方向 |
|------|----------|
| 页面跳转超时 | 查看 `chat_dom_*.json` 中的 `body_class` 和 `candidate_selectors`，确认 SPA 是否渲染 |
| 发送按钮不响应 | 查看控制台 `[DEBUG] ... [TOOLBAR]` 和 `[SEND]` 日志，检查按钮 disabled 状态 |
| 未定位正确聊天框 | 查看 `chat_dom_*.json` 中 `sidebar_html`，检查会话列表结构 |
| LLM 招呼语不生成 | 检查 `DEEPSEEK_API_KEY` 环境变量 |
