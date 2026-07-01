"""BOSS直聘 Job-Resume LLM 匹配器

使用 DeepSeek API 对岗位与简历进行语义匹配判断。
三分类：完全匹配 / 部分匹配 / 完全不匹配。

流程：
1. 提取简历中项目经验、个人总结、工作年限、学历
2. 对每个岗位构造 prompt，调用 DeepSeek API 判断匹配度
3. 生成 HTML 确认页面，用户逐个确认
4. 归档：确认的 → ai_agent_jobs_checked.json/csv，
         不匹配的 → ai_agent_jobs_llm_rejected.json

并发控制：每批 CONC 个岗位并发
"""
import json
import csv
import re
import argparse
import os
import html as html_mod
import asyncio
from pathlib import Path
from datetime import datetime
from typing import Any

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import httpx

# ============================================================
# 配置
# ============================================================
CONC = 3  # 并发数
DS_ENDPOINT = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1/chat/completions")
DS_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
DEFAULT_RESUME_PATH = "resume/resume.md"


# ============================================================
# 简历解析 — 提取项目经验 + 个人总结 + 工作年限 + 学历
# ============================================================

PII_PATTERNS = [
    re.compile(r'1[3-9]\d{9}'),           # 手机号
    re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+'),  # 邮箱
    re.compile(r'\d{17}[\dXx]'),          # 身份证
]


def strip_pii(text: str) -> str:
    for pat in PII_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text


def calc_work_years(work_text: str) -> int:
    """从工作经历计算工作年限（到当前年份）"""
    now_year = datetime.now().year
    years = []
    # 匹配 "2020.02 - 2021.03" 或 "2020 - 2021" 或 "2023.10 - 至今"
    for m in re.finditer(r'(\d{4})[\./]?\d{0,2}\s*[-–—]\s*(至今|\d{4})', work_text):
        start = int(m.group(1))
        end_str = m.group(2)
        end = now_year if end_str == "至今" else int(end_str)
        years.append(end - start)
    return sum(years)


def calc_education_level(edu_text: str) -> str:
    """提取最高学历"""
    if "博士" in edu_text:
        return "博士"
    if "研究生" in edu_text or "硕士" in edu_text or "研" in edu_text:
        return "硕士"
    if "本科" in edu_text or "学士" in edu_text:
        return "本科"
    if "大专" in edu_text or "专科" in edu_text:
        return "大专"
    return "未知"


def extract_resume_profile(resume_path: str) -> dict:
    """从 resume.md 提取匹配所需信息（脱敏版）

    Returns:
        {
            "summary": str,              # 个人总结（脱敏）
            "projects": list[str],       # 项目经验列表（脱敏）
            "work_years": int,           # 工作年限
            "education": str,            # 最高学历
            "resume_info": str,          # 拼接后的完整脱敏文本（用于 LLM）
        }
    """
    with open(resume_path, "r", encoding="utf-8") as f:
        content = f.read()

    # 提取个人总结
    summary_match = re.search(
        r'个人总结</h5>\s*<hr\s*/?>\s*(.*?)(?=<h5|$)',
        content, re.DOTALL
    )
    summary = summary_match.group(1).strip() if summary_match else ""

    # 提取项目经验
    projects_match = re.search(
        r'项目经验</h5>\s*<hr\s*/?>\s*(.*?)(?=<h5|$)',
        content, re.DOTALL
    )
    projects_raw = projects_match.group(1).strip() if projects_match else ""

    # 按项目编号分割
    project_blocks = re.split(r'（\d+）', projects_raw)
    project_blocks = [b.strip() for b in project_blocks if b.strip()]

    # 提取教育经历
    edu_match = re.search(
        r'教育经历</h5>\s*<hr\s*/?>\s*(.*?)(?=<h5|$)',
        content, re.DOTALL
    )
    edu_text = edu_match.group(1).strip() if edu_match else ""

    # 提取工作经历
    work_match = re.search(
        r'工作经历</h5>\s*<hr\s*/?>\s*(.*)',
        content, re.DOTALL
    )
    work_text = work_match.group(1).strip() if work_match else ""

    # 计算工作年限和学历
    work_years = calc_work_years(work_text)
    education = calc_education_level(edu_text)

    # 脱敏
    summary = strip_pii(summary)
    project_blocks = [strip_pii(p) for p in project_blocks]

    # 拼接完整信息
    resume_info = f"【个人总结】\n{summary}\n\n【项目经验】\n"
    for i, p in enumerate(project_blocks, 1):
        resume_info += f"\n项目{i}：{p}\n"
    resume_info += f"\n【工作年限】{work_years}年\n【最高学历】{education}"

    return {
        "summary": summary,
        "projects": project_blocks,
        "work_years": work_years,
        "education": education,
        "resume_info": resume_info,
    }


# ============================================================
# 薪酬解析与筛选
# ============================================================

def parse_salary(salary_str: str) -> dict:
    """解析薪酬字符串

    Returns:
        {
            "valid": bool,        # 是否为正式月薪岗位
            "min_k": float|None,  # 最低月薪（K），如 20-35K → 20.0
            "raw": str,           # 原始字符串
        }
    """
    if not salary_str:
        return {"valid": True, "min_k": None, "raw": salary_str or ""}

    s = salary_str.strip()

    # 兼职/实习：元/天、元/时 → 过滤
    if "元/天" in s or "元/时" in s:
        return {"valid": False, "min_k": None, "raw": s}

    # 正式月薪：如 "20-35K", "15-30K·14薪", "80-100K"
    m = re.match(r'(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*K', s, re.IGNORECASE)
    if m:
        min_k = float(m.group(1))
        return {"valid": True, "min_k": min_k, "raw": s}

    # 无法识别格式，保留但标记无最低薪酬
    return {"valid": True, "min_k": None, "raw": s}


def filter_salary(jobs: list[dict]) -> tuple[list[dict], list[dict]]:
    """筛选兼职岗位，为正式岗位添加 _salary_min_k 字段

    Returns:
        (valid_jobs, filtered_out)
    """
    valid = []
    filtered = []
    for job in jobs:
        salary_str = job.get("salary", "")
        parsed = parse_salary(salary_str)
        if not parsed["valid"]:
            job_copy = dict(job)
            job_copy["_filter_reason"] = f"兼职/日薪: {salary_str}"
            filtered.append(job_copy)
        else:
            job_copy = dict(job)
            job_copy["_salary_min_k"] = parsed["min_k"]
            valid.append(job_copy)
    return valid, filtered


# ============================================================
# DeepSeek API 调用
# ============================================================

SYSTEM_PROMPT = """你是资深求职助手。请完全依据下面提供的【求职者简历信息】，判断某个岗位与求职者的匹配程度。

【判断标准·适中】——不严不松：
- 完全匹配：岗位方向与简历技能/经历相关，经验年限/学历/级别"够得着"
- 部分匹配：岗位与简历技能、技能与经历、经验年限/学历，存在【某一项】不匹配
- 完全不匹配：岗位和简历经历无关，经验/学历/硬技能明显超出，级别明显高于

【输出】只输出一个JSON对象，不要markdown代码块：
{"match": "完全匹配"或"部分匹配"或"完全不匹配", "reason": "一句话理由"}"""


def build_user_prompt(resume_info: str, job: dict) -> str:
    """构造 user prompt"""
    name = job.get("name", "")
    tags = job.get("tags", [])
    tags_str = "、".join(tags) if isinstance(tags, list) else str(tags)
    company = job.get("company", "") or job.get("companyName", "")
    desc = job.get("description", "")

    prompt = f"""【求职者简历信息】
{resume_info}

【待判断岗位】
岗位名：{name}
技能标签：{tags_str}
公司：{company}
岗位描述：
{desc}

请严格按标准判断匹配程度，只输出JSON。"""
    return prompt


async def call_ds(client: httpx.AsyncClient, api_key: str,
                  resume_info: str, job: dict,
                  semaphore: asyncio.Semaphore) -> dict:
    """调用 DeepSeek API 判断单个岗位匹配度"""
    async with semaphore:
        user_prompt = build_user_prompt(resume_info, job)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
        payload = {
            "model": DS_MODEL,
            "messages": messages,
            "max_tokens": 200,
            "temperature": 0.5,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        try:
            resp = await client.post(DS_ENDPOINT, json=payload, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            content = (data.get("choices", [{}])[0]
                       .get("message", {})
                       .get("content", ""))
            return parse_llm_result(content, job)
        except Exception as e:
            print(f"  ⚠ API 调用失败 [{job.get('name', '?')}]: {e}")
            # 失败标记为部分匹配
            return {
                "_match_level": "partial",
                "_match_reason": f"API调用失败: {e}",
            }


def parse_llm_result(content: str, job: dict) -> dict:
    """解析 LLM 返回的 JSON"""
    result = {
        "_match_level": "partial",
        "_match_reason": "",
    }
    # 尝试提取 JSON
    try:
        # 去掉可能的 markdown 代码块
        content = re.sub(r'^```(?:json)?\s*', '', content.strip())
        content = re.sub(r'\s*```$', '', content.strip())
        parsed = json.loads(content)
    except json.JSONDecodeError:
        # 尝试提取花括号内容
        m = re.search(r'\{[\s\S]*\}', content)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except json.JSONDecodeError:
                return result
        else:
            return result

    match_val = parsed.get("match", "部分匹配")
    reason = parsed.get("reason", "")

    # 标准化匹配等级
    if "完全" in match_val and "不" not in match_val:
        level = "full"
    elif "完全不" in match_val:
        level = "none"
    else:
        level = "partial"

    result["_match_level"] = level
    result["_match_reason"] = reason
    return result


# ============================================================
# 批量匹配
# ============================================================

async def match_jobs_llm(jobs: list[dict], resume_profile: dict,
                         api_key: str) -> dict:
    """批量 LLM 匹配

    Returns:
        {
            "full": list[dict],
            "partial": list[dict],
            "none": list[dict],
            "stats": dict,
        }
    """
    resume_info = resume_profile["resume_info"]
    semaphore = asyncio.Semaphore(CONC)

    full_matches = []
    partial_matches = []
    none_matches = []

    total = len(jobs)
    print(f"\n开始 LLM 匹配，共 {total} 个岗位，并发数 {CONC}")

    async with httpx.AsyncClient() as client:
        # 分批处理，显示进度
        tasks = []
        for i, job in enumerate(jobs):
            tasks.append((i, job, call_ds(client, api_key, resume_info, job, semaphore)))

        done = 0
        for batch_start in range(0, len(tasks), CONC):
            batch = tasks[batch_start:batch_start + CONC]
            results = await asyncio.gather(*[t[2] for t in batch])
            for (i, job, _), result in zip(batch, results):
                job_copy = dict(job)
                job_copy["_match_level"] = result["_match_level"]
                job_copy["_match_reason"] = result.get("_match_reason", "")
                done += 1

                level = result["_match_level"]
                if level == "full":
                    full_matches.append(job_copy)
                elif level == "partial":
                    partial_matches.append(job_copy)
                else:
                    none_matches.append(job_copy)

                level_label = {"full": "完全匹配", "partial": "部分匹配", "none": "完全不匹配"}
                salary_min = job_copy.get("_salary_min_k")
                salary_tag = f" · {salary_min}K" if salary_min else ""
                print(f"  [{done}/{total}] {job_copy.get('name', '?')} → {level_label[level]}{salary_tag}")

    stats = {
        "total": total,
        "full": len(full_matches),
        "partial": len(partial_matches),
        "none": len(none_matches),
        "work_years": resume_profile["work_years"],
        "education": resume_profile["education"],
    }

    return {
        "full": full_matches,
        "partial": partial_matches,
        "none": none_matches,
        "stats": stats,
    }


# ============================================================
# HTML 报告 — 用户逐个确认
# ============================================================

def generate_review_html(full_matches: list[dict], partial_matches: list[dict],
                         none_matches: list[dict], stats: dict) -> str:
    """生成用户确认页面（三 Tab + 分页）"""

    def job_card(job: dict, index: int) -> str:
        name = html_mod.escape(job.get("name", ""))
        company = html_mod.escape(job.get("company", "") or job.get("companyName", ""))
        salary = html_mod.escape(job.get("salary", ""))
        location = html_mod.escape(job.get("location", ""))
        experience = html_mod.escape(job.get("experience", ""))
        education = html_mod.escape(job.get("education", ""))
        financing = html_mod.escape(job.get("financing", ""))
        scale = html_mod.escape(job.get("scale", ""))
        industry = html_mod.escape(job.get("industry", ""))
        desc = html_mod.escape(job.get("description", ""))
        match_level = job.get("_match_level", "partial")
        match_reason = html_mod.escape(job.get("_match_reason", ""))
        level_label = {"full": "完全匹配", "partial": "部分匹配", "none": "完全不匹配"}.get(match_level, "部分匹配")
        salary_min_k = job.get("_salary_min_k")
        salary_display = f'<span class="salary-min-k">💰{salary_min_k:.0f}K起</span>' if salary_min_k is not None else ''
        salary_data = f'{salary_min_k:.0f}' if salary_min_k is not None else ''

        return f'''
        <div class="job-card" data-index="{index}" data-level="{match_level}" data-salary-min="{salary_data}">
          <div class="card-header">
            <div class="card-title-row">
              <span class="level-badge level-{match_level}">{level_label}</span>
              <h3>{name}</h3>
              <span class="card-company">{company}</span>
              {salary_display}
            </div>
            <div class="card-meta">
              {salary} · {location} · {experience} · {education}
              <br>{financing} · {scale} · {industry}
            </div>
            <div class="card-reason">
              <span class="reason-label">LLM 判断：</span>{match_reason}
            </div>
          </div>
          <details class="card-details">
            <summary>岗位详情</summary>
            <pre class="jd-text">{desc}</pre>
          </details>
          <div class="card-actions">
            <button class="btn btn-accept" onclick="decide({index}, 'accept')">确认</button>
            <button class="btn btn-reject" onclick="decide({index}, 'reject')">拒绝</button>
          </div>
        </div>
        '''

    def tab_content(tab_id: str, jobs: list[dict], global_offset: int) -> str:
        """生成单个 Tab 的卡片 HTML"""
        cards = ""
        for i, job in enumerate(jobs):
            cards += job_card(job, global_offset + i)
        if not jobs:
            cards = '<div class="empty-hint">暂无岗位</div>'
        return f'<div class="tab-pane" id="tab-{tab_id}">{cards}</div>'

    full_offset = 0
    partial_offset = len(full_matches)
    none_offset = len(full_matches) + len(partial_matches)

    resume_summary = f"工作年限: {stats.get('work_years', '?')}年 | 最高学历: {stats.get('education', '?')}"
    reviewable = stats['full'] + stats['partial']

    # 计算薪资范围（用于滑块过滤器）
    all_jobs = full_matches + partial_matches + none_matches
    salary_vals = [j.get("_salary_min_k") for j in all_jobs if j.get("_salary_min_k") is not None]
    salary_min = int(min(salary_vals)) if salary_vals else 0
    salary_max = int(max(salary_vals)) if salary_vals else 100
    if salary_min == salary_max:
        salary_max = salary_min + 1  # 避免滑块范围为零

    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Job-Resume LLM 匹配确认</title>
<style>
  :root {{
    --bg: #FAF9F6;
    --text: #1a1a1a;
    --muted: #6b7280;
    --accent: #9A3412;
    --green: #15803d;
    --red: #b91c1c;
    --yellow: #a16207;
    --card-bg: #fff;
    --border: #e5e5e5;
  }}
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{
    font-family: -apple-system, "Noto Sans SC", sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.6;
  }}
  .container {{ max-width: 860px; margin: 0 auto; padding: 24px 16px 80px; }}
  h1 {{
    font-family: "Noto Serif SC", serif;
    font-size: 1.75rem;
    margin-bottom: 8px;
    color: var(--accent);
  }}
  .stats-bar {{
    display: flex; gap: 24px; margin-bottom: 8px;
    font-size: 0.9rem; color: var(--muted);
  }}
  .stats-bar strong {{ color: var(--text); }}
  .resume-summary {{
    font-size: 0.85rem; color: var(--muted); margin-bottom: 20px;
    padding: 8px 12px; background: #fff; border: 1px solid var(--border);
    border-radius: 6px;
  }}

  /* Tabs */
  .tabs {{
    display: flex; gap: 0; border-bottom: 2px solid var(--border);
    margin-bottom: 16px;
  }}
  .tab-btn {{
    padding: 10px 22px; font-size: 0.95rem; font-weight: 600;
    border: none; background: transparent; cursor: pointer;
    color: var(--muted); border-bottom: 3px solid transparent;
    margin-bottom: -2px; transition: all 0.15s;
  }}
  .tab-btn:hover {{ color: var(--text); }}
  .tab-btn.active {{ color: var(--accent); border-bottom-color: var(--accent); }}
  .tab-btn .tab-count {{
    font-size: 0.78rem; font-weight: 400; margin-left: 4px;
    background: #e5e5e5; padding: 1px 7px; border-radius: 10px;
  }}
  .tab-btn.active .tab-count {{ background: #fde8d8; color: var(--accent); }}

  .tab-pane {{ display: none; }}
  .tab-pane.active {{ display: block; }}

  /* Cards */
  .job-card {{
    background: var(--card-bg); border: 1px solid var(--border);
    border-radius: 8px; padding: 16px 20px; margin-bottom: 14px;
  }}
  .card-title-row {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
  .level-badge {{
    font-size: 0.72rem; font-weight: 700; padding: 2px 8px;
    border-radius: 4px; white-space: nowrap;
  }}
  .level-full {{ background: #dcfce7; color: var(--green); }}
  .level-partial {{ background: #fef3c7; color: var(--yellow); }}
  .level-none {{ background: #fee2e2; color: var(--red); }}
  .card-title-row h3 {{ font-size: 1.05rem; font-weight: 700; }}
  .card-company {{ color: var(--muted); font-size: 0.88rem; }}
  .card-meta {{ font-size: 0.82rem; color: var(--muted); margin: 6px 0 4px; }}
  .card-reason {{
    font-size: 0.82rem; color: var(--accent); margin-top: 4px;
    padding: 6px 10px; background: #fef3c7; border-radius: 4px;
  }}
  .reason-label {{ font-weight: 600; }}
  .card-details {{ margin-top: 8px; }}
  .card-details summary {{ cursor: pointer; font-size: 0.82rem; color: var(--accent); }}
  .jd-text {{
    margin-top: 8px; font-size: 0.8rem; white-space: pre-wrap;
    max-height: 300px; overflow-y: auto; background: #f9f9f9;
    padding: 10px; border-radius: 4px;
  }}
  .card-actions {{
    margin-top: 12px; display: flex; gap: 10px;
  }}
  .btn {{
    padding: 6px 20px; border-radius: 5px; border: none;
    font-size: 0.85rem; font-weight: 600; cursor: pointer;
    transition: opacity 0.15s;
  }}
  .btn:hover {{ opacity: 0.85; }}
  .btn-accept {{ background: var(--green); color: #fff; }}
  .btn-reject {{ background: var(--red); color: #fff; }}
  .card-decided {{ opacity: 0.5; }}
  .card-decided .card-actions {{ display: none; }}
  .decided-stamp {{
    font-size: 0.82rem; font-weight: 700; margin-top: 8px;
  }}
  .stamp-accept {{ color: var(--green); }}
  .stamp-reject {{ color: var(--red); }}

  /* Pagination */
  .pagination {{
    display: flex; gap: 6px; justify-content: center;
    margin: 20px 0; flex-wrap: wrap; align-items: center;
  }}
  .page-btn {{
    padding: 6px 14px; border: 1px solid var(--border); border-radius: 4px;
    background: #fff; cursor: pointer; font-size: 0.85rem; color: var(--text);
    transition: all 0.15s;
  }}
  .page-btn:hover {{ border-color: var(--accent); color: var(--accent); }}
  .page-btn.active {{ background: var(--accent); color: #fff; border-color: var(--accent); }}
  .page-btn:disabled {{ opacity: 0.4; cursor: default; }}
  .page-info {{ font-size: 0.82rem; color: var(--muted); margin: 0 8px; }}

  .empty-hint {{
    text-align: center; padding: 40px 0; color: var(--muted); font-size: 0.9rem;
  }}

  /* Toolbar */
  .toolbar {{
    position: fixed; bottom: 0; left: 0; right: 0;
    background: var(--bg); border-top: 1px solid var(--border);
    padding: 10px 16px; display: flex; gap: 12px; flex-wrap: wrap;
    align-items: center; justify-content: center; z-index: 100;
    box-shadow: 0 -2px 8px rgba(0,0,0,0.06);
  }}
  .toolbar .btn {{ padding: 8px 24px; font-size: 0.9rem; }}
  .btn-outline {{
    background: transparent; border: 1.5px solid var(--border);
    color: var(--text);
  }}
  .btn-primary {{ background: var(--accent); color: #fff; border: none; }}
  .counter {{ font-size: 0.85rem; color: var(--muted); margin-left: auto; }}

  /* Salary filter */
  .salary-filter {{
    display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
    padding: 10px 14px; background: #fff; border: 1px solid var(--border);
    border-radius: 6px; margin-bottom: 16px; font-size: 0.85rem;
  }}
  .salary-filter label {{ color: var(--muted); white-space: nowrap; }}
  .salary-filter input[type="range"] {{ flex: 1; min-width: 120px; accent-color: var(--accent); }}
  .salary-filter .salary-val {{
    font-weight: 700; color: var(--accent); min-width: 50px; text-align: center;
  }}
  .salary-filter .salary-hidden-count {{
    color: var(--muted); font-size: 0.78rem; white-space: nowrap;
  }}
  .salary-min-k {{
    font-size: 0.78rem; font-weight: 700; color: #9A3412;
    background: #fde8d8; padding: 1px 7px; border-radius: 10px;
    white-space: nowrap;
  }}
  .job-card.salary-hidden {{ display: none !important; }}
</style>
</head>
<body>
<div class="container">
  <h1>Job-Resume LLM 匹配确认</h1>
  <div class="stats-bar">
    <span>共 <strong>{stats['total']}</strong> 岗位</span>
    <span>完全匹配 <strong>{stats['full']}</strong></span>
    <span>部分匹配 <strong>{stats['partial']}</strong></span>
    <span>完全不匹配 <strong>{stats['none']}</strong></span>
    {'<span>兼职/日薪过滤 <strong>' + str(stats['salary_filtered']) + '</strong></span>' if stats.get('salary_filtered') else ''}
  </div>
  <div class="resume-summary">{resume_summary}</div>

  <div class="salary-filter">
    <label>💰 最低月薪 ≥</label>
    <input type="range" id="salary-slider" min="{salary_min}" max="{salary_max}" value="{salary_min}" step="1" oninput="updateSalaryFilter()">
    <span class="salary-val" id="salary-val">{salary_min}K</span>
    <span class="salary-hidden-count" id="salary-hidden-count"></span>
  </div>

  <div class="tabs">
    <button class="tab-btn active" onclick="switchTab('full')">完全匹配<span class="tab-count">{stats['full']}</span></button>
    <button class="tab-btn" onclick="switchTab('partial')">部分匹配<span class="tab-count">{stats['partial']}</span></button>
    <button class="tab-btn" onclick="switchTab('none')">完全不匹配<span class="tab-count">{stats['none']}</span></button>
  </div>

  {tab_content('full', full_matches, full_offset)}
  {tab_content('partial', partial_matches, partial_offset)}
  {tab_content('none', none_matches, none_offset)}

  <div class="pagination" id="pagination"></div>
</div>

<div class="toolbar">
  <button class="btn btn-accept" onclick="batchAccept()">本页确认</button>
  <button class="btn btn-reject" onclick="batchReject()">本页拒绝</button>
  <button class="btn btn-outline" onclick="resetAll()">重置</button>
  <button class="btn btn-primary" onclick="exportResult()">导出结果</button>
  <span class="counter" id="counter">已确认: 0 / {reviewable}</span>
</div>

<script>
const PAGE_SIZE = 10;
const reviewable = {reviewable};
const decisions = {{}};
let currentTab = 'full';
const pageState = {{ full: 1, partial: 1, none: 1 }};

const tabCards = {{
  full: document.querySelectorAll('#tab-full .job-card'),
  partial: document.querySelectorAll('#tab-partial .job-card'),
  none: document.querySelectorAll('#tab-none .job-card'),
}};

function switchTab(tab) {{
  currentTab = tab;
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  event.target.closest('.tab-btn').classList.add('active');
  document.getElementById('tab-' + tab).classList.add('active');
  renderPage();
}}

function getVisible(cards) {{
  // 返回未被 salary 过滤隐藏的卡片
  return Array.from(cards).filter(c => !c.classList.contains('salary-hidden'));
}}

function renderPage() {{
  const allCards = tabCards[currentTab];
  const visible = getVisible(allCards);
  const page = pageState[currentTab];
  const total = visible.length;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  // Clamp page
  if (page > totalPages) pageState[currentTab] = totalPages;
  if (pageState[currentTab] < 1) pageState[currentTab] = 1;
  const p = pageState[currentTab];

  // Show/hide cards: salary-hidden stay hidden, rest paginated
  allCards.forEach(card => {{
    card.style.display = 'none';
  }});
  visible.forEach((card, i) => {{
    if (i >= (p - 1) * PAGE_SIZE && i < p * PAGE_SIZE) {{
      card.style.display = '';
    }}
  }});

  // Render pagination
  const pg = document.getElementById('pagination');
  if (totalPages <= 1) {{ pg.innerHTML = ''; return; }}

  let html = '';
  html += `<button class="page-btn" onclick="goPage(${{p-1}})" ${{p <= 1 ? 'disabled' : ''}}>上一页</button>`;
  // Page buttons: show first, last, current ± 1
  const pages = new Set([1, totalPages]);
  for (let i = p - 1; i <= p + 1; i++) if (i >= 1 && i <= totalPages) pages.add(i);
  const sorted = [...pages].sort((a, b) => a - b);
  let prev = 0;
  for (const pn of sorted) {{
    if (pn - prev > 1) html += '<span class="page-info">…</span>';
    html += `<button class="page-btn ${{pn === p ? 'active' : ''}}" onclick="goPage(${{pn}})">${{pn}}</button>`;
    prev = pn;
  }}
  html += `<span class="page-info">${{p}}/${{totalPages}}页 (${{total}}条)</span>`;
  html += `<button class="page-btn" onclick="goPage(${{p+1}})" ${{p >= totalPages ? 'disabled' : ''}}>下一页</button>`;
  pg.innerHTML = html;
}}

function goPage(n) {{
  const visible = getVisible(tabCards[currentTab]);
  const total = visible.length;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  if (n < 1 || n > totalPages) return;
  pageState[currentTab] = n;
  renderPage();
  window.scrollTo({{ top: 0, behavior: 'smooth' }});
}}

function decide(index, action) {{
  decisions[index] = action;
  const card = document.querySelector(`.job-card[data-index="${{index}}"]`);
  card.classList.add('card-decided');
  const stamp = document.createElement('div');
  stamp.className = 'decided-stamp stamp-' + action;
  stamp.textContent = action === 'accept' ? '已确认' : '已拒绝';
  card.querySelector('.card-actions').after(stamp);
  updateCounter();
}}

function updateCounter() {{
  const n = Object.keys(decisions).length;
  document.getElementById('counter').textContent = `已确认: ${{n}} / ${{reviewable}}`;
}}

function getPageCards() {{
  // 返回当前 tab 当前页的可见卡片（未被 salary 过滤，未决定）
  const visible = getVisible(tabCards[currentTab]);
  const p = pageState[currentTab];
  const start = (p - 1) * PAGE_SIZE;
  const end = start + PAGE_SIZE;
  return visible.slice(start, end).filter(c => !c.classList.contains('card-decided'));
}}

function batchAccept() {{
  getPageCards().forEach(card => {{
    decide(card.dataset.index, 'accept');
  }});
}}

function batchReject() {{
  getPageCards().forEach(card => {{
    decide(card.dataset.index, 'reject');
  }});
}}

function resetAll() {{
  Object.keys(decisions).forEach(k => delete decisions[k]);
  document.querySelectorAll('.job-card').forEach(card => {{
    card.classList.remove('card-decided');
    const stamp = card.querySelector('.decided-stamp');
    if (stamp) stamp.remove();
  }});
  updateCounter();
}}

function exportResult() {{
  const undecided = reviewable - Object.keys(decisions).length;
  if (undecided > 0) {{
    if (!confirm(`还有 ${{undecided}} 个岗位未确认，是否继续导出？未确认的将视为"拒绝"。`)) return;
  }}
  for (let i = 0; i < reviewable; i++) {{
    if (!decisions[i]) decisions[i] = 'reject';
  }}
  const blob = new Blob([JSON.stringify(decisions, null, 2)], {{type: 'application/json'}});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'match_decisions.json';
  a.click();
  URL.revokeObjectURL(url);
  alert('已导出 match_decisions.json，请将其放到项目根目录后运行归档命令。');
}}

function updateSalaryFilter() {{
  const slider = document.getElementById('salary-slider');
  const threshold = parseInt(slider.value);
  document.getElementById('salary-val').textContent = threshold + 'K';

  let hiddenCount = 0;
  document.querySelectorAll('.job-card').forEach(card => {{
    const minK = card.dataset.salaryMin;
    if (minK && parseInt(minK) < threshold) {{
      card.classList.add('salary-hidden');
      hiddenCount++;
    }} else {{
      card.classList.remove('salary-hidden');
    }}
  }});

  const hc = document.getElementById('salary-hidden-count');
  hc.textContent = hiddenCount > 0 ? '（过滤 ' + hiddenCount + ' 条岗位）' : '';

  // 重置分页到第1页
  pageState['full'] = 1;
  pageState['partial'] = 1;
  pageState['none'] = 1;
  renderPage();
}}

// Init
updateSalaryFilter();
</script>
</body>
</html>'''


# ============================================================
# 归档
# ============================================================

def archive_results(jobs_full: list[dict], jobs_partial: list[dict],
                    jobs_none: list[dict], salary_filtered: list[dict],
                    decisions: dict[str, str],
                    output_stem: str) -> dict:
    """根据用户确认结果归档"""
    all_reviewable = jobs_full + jobs_partial
    checked = []
    llm_rejected = list(jobs_none) + list(salary_filtered)

    for i, job in enumerate(all_reviewable):
        decision = decisions.get(str(i), "reject")
        job_copy = {k: v for k, v in job.items() if not k.startswith("_")}
        if decision == "accept":
            job_copy["_user_decision"] = "accept"
            checked.append(job_copy)
        else:
            job_copy["_user_decision"] = "reject"
            job_copy["_match_level"] = job.get("_match_level", "")
            job_copy["_match_reason"] = job.get("_match_reason", "")
            llm_rejected.append(job_copy)

    checked_json, checked_csv = save_data(checked, output_stem + "_checked")
    rejected_json, _ = save_data(llm_rejected, output_stem + "_llm_rejected")

    print(f"\n归档完成:")
    print(f"  用户确认通过: {len(checked)} 条")
    print(f"  LLM拒绝 + 用户拒绝: {len(llm_rejected)} 条")
    print(f"  已确认数据: {checked_json} / {checked_csv}")
    print(f"  拒绝数据: {rejected_json}")

    return {
        "checked_json": str(checked_json),
        "checked_csv": str(checked_csv),
        "rejected_json": str(rejected_json),
        "checked_count": len(checked),
        "rejected_count": len(llm_rejected),
    }


# ============================================================
# I/O
# ============================================================

def load_data(path: str) -> list[dict]:
    p = Path(path)
    if p.suffix == ".json":
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    elif p.suffix == ".csv":
        with open(p, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            return list(reader)
    else:
        raise ValueError(f"不支持的文件格式: {p.suffix}")


def save_data(data: list[dict], path: str):
    p = Path(path)
    out_dir = p.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / f"{p.stem}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    csv_path = out_dir / f"{p.stem}.csv"
    if data:
        fields = list(data[0].keys())
    else:
        fields = []
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(data)

    return json_path, csv_path


def print_match_report(stats: dict):
    print("=" * 55)
    print("  Job-Resume LLM 匹配报告")
    print("=" * 55)
    print(f"  工作年限: {stats.get('work_years', '?')}年 | 最高学历: {stats.get('education', '?')}")
    print(f"  岗位总数: {stats['total']}")
    if stats.get("salary_filtered"):
        print(f"  兼职/日薪过滤: {stats['salary_filtered']} 条")
    print(f"  完全匹配: {stats['full']} 条")
    print(f"  部分匹配: {stats['partial']} 条")
    print(f"  完全不匹配: {stats['none']} 条")
    print("=" * 55)


# ============================================================
# CLI
# ============================================================

def main():
    global CONC

    parser = argparse.ArgumentParser(description="Job-Resume LLM 匹配器 (DeepSeek)")
    parser.add_argument("input", nargs="?", default=None,
                        help="输入文件路径 (JSON/CSV)，默认自动检测 data/ai_agent_jobs_cleaned.json")
    parser.add_argument("--resume", default=DEFAULT_RESUME_PATH, help="简历文件路径")
    parser.add_argument("-o", "--output", default=None, help="输出路径前缀 (不含后缀)")
    parser.add_argument("--api-key", default=None, help="DeepSeek API Key (也可设置 DEEPSEEK_API_KEY 环境变量)")
    parser.add_argument("--conc", type=int, default=CONC, help=f"并发数 (默认 {CONC})")
    parser.add_argument("--archive", default=None, help="归档模式：指定 match_decisions.json 路径")
    parser.add_argument("--review", action="store_true", help="审阅模式：跳过LLM调用，直接从 match_result_llm.json 重新生成HTML")
    args = parser.parse_args()

    CONC = args.conc

    # API Key
    api_key = args.api_key or os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key and not args.archive and not args.review:
        print("错误: 未设置 DeepSeek API Key")
        print("  请设置环境变量: export DEEPSEEK_API_KEY=your-key")
        print("  或使用参数: --api-key your-key")
        return

    # 自动检测输入文件
    input_path = args.input
    if not input_path:
        candidates = [
            "data/ai_agent_jobs_cleaned.json",
            "data/ai_agent_jobs_cleaned.csv",
            "data/ai_agent_jobs.json",
            "data/ai_agent_jobs.csv",
        ]
        for c in candidates:
            if Path(c).exists():
                input_path = c
                break
        if not input_path:
            print("错误: 未找到岗位数据文件，请指定输入路径")
            return

    # 加载数据
    jobs = load_data(input_path)
    print(f"已加载 {len(jobs)} 条岗位数据: {input_path}")

    # 薪酬筛选：过滤兼职/日薪，提取最低月薪
    jobs, salary_filtered = filter_salary(jobs)
    if salary_filtered:
        print(f"薪酬筛选: 过滤 {len(salary_filtered)} 条兼职/日薪岗位")
        for j in salary_filtered[:5]:
            print(f"  - {j.get('name', '?')} ({j.get('_filter_reason', '')})")
        if len(salary_filtered) > 5:
            print(f"  ... 共 {len(salary_filtered)} 条")

    # 提取简历
    profile = extract_resume_profile(args.resume)
    print(f"已提取简历: 个人总结 + {len(profile['projects'])} 个项目")
    print(f"  工作年限: {profile['work_years']}年 | 最高学历: {profile['education']}")

    # 输出路径
    if args.output:
        out_stem = args.output
    else:
        p = Path(input_path)
        # ai_agent_jobs_cleaned → data/ai_agent_jobs_cleaned
        out_stem = str(p.parent / p.stem)

    # 归档模式
    if args.archive:
        # 加载匹配结果
        match_result_path = Path(out_stem).parent / "match_result_llm.json"
        if not match_result_path.exists():
            print(f"错误: 未找到匹配结果文件 {match_result_path}")
            print("  请先运行匹配流程生成匹配结果")
            return
        with open(match_result_path, "r", encoding="utf-8") as f:
            match_data = json.load(f)
        with open(args.archive, "r", encoding="utf-8") as f:
            decisions = json.load(f)
        archive_results(
            match_data["full"], match_data["partial"], match_data["none"],
            match_data.get("salary_filtered", []),
            decisions, out_stem
        )
        return

    # 审阅模式：跳过LLM，直接从缓存 JSON 重新生成 HTML
    if args.review:
        match_json_path = Path(out_stem).parent / "match_result_llm.json"
        if not match_json_path.exists():
            print(f"错误: 未找到匹配结果文件 {match_json_path}")
            print("  请先运行匹配流程生成匹配结果")
            return
        with open(match_json_path, "r", encoding="utf-8") as f:
            match_data = json.load(f)
        result = {
            "full": match_data["full"],
            "partial": match_data["partial"],
            "none": match_data["none"],
            "stats": match_data["stats"],
        }
        salary_filtered = match_data.get("salary_filtered", [])
        result["stats"]["salary_filtered"] = len(salary_filtered)
        print_match_report(result["stats"])
        print("(审阅模式：跳过LLM调用，直接使用缓存匹配结果)")

        # 生成 HTML
        reports_dir = Path("reports")
        reports_dir.mkdir(parents=True, exist_ok=True)
        html_path = reports_dir / "match_review_llm.html"
        html_content = generate_review_html(
            result["full"], result["partial"], result["none"], result["stats"]
        )
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_content)

        print(f"\nHTML 确认页面已生成: {html_path}")
        import webbrowser
        webbrowser.open(f"file://{html_path.resolve()}")
        return

    # LLM 匹配
    result = asyncio.run(match_jobs_llm(jobs, profile, api_key))
    result["stats"]["salary_filtered"] = len(salary_filtered)
    print_match_report(result["stats"])

    # 生成 HTML 确认页面 → reports/
    reports_dir = Path("reports")
    reports_dir.mkdir(parents=True, exist_ok=True)
    html_path = reports_dir / "match_review_llm.html"
    html_content = generate_review_html(
        result["full"], result["partial"], result["none"], result["stats"]
    )
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    # 保存匹配结果 JSON（供归档模式使用）
    match_data = {
        "full": result["full"],
        "partial": result["partial"],
        "none": result["none"],
        "salary_filtered": salary_filtered,
        "stats": result["stats"],
    }
    match_json_path = Path(out_stem).parent / "match_result_llm.json"
    with open(match_json_path, "w", encoding="utf-8") as f:
        json.dump(match_data, f, ensure_ascii=False, indent=2)

    print(f"\nHTML 确认页面已生成: {html_path}")
    print(f"匹配结果 JSON: {match_json_path}")
    print(f"\n请在浏览器中打开确认页面，逐个确认后导出 match_decisions.json")
    print(f"然后运行归档命令:")
    print(f"  .venv/bin/python boss_matcher_llm.py --archive match_decisions.json")

    # 尝试打开浏览器
    import webbrowser
    webbrowser.open(f"file://{html_path.resolve()}")


if __name__ == "__main__":
    main()
