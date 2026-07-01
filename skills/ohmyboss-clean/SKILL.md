---
name: ohmyboss-clean
description: 清洗BOSS直聘抓取数据，过滤不合适职位。规则可配置：公司名不详、实习/校招、高级管理、成立<2年、微型初创、外包驻场、工时红线。触发词：清洗boss、清洗数据、boss数据清洗、职位清洗。
---

# BOSS直聘数据清洗

根据6条可配置规则过滤不合适的职位，输出保留数据和被过滤数据（含命中原因）。

核心脚本：`.claude/skills/ohmyboss-clean/boss_cleaner.py`

## 前置条件检测

检查是否有可清洗的数据：

```bash
ls -lt data/*_jobs.json 2>/dev/null | head -5
```

- 找到 JSON/CSV 文件 → 继续
- 无文件 → 告诉用户：「没有找到抓取数据，请先说"ohmyboss-scrape"来采集数据。」**不要继续执行**

## 参数获取

| 参数 | 说明 | 默认值 |
|------|------|--------|
| **数据文件** | data/ 下的 JSON/CSV 文件 | 自动检测最新的 `*_jobs.json` |
| **自定义配置** | 规则配置 JSON 文件 | 使用内置默认配置 |

## 清洗规则

| 规则 | 说明 | 检查字段 |
|------|------|----------|
| vague_company | 公司名含"某"/"知名公司"等模糊词 | company |
| exclude_keywords (intern) | 标题含实习/校招/应届/intern | name |
| exclude_keywords (senior) | 标题含总监/架构师/首席/VP/副总裁/P8/高级专家 | name |
| founded_lt_2y | 成立时间 < 2 年 | establishDate |
| reject_micro_early | 0-20人 + (未融资/天使轮) | scale, financing |
| outsourcing | 含外包/驻场关键词 | description, name, company, companyName |
| overtime_redline | 含单休/996/大小周关键词 | description, name, company, companyName |

**空值处理**：字段为空时该规则不触发（避免误杀）。

**多规则命中**：一条记录可被多条规则命中，均记录原因但只计一次过滤。

## 执行流程

### Step 1: 运行清洗脚本

```bash
.venv/bin/python .claude/skills/ohmyboss-clean/boss_cleaner.py data/{{DATA_FILE}}
```

将 `{{DATA_FILE}}` 替换为实际文件名，如 `ai_agent_jobs.json`。

可选参数：
- `-o PATH` — 指定保留数据输出路径（不含后缀）
- `--rejected-out PATH` — 指定被过滤数据输出路径
- `--config CONFIG.json` — 自定义规则配置文件

### Step 2: 自定义配置（可选）

如需调整规则，创建配置 JSON 文件，仅覆盖需修改的字段：

```json
{
  "vague_company": { "enabled": false },
  "exclude_keywords": {
    "groups": [
      { "name": "intern", "keywords": ["实习", "校招", "应届", "intern"], "reason": "实习/校招岗位" },
      { "name": "senior", "keywords": ["总监", "架构师", "首席", "VP", "副总裁", "P8", "高级专家"], "reason": "高级管理岗位" }
    ]
  },
  "founded_lt_2y": { "max_years": 3 },
  "reject_micro_early": { "scale_values": ["0-20人", "20-99人"] },
  "overtime_redline": { "keywords": ["单休", "996", "大小周", "886"] }
}
```

然后运行：

```bash
.venv/bin/python .claude/skills/ohmyboss-clean/boss_cleaner.py data/{{DATA_FILE}} --config config.json
```

### Step 3: 查看清洗报告

脚本自动输出报告，包含：
- 总计/保留/过滤数量
- 各规则命中统计
- 过滤示例（前5条 + 命中原因）

### Step 4: 使用清洗后数据

输出文件：
- `data/*_jobs_cleaned.json` / `.csv` — 保留数据
- `data/*_jobs_rejected.json` / `.csv` — 被过滤数据（含 `_reasons` 和 `_rules` 字段）

清洗后数据可直接用于 `ohmyboss-match` 匹配分析。
