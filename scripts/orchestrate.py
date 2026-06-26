#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
专利生成一键编排器

将撰写完成后的完整流水线打包为单一命令：

    python scripts/orchestrate.py patent_content.json --output output.docx

流水线步骤：
    1. 自动验证（validate_patent_json.py）
    2. 多子代理语义审查聚合（multi_agent_review.py，需 reviews/ 目录）
    3. 综合评分与退出建议
    4. 生成 Word（generate_patent_docx.py）

退出码：
    0 — 质量通过，Word 已生成
    1 — 自动验证发现 errors，Word 未生成（需修复后重试）
    2 — 语义审查评分 < 70，Word 未生成（需修复后重试）
    3 — 评分一般（70-84），Word 已生成但建议优化
    4 — 脚本内部错误（文件缺失、JSON 解析失败等）
    5 — 语义审查未执行，综合评分强制封顶 69，Word 未生成

用例：
    # 完整流水线（需先完成手动语义审查，见 SKILL.md 步骤3b）
    python scripts/orchestrate.py patent.json --output out.docx

    # 分部写作后入口（草稿已有 review_report.json）
    python scripts/orchestrate.py drafts/lamp/draft.json \
        --auto-report drafts/lamp/review.json \
        --output out.docx

    # 仅自动验证（语义审查前先用此命令排查硬性错误）
    python scripts/orchestrate.py patent.json --dry-run

    # 跳过语义审查（评分强制封顶 69，Word 不会生成，仅用于快速自检）
    python scripts/orchestrate.py patent.json --dry-run --skip-semantic
"""

import argparse
import json
import sys
import copy
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# Ensure UTF-8 output on Windows to avoid UnicodeEncodeError with Chinese characters
if sys.platform == 'win32':
    try:
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass

# Ensure scripts/ is in path
SCRIPTS_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPTS_DIR))

from validate_patent_json import validate_patent_json, format_report as format_auto_report


# ==================== 综合评分 ====================

def compute_composite_score(auto_report: Dict[str, Any],
                            semantic_review: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    计算综合质量评分。

    自动验证分 = max(0, 100 - errors×15 - warnings×3)
    语义审查分 = 4 子代理等权平均（或 0 如果跳过）
    综合评分   = 自动验证分 × 0.4 + 语义审查分 × 0.6

    语义审查被跳过时，综合评分强制封顶 69 分——因为最关键的逻辑链、
    充分公开、保护范围审查均未执行，自动验证通过不足以证明质量合格。
    """
    n_errors = len(auto_report.get("errors", []))
    n_warnings = len(auto_report.get("warnings", []))
    auto_score = max(0, 100 - n_errors * 15 - n_warnings * 3)

    semantic_skipped = (semantic_review is None or semantic_review.get("skipped", False))

    if not semantic_skipped:
        semantic_score = semantic_review.get("overall_score", 85)
        semantic_weight = 0.6
        auto_weight = 0.4
        composite = round(auto_score * auto_weight + semantic_score * semantic_weight)
    else:
        semantic_score = 0
        # 语义审查跳过时：仅用自动验证分，但强制封顶 69。
        # 使用比例映射而非一刀切截断，使不同质量的稿件得分可区分：
        #   100 → 69,  95 → 65,  85 → 58,  70 → 48
        # 所有跳过语义审查的稿件统一落在 <70 区段，不会生成 Word，
        # 但用户可以从得分了解自动验证层面的质量差异。
        composite = min(auto_score * 2 // 3, 69)

    # 质量等级
    if semantic_skipped:
        tier = "C"
        tier_label = "🔴 需改进 — 语义审查未执行，综合评分强制封顶 69。请完成多子代理语义审查后重试。"
    elif composite >= 85:
        tier = "A"
        tier_label = "🟢 优秀 — 可直接用于提交前审阅"
    elif composite >= 70:
        tier = "B"
        tier_label = "🟡 良好 — 基本合格，建议根据 warning 优化后提交"
    else:
        tier = "C"
        tier_label = "🔴 需改进 — 存在较严重问题，建议修复后再生成"

    return {
        "composite_score": composite,
        "tier": tier,
        "tier_label": tier_label,
        "auto_score": auto_score,
        "semantic_score": semantic_score if semantic_score > 0 else None,
        "semantic_skipped": semantic_skipped,
        "auto_errors": n_errors,
        "auto_warnings": n_warnings,
        "semantic_errors": semantic_review.get("errors", 0) if semantic_review else 0,
        "semantic_warnings": semantic_review.get("warnings", 0) if semantic_review else 0,
    }


# ==================== 主流程 ====================

def run_pipeline(patent_json_path: Path,
                 output_docx: Optional[Path],
                 auto_report_path: Optional[Path] = None,
                 reviews_dir: Optional[Path] = None,
                 skip_semantic: bool = False,
                 dry_run: bool = False,
                 strict: bool = False) -> Tuple[int, Dict[str, Any]]:
    """
    执行完整流水线。

    返回：(exit_code, summary_dict)
    """

    # ---- 加载专利 JSON ----
    if not patent_json_path.exists():
        print(f"错误：专利文件不存在 {patent_json_path}", file=sys.stderr)
        return 4, {"error": "patent_json_not_found"}

    try:
        with open(patent_json_path, "r", encoding="utf-8") as f:
            patent_data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"错误：无法解析专利 JSON 文件 — {e}", file=sys.stderr)
        return 4, {"error": "patent_json_parse_failed", "detail": str(e)}
    except OSError as e:
        print(f"错误：无法读取文件 {patent_json_path} — {e}", file=sys.stderr)
        return 4, {"error": "patent_json_read_failed", "detail": str(e)}

    # ---- 步骤 1：自动验证 ----
    print("=" * 60)
    print("步骤 1/3：自动验证（硬性规则检查）")
    print("=" * 60)

    if auto_report_path and auto_report_path.exists():
        with open(auto_report_path, "r", encoding="utf-8") as f:
            auto_report = json.load(f)
        print(f"  使用已有报告: {auto_report_path}")
        print(f"  pass: {auto_report.get('pass', '?')}, "
              f"errors: {len(auto_report.get('errors', []))}, "
              f"warnings: {len(auto_report.get('warnings', []))}")
    else:
        auto_report = validate_patent_json(patent_data, strict=strict)
        print(format_auto_report(auto_report))
    print()

    n_errors = len(auto_report.get("errors", []))
    n_warnings = len(auto_report.get("warnings", []))

    if n_errors > 0:
        print(f"⛔ 自动验证发现 {n_errors} 个错误，必须修复后重试。")
        print(f"   运行 python scripts/validate_patent_json.py {patent_json_path} 查看详情。")
        return 1, {"auto_report": auto_report}

    print(f"✓ 自动验证通过（{n_warnings} warnings）")

    # ---- 步骤 2：多子代理语义审查 ----
    semantic_review = None

    if skip_semantic:
        print("\n⏩ 跳过语义审查（--skip-semantic）。")
    else:
        print()
        print("=" * 60)
        print("步骤 2/3：多子代理语义审查")
        print("=" * 60)

        reviews_path = reviews_dir or Path("reviews")
        if not reviews_path.exists():
            reviews_path.mkdir(parents=True, exist_ok=True)
            print(f"📁 已创建 reviews/ 目录 ({reviews_path})，请将 4 个子代理输出放置于此。")
            print(f"  语义审查暂时跳过，子代理输出就位后请重新运行编排脚本。")
        else:
            from multi_agent_review import load_reviews, aggregate
            reviews = load_reviews(reviews_path)
            if not reviews:
                print("⚠ 未找到任何子代理输出文件，语义审查跳过。")
            else:
                semantic_review = aggregate(reviews)
                print(f"语义评分: {semantic_review['overall_score']} 分")
                print(f"发现 issues: {semantic_review['total_issues']} "
                      f"（errors: {semantic_review.get('errors', 0)}, "
                      f"warnings: {semantic_review.get('warnings', 0)}）")

                if semantic_review.get("conflicts"):
                    print(f"冲突: {len(semantic_review['conflicts'])} 处待主代理裁决")
                print()

    # ---- 步骤 3：综合评分 ----
    print()
    print("=" * 60)
    print("步骤 3/3：综合评分与质量判定")
    print("=" * 60)

    score = compute_composite_score(auto_report, semantic_review)
    print(f"  综合评分: {score['composite_score']} 分 ({score['tier']} 级)")
    print(f"  自动验证分: {score['auto_score']} 分"
          f"（errors: {score['auto_errors']}, warnings: {score['auto_warnings']}）")
    if score['semantic_score'] is not None:
        print(f"  语义审查分: {score['semantic_score']} 分"
              f"（errors: {score['semantic_errors']}, warnings: {score['semantic_warnings']}）")
    else:
        print(f"  ⚠ 未执行语义审查，综合评分强制封顶 69（自动验证分 {score['auto_score']} → 封顶至 {score['composite_score']}）")
    print(f"  {score['tier_label']}")
    print()

    summary = {
        "composite": score,
        "auto_report": auto_report,
        "semantic_review": semantic_review,
    }

    # 5. 判断是否生成 Word
    if dry_run:
        print("🔍 干跑模式，不生成 Word。")
        if score.get('semantic_skipped'):
            return 5, summary
        if score['composite_score'] < 70:
            return 2, summary
        return 0, summary

    if score['composite_score'] < 70:
        if score.get('semantic_skipped'):
            print("⛔ 语义审查未执行，综合评分强制封顶 69，Word 未生成。")
            print("   请完成多子代理语义审查后重试（见 SKILL.md「步骤3b」）。")
            return 5, summary
        print("⛔ 综合评分 < 70，Word 未生成。请修复问题后重试。")
        return 2, summary

    # ---- 生成 Word ----
    if output_docx is None:
        print("未指定 --output，跳过 Word 生成。")
        return 0, summary

    from generate_patent_docx import generate_patent_docx
    generate_patent_docx(patent_data, str(output_docx))
    print()

    if score['composite_score'] >= 85:
        print("✅ 质量优秀，专利文档已生成！")
        return 0, summary
    else:
        print("⚠ 文档已生成，但评分一般（70-84），建议根据 warning 优化后重新生成。")
        return 3, summary


# ==================== 阶段内验证辅助 ====================

# 各阶段对应的验证章节（仅用于 stage 2 和 stage 6——这两个阶段有独立的顶级 section）
STAGE_SECTION_MAP = {
    2: "claims",
    6: "abstract",
}

# 各阶段应已完成填写的 specification 子字段
# 阶段 3/4/5 的验证会用占位符填充尚未涉及的字段，避免空字段误报
STAGE_SPEC_COMPLETED_FIELDS = {
    3: {"tech_field", "background", "problem"},
    4: {"tech_field", "background", "problem", "solution", "effects"},
    5: {"tech_field", "background", "problem", "solution", "effects", "figure_desc", "embodiment"},
}

# 用于填充未完成字段的合法占位符（长度和内容都满足验证规则）
# 注意：不使用"本发明"或"本实用新型"以避免术语一致性检查误报
_SPEC_PLACEHOLDERS = {
    "tech_field": "本申请涉及通用技术领域，具体涉及一种通用装置及方法。",
    "background": "现有技术中，相关方案存在效率较低、成本较高的缺陷，无法满足实际应用的需求。亟需一种改进的技术方案来解决上述问题。",
    "problem": "现有技术中相关方案存在效率和成本方面的不足，无法很好地应用于实际场景。",
    "solution": "本申请提供一种技术方案，包括：\n\nS1：获取输入数据。\n\nS2：对所述输入数据进行处理，得到处理结果。\n\nS3：输出所述处理结果。",
    "effects": "本申请通过优化数据处理流程，实现了处理效率的提升。与现有技术相比，在相同条件下处理时间缩短，具有实用性。",
    "figure_desc": "图1是本申请实施例的流程示意图。",
    # S 步骤之间用 \n\n 分隔，避免 check_s_step_paragraph_separation 误报 warning
    "embodiment": (
        "实施例1：\n\n"
        "S1：获取输入数据，对数据进行预处理。\n\n"
        "S2：对预处理后的数据进行分析计算。\n\n"
        "S3：输出计算结果。\n\n"
        "经验证，本实施例能够有效解决所述技术问题。"
    ),
}


def stage_validate(patent_json_path: Path, stage: int, strict: bool = False) -> bool:
    """
    分部写作阶段内快速验证。返回 True 表示通过（0 errors）。

    阶段 2 仅验证 claims，阶段 6 仅验证 abstract。
    阶段 3/4/5 验证整个 specification，但用占位符填充当前阶段尚未涉及的字段，
    避免空字段产生误报 error。
    """
    with open(patent_json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 阶段 2 和 6 有独立的顶级 section，直接按 section 验证
    if stage in STAGE_SECTION_MAP:
        section = STAGE_SECTION_MAP[stage]
        report = validate_patent_json(data, section=section, strict=strict)
        section_label = {"claims": "权利要求书", "abstract": "摘要"}[section]
    elif stage in (3, 4, 5):
        # 阶段 3/4/5：用占位符填充未完成字段，整体验证 specification
        completed = STAGE_SPEC_COMPLETED_FIELDS[stage]
        data = _fill_spec_placeholders(data, completed)
        report = validate_patent_json(data, section="specification", strict=strict)
        section_label = "说明书（阶段验证）"
    else:
        print(f"⚠ 阶段 {stage} 没有对应的验证逻辑，跳过。")
        return True

    n_err = len(report.get("errors", []))
    n_warn = len(report.get("warnings", []))

    if n_err == 0 and n_warn == 0:
        print(f"  ✓ 阶段{stage} ({section_label}) 验证通过")
        return True
    elif n_err == 0:
        print(f"  ⚠ 阶段{stage} ({section_label}) 通过但有 {n_warn} 个 warning")
        for w in report.get("warnings", [])[:3]:
            print(f"     - [{w.get('rule', '')}] {w.get('message', '')[:60]}")
        return True
    else:
        print(f"  ✗ 阶段{stage} ({section_label}) 发现 {n_err} 个 error：")
        for e in report.get("errors", [])[:5]:
            print(f"     - [{e.get('rule', '')}] {e.get('message', '')[:70]}")
        # 提示用户如何查看完整报告
        section_arg = {2: 'claims', 3: 'specification', 4: 'specification', 5: 'specification', 6: 'abstract'}.get(stage)
        print(f"\n  运行以下命令可查看完整审查报告：")
        print(f"     python scripts/validate_patent_json.py {patent_json_path} --section {section_arg}")
        return False


def _fill_spec_placeholders(data: dict, completed_fields: set) -> dict:
    """
    用合法占位符填充 specification 中尚未完成的字段，返回新 dict（不修改原数据）。

    这样在分部写作阶段验证时，未涉及的字段不会因空值触发误报 error。
    """
    data = copy.deepcopy(data)
    spec = data.setdefault("sections", {}).setdefault("specification", {})
    ic = spec.setdefault("invention_content", {})
    # Guard: if invention_content exists but is not a dict (e.g., null, empty string),
    # replace with empty dict so downstream .get() calls don't crash.
    if not isinstance(ic, dict):
        spec["invention_content"] = {}
        ic = spec["invention_content"]

    # 规范化 completed_fields：problem/solution/effects 属于 invention_content
    field_map = {
        "tech_field": ("spec_top", "tech_field"),
        "background": ("spec_top", "background"),
        "figure_desc": ("spec_top", "figure_desc"),
        "embodiment": ("spec_top", "embodiment"),
        "problem": ("ic", "problem"),
        "solution": ("ic", "solution"),
        "effects": ("ic", "effects"),
    }

    for logical_name, placeholder in _SPEC_PLACEHOLDERS.items():
        if logical_name in completed_fields:
            continue  # 该字段已由用户完成，不覆盖
        mapping = field_map.get(logical_name)
        if mapping is None:
            continue
        container, key = mapping
        if container == "spec_top":
            if not spec.get(key):
                spec[key] = placeholder
        elif container == "ic":
            if not ic.get(key):
                ic[key] = placeholder

    return data


# ==================== CLI ====================

def main():
    parser = argparse.ArgumentParser(
        description="专利生成一键编排器 — 自动验证 → 语义审查 → 综合评分 → 生成 Word",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 先干跑验证
  python scripts/orchestrate.py patent.json --dry-run
  # 语义审查通过后生成 Word
  python scripts/orchestrate.py patent.json --output out.docx
  # 阶段验证
  python scripts/orchestrate.py patent.json --stage-validate 2
        """,
    )
    parser.add_argument("patent_json", help="专利内容 JSON 文件路径")
    parser.add_argument("--output", "-o", help="输出 .docx 路径")
    parser.add_argument("--auto-report", help="已有的自动验证报告路径（跳过重复验证）")
    parser.add_argument("--reviews-dir", default="reviews", help="子代理输出目录（默认 reviews/）")
    parser.add_argument("--skip-semantic", action="store_true", help="跳过多子代理语义审查")
    parser.add_argument("--strict", action="store_true", help="严格模式（warnings 升级为 errors）")
    parser.add_argument("--dry-run", action="store_true", help="仅审查，不生成 Word")
    parser.add_argument("--stage-validate", type=int, choices=[2, 3, 4, 5, 6],
                        help="分部写作阶段内验证（2=权利要求, 3=背景/问题, 4=方案/效果, 5=附图/实施例, 6=摘要）")

    args = parser.parse_args()
    patent_path = Path(args.patent_json)

    # 阶段验证快捷模式
    if args.stage_validate:
        ok = stage_validate(patent_path, args.stage_validate, strict=args.strict)
        sys.exit(0 if ok else 1)

    # 完整流水线
    output_path = Path(args.output) if args.output else None
    reviews = Path(args.reviews_dir) if args.reviews_dir else None

    auto_report = Path(args.auto_report) if args.auto_report else None

    exit_code, _ = run_pipeline(
        patent_json_path=patent_path,
        output_docx=output_path,
        auto_report_path=auto_report,
        reviews_dir=reviews,
        skip_semantic=args.skip_semantic,
        dry_run=args.dry_run,
        strict=args.strict,
    )

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
