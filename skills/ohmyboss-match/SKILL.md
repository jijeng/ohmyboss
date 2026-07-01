---
name: ohmyboss-match
description: 岗位-简历LLM匹配。用DeepSeek API对岗位与简历语义匹配，分完全匹配/部分匹配/完全不匹配，生成HTML确认页面，归档确认与拒绝数据。触发词：匹配岗位、岗位匹配、简历匹配、job match、resume match。
---

# Job-Resume LLM 匹配

用 DeepSeek API 对清洗后的岗位数据与简历进行语义匹配判断，输出三分类结果并生成用户确认页面。

核心脚本：`.claude/skills/ohmyboss-match/boss_matcher_llm.py`

## 前置条件检测

1. **简历文件**：检查 `resume/resume.md` 是否存在
2. **岗位数据**：检查 `data/` 下是否有清洗后的岗位文件

```bash
ls resume/resume.md 2>/dev/null && echo "简历OK" || echo "缺简历"
ls data/ai_agent_jobs_cleaned.json 2>/dev/null && echo "岗位数据OK" || echo "缺岗位数据"
```

- 简历缺失 → 告诉用户：「请先将简历放到 resume/resume.md」**不要继续执行**
- 岗位数据缺失 → 告诉用户：「没有找到清洗后的岗位数据，请先说"ohmyboss-clean"。」**不要继续执行**

3. **DeepSeek API 配置**（支持 `.env` 文件或环境变量）：

```bash
cat .env 2>/dev/null | grep DEEPSEEK
```

需配置三项：
- `DEEPSEEK_API_KEY` — API Key（必填）
- `DEEPSEEK_BASE_URL` — API 端点（默认 `https://api.deepseek.com/v1/chat/completions`）
- `DEEPSEEK_MODEL` — 模型名（默认 `deepseek-chat`）

无 Key → 告诉用户：「请在 `.env` 中配置 `DEEPSEEK_API_KEY=sk-xxx`，或设置环境变量 `export DEEPSEEK_API_KEY=your-key`」**不要继续执行**

## 参数获取

| 参数 | 说明 | 默认值 |
|------|------|--------|
| **数据文件** | data/ 下的 JSON/CSV 文件 | 自动检测 `data/ai_agent_jobs_cleaned.json` |
| **简历文件** | resume/resume.md | `resume/resume.md` |
| **并发数** | API 并发调用数 | 3 |
| **API Key** | DeepSeek API Key | `.env` 或环境变量 `DEEPSEEK_API_KEY` |
| **API 端点** | DeepSeek API URL | `.env` 或环境变量 `DEEPSEEK_BASE_URL`，默认 `https://api.deepseek.com/v1/chat/completions` |
| **模型** | LLM 模型名 | `.env` 或环境变量 `DEEPSEEK_MODEL`，默认 `deepseek-chat` |

## 匹配逻辑

### 简历提取

从 `resume/resume.md` 提取：
- **个人总结**：脱敏后的个人总结文本
- **项目经验**：所有项目经验（脱敏）
- **工作年限**：从工作经历计算总年限
- **最高学历**：从教育经历提取

自动去除姓名、联系方式等 PII 信息。

### LLM 匹配标准（适中）

| 匹配等级 | 判断标准 |
|----------|----------|
| 完全匹配 | 岗位方向与简历技能/经历相关，经验年限/学历/级别"够得着" |
| 部分匹配 | 岗位与简历技能、技能与经历、经验年限/学历，存在某一项不匹配 |
| 完全不匹配 | 岗位和简历经历无关，经验/学历/硬技能明显超出，级别明显高于 |

### API 调用

- 端点/模型：从环境变量 `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` 读取，默认 `deepseek-chat`
- `max_tokens=200`，`temperature=0.5`
- 并发控制：每批 3 个岗位并发（可通过 `--conc` 调整）
- API 失败时默认标记为"部分匹配"
- 支持 `.env` 文件配置，`python-dotenv` 自动加载

## 执行流程

### Step 1: 运行 LLM 匹配

```bash
.venv/bin/python .claude/skills/ohmyboss-match/boss_matcher_llm.py
```

自动检测 `data/ai_agent_jobs_cleaned.json`，提取简历，调用 DeepSeek API 逐批匹配。

可选参数：
- `data/xxx.json` — 指定输入文件
- `--resume PATH` — 指定简历路径
- `-o PATH` — 指定输出路径前缀（不含后缀）
- `--api-key KEY` — 直接传入 API Key
- `--conc N` — 调整并发数
- `--review` — 审阅模式，跳过 LLM 调用，直接从 `match_result_llm.json` 重新生成 HTML（测试用，秒级）

### Step 2: 在浏览器中确认

脚本自动打开 HTML 确认页面，展示：
- 完全匹配 / 部分匹配 / 完全不匹配 三个 Tab
- 每个岗位卡片含 LLM 判断理由、💰最低月薪徽章
- **薪资过滤滑块**：拖动滑块隐藏月薪不达标的岗位
- 分页（每页 10 条），页面底部「本页确认」「本页拒绝」仅作用于当前页

每个岗位可「确认」或「拒绝」，底部工具栏还有重置和导出按钮。

### Step 3: 导出确认结果

在 HTML 页面中点击「导出结果」→ 下载 `match_decisions.json`，放到项目根目录。

### Step 4: 归档

```bash
.venv/bin/python .claude/skills/ohmyboss-match/boss_matcher_llm.py --archive match_decisions.json
```

归档输出：
- `data/ai_agent_jobs_checked.json` / `.csv` — 用户确认通过的岗位
- `data/ai_agent_jobs_llm_rejected.json` — LLM 判定完全不匹配 + 用户拒绝的岗位

## 输出文件

| 文件 | 说明 |
|------|------|
| `reports/match_review_llm.html` | 确认页面（浏览器打开） |
| `data/match_result_llm.json` | 匹配结果中间文件 |
| `data/*_checked.json` / `.csv` | 用户确认通过的岗位 |
| `data/*_llm_rejected.json` | 拒绝的岗位（含原因） |
