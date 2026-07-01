"""BOSS直聘职位数据清洗器

根据可配置规则过滤不合适的职位：
1. vague_company  - 语焉不详公司名称
2. exclude_keywords - 排除实习/校招/应届 或 总监/架构师等高级岗位
3. founded_lt_2y   - 成立不到2年
4. reject_micro_early - 微型早期初创 (0-20人 + 未融资/天使轮)
5. outsourcing     - 外包/驻场
6. overtime_redline - 工时红线 (单休/996/大小周)
"""
import json
import csv
import re
import argparse
from datetime import datetime, date
from pathlib import Path
from typing import Any

# ============================================================
# 清洗配置 — 所有规则均可通过修改此配置调整
# ============================================================
CLEAN_CONFIG = {
    # (1) 语焉不详公司名称
    "vague_company": {
        "enabled": True,
        "pattern": r"某|知名公司|^知名$",
        "fields": ["company"],  # 检查的字段
        "reason": "公司名称语焉不详",
    },

    # (2) 排除关键词 — 分两组，任一组命中即排除
    "exclude_keywords": {
        "enabled": True,
        "groups": [
            {
                "name": "intern",
                "label": "实习/校招",
                "keywords": ["实习", "校招", "应届", "intern"],
                "reason": "实习/校招岗位",
            },
            {
                "name": "senior",
                "label": "高级管理",
                "keywords": ["总监", "架构师", "首席", "VP", "副总裁", "P8", "高级专家"],
                "reason": "高级管理岗位",
            },
        ],
        "fields": ["name"],  # 检查的字段
    },

    # (3) 成立时间 < 2 年
    "founded_lt_2y": {
        "enabled": True,
        "field": "establishDate",
        "max_years": 2,
        "reason": "公司成立不足2年",
    },

    # (4) 微型早期初创: 0-20人 + (未融资 | 天使轮)
    "reject_micro_early": {
        "enabled": True,
        "scale_values": ["0-20人"],
        "financing_values": ["未融资", "天使轮"],
        "reason": "微型早期初创 (0-20人+未融资/天使轮)",
    },

    # (5) 外包/驻场
    "outsourcing": {
        "enabled": True,
        "keywords": ["外包", "驻场"],
        "fields": ["description", "name", "company", "companyName"],
        "reason": "外包/驻场岗位",
    },

    # (6) 工时红线
    "overtime_redline": {
        "enabled": True,
        "keywords": ["单休", "996", "大小周"],
        "fields": ["description", "name", "company", "companyName"],
        "reason": "工时红线 (单休/996/大小周)",
    },
}


def _get_field(record: dict, field: str) -> str:
    """安全取字段值，空值返回空字符串"""
    val = record.get(field)
    if val is None:
        return ""
    return str(val).strip()


def _concat_fields(record: dict, fields: list[str]) -> str:
    """拼接多个字段的文本用于关键词搜索"""
    return " ".join(_get_field(record, f) for f in fields)


# ============================================================
# 各规则检查函数
# ============================================================

def check_vague_company(record: dict, cfg: dict) -> str | None:
    """(1) 公司名称语焉不详"""
    if not cfg.get("enabled", True):
        return None
    pattern = cfg["pattern"]
    for field in cfg["fields"]:
        text = _get_field(record, field)
        if text and re.search(pattern, text):
            return cfg["reason"]
    return None


def check_exclude_keywords(record: dict, cfg: dict) -> str | None:
    """(2) 标题包含排除关键词"""
    if not cfg.get("enabled", True):
        return None
    text = _concat_fields(record, cfg["fields"])
    if not text:
        return None
    for group in cfg["groups"]:
        for kw in group["keywords"]:
            if kw.lower() in text.lower():
                return group["reason"]
    return None


def check_founded_lt_2y(record: dict, cfg: dict) -> str | None:
    """(3) 成立不到 N 年"""
    if not cfg.get("enabled", True):
        return None
    date_str = _get_field(record, cfg["field"])
    if not date_str:
        return None  # 无数据不过滤
    try:
        # 支持格式: "2011-05-06", "2011/05/06", "20110506"
        date_str = date_str.replace("/", "-")
        founded = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        today = date.today()
        years = (today - founded).days / 365.25
        if years < cfg["max_years"]:
            return cfg["reason"]
    except (ValueError, IndexError):
        pass  # 解析失败不过滤
    return None


def check_reject_micro_early(record: dict, cfg: dict) -> str | None:
    """(4) 微型早期初创"""
    if not cfg.get("enabled", True):
        return None
    scale = _get_field(record, "scale")
    financing = _get_field(record, "financing")
    if not scale or not financing:
        return None  # 缺数据不过滤（避免误杀）
    scale_match = scale in cfg["scale_values"]
    financing_match = financing in cfg["financing_values"]
    if scale_match and financing_match:
        return cfg["reason"]
    return None


def check_outsourcing(record: dict, cfg: dict) -> str | None:
    """(5) 外包/驻场"""
    if not cfg.get("enabled", True):
        return None
    text = _concat_fields(record, cfg["fields"])
    if not text:
        return None
    for kw in cfg["keywords"]:
        if kw in text:
            return cfg["reason"]
    return None


def check_overtime_redline(record: dict, cfg: dict) -> str | None:
    """(6) 工时红线"""
    if not cfg.get("enabled", True):
        return None
    text = _concat_fields(record, cfg["fields"])
    if not text:
        return None
    for kw in cfg["keywords"]:
        if kw in text:
            return cfg["reason"]
    return None


# 规则执行顺序
RULE_CHECKS = [
    ("vague_company", check_vague_company),
    ("exclude_keywords", check_exclude_keywords),
    ("founded_lt_2y", check_founded_lt_2y),
    ("reject_micro_early", check_reject_micro_early),
    ("outsourcing", check_outsourcing),
    ("overtime_redline", check_overtime_redline),
]


def clean_record(record: dict, config: dict = None) -> dict:
    """对单条记录执行所有规则检查

    Returns:
        dict with keys:
            record: 原始记录
            rejected: bool
            reasons: list[str]  命中原因列表
            rules: list[str]    命中规则名列表
    """
    cfg = config or CLEAN_CONFIG
    reasons = []
    rules = []

    for rule_name, check_fn in RULE_CHECKS:
        rule_cfg = cfg.get(rule_name, {})
        reason = check_fn(record, rule_cfg)
        if reason:
            reasons.append(reason)
            rules.append(rule_name)

    return {
        "record": record,
        "rejected": len(reasons) > 0,
        "reasons": reasons,
        "rules": rules,
    }


def clean_jobs(jobs: list[dict], config: dict = None) -> dict:
    """批量清洗

    Returns:
        dict with keys:
            kept: list[dict]     保留的职位
            rejected: list[dict] 被过滤的职位 (含 _reason, _rules 字段)
            stats: dict          统计信息
    """
    kept = []
    rejected = []
    stats = {name: 0 for name, _ in RULE_CHECKS}
    stats["total"] = len(jobs)

    for job in jobs:
        result = clean_record(job, config)
        if result["rejected"]:
            job_copy = dict(job)
            job_copy["_reasons"] = result["reasons"]
            job_copy["_rules"] = result["rules"]
            rejected.append(job_copy)
            for rule_name in result["rules"]:
                stats[rule_name] += 1
        else:
            kept.append(job)

    stats["kept"] = len(kept)
    stats["rejected"] = len(rejected)
    return {"kept": kept, "rejected": rejected, "stats": stats}


def load_data(path: str) -> list[dict]:
    """加载 JSON 或 CSV 数据"""
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
    """保存为 JSON 和 CSV"""
    p = Path(path)
    stem = p.stem  # e.g. "ai_agent_jobs_cleaned"
    out_dir = p.parent

    # JSON
    json_path = out_dir / f"{stem}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # CSV
    csv_path = out_dir / f"{stem}.csv"
    if data:
        # 保留原始字段顺序，去掉 _reasons/_rules
        sample = {k: v for k, v in data[0].items() if not k.startswith("_")}
        fields = list(sample.keys())
    else:
        fields = []
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(data)

    return json_path, csv_path


def print_report(stats: dict, rejected: list[dict]):
    """打印清洗报告"""
    print("=" * 55)
    print("  BOSS直聘数据清洗报告")
    print("=" * 55)
    print(f"  总计: {stats['total']} 条")
    print(f"  保留: {stats['kept']} 条")
    print(f"  过滤: {stats['rejected']} 条")
    print("-" * 55)
    print("  规则命中统计:")
    for name, _ in RULE_CHECKS:
        if name in stats:
            label = CLEAN_CONFIG.get(name, {}).get("reason", name)
            if name == "exclude_keywords":
                # 展示子组
                for g in CLEAN_CONFIG["exclude_keywords"]["groups"]:
                    print(f"    {g['label']:12s}  {stats[name]:>3d} 条")
            elif name == "vague_company":
                print(f"    {'公司名不详':12s}  {stats[name]:>3d} 条")
            elif name == "founded_lt_2y":
                print(f"    {'成立<2年':12s}  {stats[name]:>3d} 条")
            elif name == "reject_micro_early":
                print(f"    {'微型初创':12s}  {stats[name]:>3d} 条")
            elif name == "outsourcing":
                print(f"    {'外包/驻场':12s}  {stats[name]:>3d} 条")
            elif name == "overtime_redline":
                print(f"    {'工时红线':12s}  {stats[name]:>3d} 条")
    print("-" * 55)

    # 打印部分被过滤的示例
    if rejected:
        print("  过滤示例 (前5条):")
        for r in rejected[:5]:
            reasons = " + ".join(r.get("_reasons", []))
            title = r.get("name", "")[:35]
            company = r.get("company", "")[:15]
            print(f"    [{reasons}] {title} | {company}")

    print("=" * 55)


def main():
    parser = argparse.ArgumentParser(description="BOSS直聘职位数据清洗")
    parser.add_argument("input", help="输入文件路径 (JSON/CSV)")
    parser.add_argument("-o", "--output", help="输出文件路径 (不含后缀)", default=None)
    parser.add_argument("--rejected-out", help="被过滤数据输出路径", default=None)
    parser.add_argument("--config", help="自定义配置 JSON 文件", default=None)
    args = parser.parse_args()

    # 加载配置
    config = CLEAN_CONFIG
    if args.config:
        with open(args.config, "r", encoding="utf-8") as f:
            user_cfg = json.load(f)
        # 深度合并
        for k, v in user_cfg.items():
            if k in config and isinstance(v, dict):
                config[k].update(v)
            else:
                config[k] = v

    # 加载数据
    jobs = load_data(args.input)
    print(f"已加载 {len(jobs)} 条数据: {args.input}")

    # 执行清洗
    result = clean_jobs(jobs, config)

    # 打印报告
    print_report(result["stats"], result["rejected"])

    # 确定输出路径
    input_path = Path(args.input)
    if args.output:
        out_stem = args.output
    else:
        out_stem = str(input_path.parent / input_path.stem.replace("_jobs", "_jobs_cleaned"))

    # 保留数据
    json_path, csv_path = save_data(result["kept"], out_stem)
    print(f"\n保留数据已保存:")
    print(f"  JSON: {json_path}")
    print(f"  CSV:  {csv_path}")

    # 被过滤数据
    if args.rejected_out:
        rej_stem = args.rejected_out
    else:
        rej_stem = str(input_path.parent / input_path.stem.replace("_jobs", "_jobs_rejected"))
    rej_json, rej_csv = save_data(result["rejected"], rej_stem)
    print(f"\n过滤数据已保存:")
    print(f"  JSON: {rej_json}")
    print(f"  CSV:  {rej_csv}")


if __name__ == "__main__":
    main()
