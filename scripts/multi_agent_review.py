#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多子代理语义审查结果聚合器

对 4 个专家子代理的审查输出进行标准化、去重、分级、冲突检测，
并与自动验证报告合并，生成最终审查报告。

用法：
    python multi_agent_review.py <patent_content.json> \
        --auto-report auto_report.json \
        --reviews-dir reviews/ \
        --output final_report.json

reviews/ 目录下应包含子代理输出文件（默认文件名）：
    - logic-chain-reviewer.json
    - enablement-reviewer.json
    - scope-reviewer.json
    - writing-quality-reviewer.json

输出：
    合并后的 review_report.json，新增 semantic_review 字段，向后兼容。
"""

import argparse
import json
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional


# 在 Windows 终端中强制使用 UTF-8 输出，避免中文乱码
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


# 子代理默认文件名映射
AGENT_FILE_MAP = {
    "LogicChainReviewer": "logic-chain-reviewer.json",
    "EnablementReviewer": "enablement-reviewer.json",
    "ScopeReviewer": "scope-reviewer.json",
    "WritingQualityReviewer": "writing-quality-reviewer.json",
}

# ==================== Schema 验证 ====================

REQUIRED_ISSUE_FIELDS = {"id", "severity", "category", "location", "description", "suggestion"}
VALID_SEVERITIES = {"error", "warning"}
VALID_CATEGORIES = {
    # LogicChainReviewer
    "断链", "错位", "缺失",
    # EnablementReviewer
    "参数缺失", "公式模糊", "公开不充分", "验证缺失", "特征未覆盖",
    # ScopeReviewer
    "范围过窄", "范围过宽", "必要特征缺失", "从权层次不足", "主题覆盖不全", "支持不足",
    # WritingQualityReviewer
    "推导不足", "套路化表述", "背景冗长", "长句问题", "术语不统一", "摘要缺失",
}


def validate_issue(issue: Dict[str, Any], agent_name: str) -> Optional[Dict[str, Any]]:
    """验证并修复单个 issue 的结构。返回 None 表示该 issue 应被丢弃。"""
    # 检查必要字段
    missing = REQUIRED_ISSUE_FIELDS - set(issue.keys())
    if missing:
        # 缺少关键字段的 issue 无法定位或修复，丢弃
        return None

    # 规范化 severity：确保全小写，修正常见拼写错误
    sev = issue.get("severity", "warning").lower().strip()

    # 常见拼写修正
    SEVERITY_FIXUPS = {
        "err": "error", "errror": "error", "fatal": "error",
        "warn": "warning", "warnning": "warning", "info": "warning", "suggestion": "warning",
    }
    sev = SEVERITY_FIXUPS.get(sev, sev)

    # 最终兜底：不在合法列表则默认 warning
    if sev not in VALID_SEVERITIES:
        sev = "warning"

    issue["severity"] = sev

    # 检查 category 是否在已知列表中
    cat = issue.get("category", "")
    if cat and cat not in VALID_CATEGORIES:
        # 不在已知列表但仍保留——可能是新类别，只做最佳匹配建议
        # 不做丢弃处理
        pass

    # 补全缺失的字段
    if "root_cause" not in issue:
        issue["root_cause"] = ""
    if "related_sections" not in issue:
        issue["related_sections"] = []

    return issue


def sanitize_score(raw_score: Any) -> int:
    """确保评分在 0-100 范围内"""
    try:
        s = int(raw_score)
        return max(0, min(100, s))
    except (ValueError, TypeError):
        return 100


# 冲突类别对：同一 location 出现相反类别时标记冲突
CONFLICT_CATEGORY_PAIRS = [
    ("范围过窄", "范围过宽"),
    ("必要特征缺失", "范围过窄"),  # 一个说缺特征，一个说范围太窄（含非必要特征）
    ("推导不足", "推导过度"),  # 少见
]

# 类别优先级：用于排序，数字越小越靠前
CATEGORY_PRIORITY = {
    "必要特征缺失": 1,
    "公开不充分": 2,
    "断链": 3,
    "错位": 4,
    "缺失": 5,
    "范围过窄": 6,
    "范围过宽": 7,
    "主题覆盖不全": 8,
    "从权层次不足": 9,
    "支持不足": 10,
    "参数缺失": 11,
    "公式模糊": 12,
    "验证缺失": 13,
    "特征未覆盖": 14,
    "推导不足": 15,
    "套路化表述": 16,
    "背景冗长": 17,
    "长句问题": 18,
    "术语不统一": 19,
    "摘要缺失": 20,
}

# location 优先级：用于排序，数字越小越靠前
LOCATION_PRIORITY = {
    "权利要求": 1,
    "权利要求1": 1,
    "claims": 1,
    "发明内容": 2,
    "技术问题": 3,
    "技术方案": 4,
    "有益效果": 5,
    "具体实施方式": 6,
    "背景技术": 7,
    "摘要": 8,
}


def load_json(path: Path) -> Any:
    """加载 JSON 文件"""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    """保存 JSON 文件"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def extract_json_block(text: str) -> Optional[str]:
    """从文本中提取第一个 JSON 代码块或 JSON 对象"""
    # 尝试提取 ```json ... ```
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        return m.group(1)
    # 尝试提取第一个 {...}
    m = re.search(r"(\{.*\})", text, re.DOTALL)
    if m:
        return m.group(1)
    return None


def safe_load_review(path: Path, agent_name: str) -> Optional[Dict[str, Any]]:
    """安全加载子代理输出，包含 JSON 提取 + Schema 验证"""
    try:
        raw_text = path.read_text(encoding="utf-8")
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError:
            block = extract_json_block(raw_text)
            if block:
                data = json.loads(block)
            else:
                raise
        # 确保包含必要字段
        if "agent" not in data:
            data["agent"] = agent_name
        if "issues" not in data:
            data["issues"] = []
        if "score" not in data:
            data["score"] = 100
        # 规范化评分
        data["score"] = sanitize_score(data["score"])

        # Schema 验证每个 issue
        raw_issues = data.get("issues", [])
        valid_issues = []
        issues_dropped = 0
        for issue in raw_issues:
            validated = validate_issue(issue, agent_name)
            if validated:
                valid_issues.append(validated)
            else:
                issues_dropped += 1

        if issues_dropped > 0:
            print(f"警告：{agent_name} 的 {issues_dropped} 个 issue 因缺少必要字段被丢弃",
                  file=sys.stderr)

        data["issues"] = valid_issues
        # 如果 issues 被丢弃，重新评估 score 可能是必要的——但保留原始 score
        if "summary" not in data:
            data["summary"] = ""

        return data
    except Exception as e:
        print(f"警告：无法加载 {path} ({agent_name}): {e}", file=sys.stderr)
        return None


def load_reviews(reviews_dir: Path) -> List[Dict[str, Any]]:
    """加载 reviews 目录下所有子代理输出"""
    reviews = []
    already_loaded = set()  # 已加载的文件路径，避免重复分配
    for agent_name, default_file in AGENT_FILE_MAP.items():
        file_path = reviews_dir / default_file
        if file_path.exists():
            review = safe_load_review(file_path, agent_name)
            if review:
                reviews.append(review)
                already_loaded.add(str(file_path.resolve()))
        else:
            # 尝试按代理名查找匹配的 .json 文件
            pattern = f"*{agent_name.replace('Reviewer', '').lower()}*.json"
            candidates = [p for p in reviews_dir.glob(pattern)
                         if str(p.resolve()) not in already_loaded]
            for candidate in candidates:
                review = safe_load_review(candidate, agent_name)
                if review:
                    reviews.append(review)
                    already_loaded.add(str(candidate.resolve()))
                    break
    return reviews


def text_similarity(a: str, b: str) -> float:
    """计算两段中文文本的相似度（0-1）"""
    if not a or not b:
        return 0.0
    # 简单清洗：去除标点和空格
    a_clean = re.sub(r"[\s\n，。；：！？、\"'（）()\[\]【】]", "", a)
    b_clean = re.sub(r"[\s\n，。；：！？、\"'（）()\[\]【】]", "", b)
    return SequenceMatcher(None, a_clean, b_clean).ratio()


def normalize_issue(issue: Dict[str, Any], agent_name: str, agg_index: int) -> Dict[str, Any]:
    """将子代理 issue 转换为统一格式"""
    location = issue.get("location", "")
    description = issue.get("description", "")
    return {
        "id": f"AGG-{agg_index:03d}",
        "severity": issue.get("severity", "warning"),
        "category": issue.get("category", "其他"),
        "location": location,
        "description": description,
        "suggestion": issue.get("suggestion", ""),
        "source_agents": [agent_name],
        "related_sections": issue.get("related_sections", []),
        "root_cause": issue.get("root_cause", ""),
    }


def normalize_issues(reviews: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """标准化所有子代理的 issues"""
    normalized = []
    idx = 1
    for review in reviews:
        agent_name = review.get("agent", "Unknown")
        for issue in review.get("issues", []):
            normalized.append(normalize_issue(issue, agent_name, idx))
            idx += 1
    return normalized


def deduplicate_issues(issues: List[Dict[str, Any]], threshold: float = 0.6) -> List[Dict[str, Any]]:
    """基于 location 和 description 相似度去重"""
    unique = []
    for issue in issues:
        merged = False
        for u in unique:
            same_location = issue["location"] == u["location"]
            similar_location = (
                issue["location"] in u["location"] or u["location"] in issue["location"]
            ) if not same_location else False
            loc_match = same_location or similar_location
            sim = text_similarity(issue["description"], u["description"])
            if loc_match and sim >= threshold:
                # 合并：保留 severity 更高的
                severity_rank = {"error": 2, "warning": 1}
                if severity_rank.get(issue["severity"], 0) > severity_rank.get(u["severity"], 0):
                    u["severity"] = issue["severity"]
                # 合并 source_agents
                if issue["source_agents"][0] not in u["source_agents"]:
                    u["source_agents"].append(issue["source_agents"][0])
                # 如果新建议更详细，保留
                if len(issue.get("suggestion", "")) > len(u.get("suggestion", "")):
                    u["suggestion"] = issue["suggestion"]
                merged = True
                break
        if not merged:
            unique.append(issue)
    return unique


def is_conflict_pair(cat_a: str, cat_b: str) -> bool:
    """判断两个类别是否构成冲突对"""
    for pair in CONFLICT_CATEGORY_PAIRS:
        if (cat_a in pair and cat_b in pair) and cat_a != cat_b:
            return True
    return False


def detect_conflicts(issues: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """检测冲突 issue

    两阶段检测：
    1. 类别冲突对（同一location，类别在 CONFLICT_CATEGORY_PAIRS 中）
    2. 建议方向相反（同一location，一个建议增加特征，另一个建议删除）
    3. 作为兜底：同一 location 上 description 相似度 ≥ 0.5 且来自不同 agent，
       类别不同——标记为潜在冲突供主代理判断。
    """
    conflicts = []
    seen = set()
    for i, a in enumerate(issues):
        for j, b in enumerate(issues):
            if i >= j:
                continue
            # 必须是不同 agent
            a_agents = set(a.get("source_agents", []))
            b_agents = set(b.get("source_agents", []))
            if a_agents == b_agents:
                continue

            # 基于 location 相似度和 description 相似度判断关联
            loc_sim = text_similarity(a["location"], b["location"])
            desc_sim = text_similarity(a.get("description", ""), b.get("description", ""))

            # location 至少需要部分匹配
            if a["location"] != b["location"] and loc_sim < 0.6:
                continue

            # 条件 A：类别冲突对
            cat_conflict = is_conflict_pair(a["category"], b["category"])

            # 条件 B：建议方向相反（关键词检测 + 宽松版）
            keywords_add = ["增加", "补充", "添加", "写入", "新增", "加入",
                          "将其.*写入", "保留.*并", "移入"]
            keywords_remove = ["删除", "移除", "移出", "不包含", "移[至到]",
                             "不应.*包含", "去掉", "去除"]
            a_sugg = a.get("suggestion", "")
            b_sugg = b.get("suggestion", "")
            a_add = any(re.search(k, a_sugg) for k in keywords_add)
            b_remove = any(re.search(k, b_sugg) for k in keywords_remove)
            a_remove = any(re.search(k, a_sugg) for k in keywords_remove)
            b_add = any(re.search(k, b_sugg) for k in keywords_add)
            opposite = (a_add and b_remove) or (a_remove and b_add)

            # 条件 C：兜底——location 相同 + description 相似 + 类别不同
            soft_conflict = (
                (a["location"] == b["location"] or loc_sim >= 0.8)
                and desc_sim >= 0.5
                and a["category"] != b["category"]
            )

            if cat_conflict or opposite or soft_conflict:
                pair_id = tuple(sorted([a["id"], b["id"]]))
                if pair_id not in seen:
                    seen.add(pair_id)
                    # 区分确定性
                    confidence = "高" if (cat_conflict or opposite) else "低（需主代理确认）"
                    conflicts.append({
                        "id_a": a["id"],
                        "id_b": b["id"],
                        "reason": (
                            f"{'/'.join(a['source_agents'])} 认为'{a['category']}'"
                            f"，{'/'.join(b['source_agents'])} 认为'{b['category']}'"
                            f"，建议方向可能冲突【置信度: {confidence}】"
                        ),
                        "source_agents": list(a_agents | b_agents),
                    })
    return conflicts


def get_category_priority(category: str) -> int:
    """获取类别优先级"""
    return CATEGORY_PRIORITY.get(category, 99)


def get_location_priority(location: str) -> int:
    """获取 location 优先级"""
    for key, value in LOCATION_PRIORITY.items():
        if key in location:
            return value
    return 99


def sort_issues(issues: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """对 issues 进行分级排序"""
    severity_rank = {"error": 0, "warning": 1}
    return sorted(
        issues,
        key=lambda x: (
            severity_rank.get(x.get("severity", "warning"), 1),
            get_category_priority(x.get("category", "")),
            get_location_priority(x.get("location", "")),
        ),
    )


def aggregate(reviews: List[Dict[str, Any]]) -> Dict[str, Any]:
    """聚合 4 个子代理的输出"""
    normalized = normalize_issues(reviews)
    deduped = deduplicate_issues(normalized)
    sorted_issues = sort_issues(deduped)

    # 重新编号（确保冲突检测使用最终 ID）
    for idx, issue in enumerate(sorted_issues, start=1):
        issue["id"] = f"AGG-{idx:03d}"

    conflicts = detect_conflicts(sorted_issues)

    # 计算总体评分（使用已 sanitized 的 score）
    scores = [sanitize_score(r.get("score", 100)) for r in reviews]
    overall_score = round(sum(scores) / len(scores)) if scores else 100

    # 如果存在 errors，封顶 84 分
    has_error = any(issue["severity"] == "error" for issue in sorted_issues)
    if has_error and overall_score >= 85:
        overall_score = 84

    errors = [i for i in sorted_issues if i["severity"] == "error"]
    warnings = [i for i in sorted_issues if i["severity"] == "warning"]

    agent_names = [r.get("agent", "Unknown") for r in reviews]

    # 注明审查覆盖度
    EXPECTED_REVIEWERS = 4
    coverage_note = ""
    if len(agent_names) < EXPECTED_REVIEWERS:
        missing = EXPECTED_REVIEWERS - len(agent_names)
        coverage_note = f"（注意：仅 {len(agent_names)}/{EXPECTED_REVIEWERS} 位审查员参与，{missing} 位缺失，评分置信度降低）"

    summary_parts = []
    if errors:
        summary_parts.append(f"发现 {len(errors)} 个 error")
    if warnings:
        summary_parts.append(f"{len(warnings)} 个 warning")
    if conflicts:
        summary_parts.append(f"{len(conflicts)} 处冲突待裁决")

    summary = f"语义审查评分 {overall_score} 分{coverage_note}。" + "，".join(summary_parts)
    if not summary_parts:
        summary += "未发现明显语义问题。"

    return {
        "aggregated": True,
        "agents": agent_names,
        "overall_score": overall_score,
        "total_issues": len(sorted_issues),
        "errors": len(errors),
        "warnings": len(warnings),
        "issues": sorted_issues,
        "conflicts": conflicts,
        "summary": summary,
    }


def merge_with_auto_report(
    semantic_review: Dict[str, Any],
    auto_report: Optional[Dict[str, Any]],
    patent_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """将语义审查结果与自动验证报告合并"""
    if auto_report is None:
        auto_report = {
            "pass": True,
            "errors": [],
            "warnings": [],
            "stats": {},
            "summary": "",
        }

    final_report = dict(auto_report)
    stats = final_report.get("stats", {})
    stats["semantic_score"] = semantic_review.get("overall_score", 100)
    stats["semantic_errors"] = semantic_review.get("errors", 0)
    stats["semantic_warnings"] = semantic_review.get("warnings", 0)
    final_report["stats"] = stats
    final_report["semantic_review"] = semantic_review

    # 重写 summary
    auto_summary = auto_report.get("summary", "")
    auto_pass = "[PASS]" in auto_summary or final_report.get("pass", False)
    if auto_pass:
        final_report["summary"] = (
            f"[PASS] 自动验证通过。{semantic_review.get('summary', '')}"
        )
    else:
        final_report["summary"] = (
            f"[FAIL] 自动验证未通过。{semantic_review.get('summary', '')}"
        )

    return final_report


def main():
    parser = argparse.ArgumentParser(description="多子代理语义审查结果聚合器")
    parser.add_argument("patent_json", help="专利内容 JSON 文件路径")
    parser.add_argument("--auto-report", help="自动验证报告路径（可选）")
    parser.add_argument("--reviews-dir", default="reviews", help="子代理输出目录（默认：reviews/）")
    parser.add_argument("--output", required=True, help="最终审查报告输出路径")
    parser.add_argument("--threshold", type=float, default=0.6, help="去重相似度阈值（默认 0.6）")
    args = parser.parse_args()

    patent_path = Path(args.patent_json)
    reviews_dir = Path(args.reviews_dir)
    output_path = Path(args.output)

    if not patent_path.exists():
        print(f"错误：专利内容文件不存在 {patent_path}", file=sys.stderr)
        sys.exit(1)

    patent_data = load_json(patent_path)

    auto_report = None
    if args.auto_report:
        auto_report_path = Path(args.auto_report)
        if auto_report_path.exists():
            auto_report = load_json(auto_report_path)

    if not reviews_dir.exists():
        print(f"错误：reviews 目录不存在 {reviews_dir}，语义审查已跳过！", file=sys.stderr)
        reviews = []
    else:
        reviews = load_reviews(reviews_dir)

    if not reviews:
        # 没有子代理输出 = 语义审查被跳过，报告明确标记为 MISSING
        print("", file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        print("  警告：语义审查未执行！", file=sys.stderr)
        print("  原因：未找到任何子代理审查输出文件。", file=sys.stderr)
        print("  影响：语义层面的逻辑链/充分公开/保护范围/", file=sys.stderr)
        print("        写作质量问题可能未被发现。", file=sys.stderr)
        print("  建议：确认 reviews/ 目录存在且包含子代理 JSON。", file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        print("", file=sys.stderr)
        semantic_review = {
            "aggregated": True,
            "agents": [],
            "skipped": True,
            "overall_score": 0,
            "total_issues": 0,
            "errors": 0,
            "warnings": 0,
            "issues": [],
            "conflicts": [],
            "summary": "语义审查未执行：未找到子代理审查输出文件。请确认 reviews/ 目录存在。",
        }
    else:
        semantic_review = aggregate(reviews)

    final_report = merge_with_auto_report(semantic_review, auto_report, patent_data)
    save_json(output_path, final_report)
    print(f"已生成最终审查报告：{output_path}")
    if semantic_review.get("skipped"):
        print(f"语义审查：已跳过（score=0）")
    else:
        print(f"语义评分：{semantic_review['overall_score']}，issues：{semantic_review['total_issues']}")


if __name__ == "__main__":
    main()
