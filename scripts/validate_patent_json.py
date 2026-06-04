#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
专利内容自动验证器

对专利申请 JSON 内容执行自动化规则检查，输出结构化审查报告。

用法：
    python validate_patent_json.py <input.json> [--output report.json] [--strict]

选项：
    --output     将审查报告保存到指定 JSON 文件（默认输出到 stdout）
    --strict     将 warnings 也视为 errors（用于迭代收敛判断）

检查维度：
    1. 格式完整性 — 五章节齐全、权利要求非空
    2. 术语规范   — "本发明"/"本实用新型"一致，禁用模糊词
    3. 禁用模式   — 序号词、商业用语、套路化表述
    4. 逻辑一致性 — 技术问题↔背景缺陷↔有益效果对应
    5. 结构规范   — 权利要求格式、背景技术长度、摘要字数
    6. 实施例质量 — 分步格式、效果验证、数学语言
"""

import json
import re
import sys
from pathlib import Path
from collections import Counter


# ==================== 规则定义 ====================

# 模糊用语黑名单
FUZZY_WORDS = [
    '约', '左右', '基本上', '大致', '较好', '最好是', '高温',
    '适当', '必要时', '优选', '大概', '差不多', '近似',
]

# 序号词黑名单（说明书正文禁用）
SEQUENCE_WORDS = [
    r'第一[，,、\s]', r'第二[，,、\s]', r'第三[，,、\s]',
    r'第四[，,、\s]', r'第五[，,、\s]',
    r'首先[，,、\s]', r'其次[，,、\s]', r'最后[，,、\s]',
    r'（一）', r'（二）', r'（三）', r'（四）', r'（五）',
    r'\(1\)', r'\(2\)', r'\(3\)',
]

# 商业宣传用语
COMMERCIAL_WORDS = [
    '市场前景广阔', '市场前景良好', '首创', '革命性', '颠覆性',
    '用户体验好', '使用便捷', '操作简单', '成本低廉',
    '全球领先', '国内首创', '国际领先', '填补空白',
    '具有巨大的商业价值', '具有广阔的市场',
]

# 套路化技术问题表述
PROHIBITED_PROBLEM_PATTERNS = [
    r'所要解决的技术问题是提供',
    r'目的是提供一种',
    r'旨在提供一种',
    r'本发明[的]?目的是',
    r'本实用新型[的]?目的是',
]

# 权利要求书禁用模式
CLAIM_FUZZY = [
    '约', '左右', '基本上', '大致', '较好', '最好是',
]

# 句子开头禁用的"由于"模式
LEADING_BECAUSE_PATTERN = r'(?:^|[。；！？\n])\s*由于'

# 项目符号列表模式
BULLET_PATTERNS = [
    r'^\s*[-–—*•]\s',      # - 或 * 项目符号
    r'^\s*\d+[\.\)、]\s',   # 1. 2) 等
    r'^\s*[（\(]\d+[）\)]\s', # (1) 等
]

# 名称中的禁用词
NAME_FORBIDDEN = [
    '及其类似物', '及其他', '等', '新型',
]


def find_line_number(text, pos):
    """根据字符位置找到行号"""
    if pos < 0 or pos >= len(text):
        return None
    return text[:pos].count('\n') + 1


def check_fuzzy_words(text, location, patent_type="发明专利"):
    """检查模糊用语"""
    issues = []
    # 对于权利要求书，所有模糊词都是问题
    # 对于说明书，需要检查是否在合理上下文中
    for word in FUZZY_WORDS:
        for m in re.finditer(re.escape(word), text):
            ctx_start = max(0, m.start() - 10)
            ctx_end = min(len(text), m.end() + 10)
            ctx = text[ctx_start:ctx_end].replace('\n', ' ')
            line_num = find_line_number(text, m.start())
            issues.append({
                "rule": "no-fuzzy-words",
                "severity": "error",
                "location": location,
                "line": line_num,
                "message": f"发现模糊用语「{word}」",
                "context": f"...{ctx}...",
                "suggestion": f"将「{word}」替换为具体数值或明确描述",
            })
    return issues


def check_sequence_words(text, location):
    """检查序号词"""
    issues = []
    for pattern in SEQUENCE_WORDS:
        for m in re.finditer(pattern, text):
            ctx_start = max(0, m.start() - 5)
            ctx_end = min(len(text), m.end() + 15)
            ctx = text[ctx_start:ctx_end].replace('\n', ' ')
            line_num = find_line_number(text, m.start())
            issues.append({
                "rule": "no-sequence-words",
                "severity": "error",
                "location": location,
                "line": line_num,
                "message": f"发现序号词「{m.group().strip()}」",
                "context": f"...{ctx}...",
                "suggestion": "说明书正文禁用序号词。多个效果用分段叙述或「进一步地」衔接。",
            })
    return issues


def check_commercial_language(text, location):
    """检查商业宣传用语"""
    issues = []
    for word in COMMERCIAL_WORDS:
        for m in re.finditer(re.escape(word), text):
            ctx_start = max(0, m.start() - 10)
            ctx_end = min(len(text), m.end() + 10)
            ctx = text[ctx_start:ctx_end].replace('\n', ' ')
            line_num = find_line_number(text, m.start())
            issues.append({
                "rule": "no-commercial-language",
                "severity": "error",
                "location": location,
                "line": line_num,
                "message": f"发现商业宣传用语「{word}」",
                "context": f"...{ctx}...",
                "suggestion": "用客观技术语言替代，如用数据说明效果。",
            })
    return issues


def check_prohibited_problem_patterns(text, location):
    """检查套路化技术问题表述"""
    issues = []
    for pattern in PROHIBITED_PROBLEM_PATTERNS:
        for m in re.finditer(pattern, text):
            ctx_start = max(0, m.start() - 10)
            ctx_end = min(len(text), m.end() + 30)
            ctx = text[ctx_start:ctx_end].replace('\n', ' ')
            line_num = find_line_number(text, m.start())
            issues.append({
                "rule": "no-formulaic-problem",
                "severity": "error",
                "location": location,
                "line": line_num,
                "message": f"技术问题不应使用「所要解决的技术问题是提供/目的是提供一种」等套路化表述",
                "context": f"...{ctx}...",
                "suggestion": "从背景技术的缺陷自然引出技术问题，聚焦核心技术矛盾。写法：'现有[方案]存在[具体缺陷]，导致[后果]。'",
            })
    return issues


def check_leading_because(text, location):
    """检查句子开头的'由于'"""
    issues = []
    for m in re.finditer(LEADING_BECAUSE_PATTERN, text):
        ctx_start = max(0, m.start())
        ctx_end = min(len(text), m.end() + 30)
        ctx = text[ctx_start:ctx_end].replace('\n', ' ')
        line_num = find_line_number(text, m.start())
        issues.append({
            "rule": "no-leading-because",
            "severity": "warning",
            "location": location,
            "line": line_num,
            "message": "句子以「由于」开头，导致读者长时间等待主句",
            "context": f"{ctx}...",
            "suggestion": "改为「本发明通过[特征]实现[效果]，因为[原理]」的结构，效果放在前面。",
        })
    return issues


def check_bullet_lists(text, location):
    """检查项目符号列表"""
    issues = []
    lines = text.split('\n')
    for i, line in enumerate(lines):
        for pattern in BULLET_PATTERNS:
            if re.match(pattern, line.strip()):
                issues.append({
                    "rule": "no-bullet-lists",
                    "severity": "error",
                    "location": location,
                    "line": i + 1,
                    "message": f"发现项目符号列表格式「{line.strip()[:30]}...」",
                    "context": line.strip(),
                    "suggestion": "说明书正文必须用连续段落叙述，不得使用项目符号或自动编号列表。",
                })
                break  # 每行只报一次
    return issues


def check_terminology_consistency(text, expected_term, location):
    """检查术语一致性"""
    issues = []
    wrong_term = "本实用新型" if expected_term == "本发明" else "本发明"

    for m in re.finditer(re.escape(wrong_term), text):
        ctx_start = max(0, m.start() - 10)
        ctx_end = min(len(text), m.end() + 10)
        ctx = text[ctx_start:ctx_end].replace('\n', ' ')
        line_num = find_line_number(text, m.start())
        issues.append({
            "rule": "terminology-consistency",
            "severity": "error",
            "location": location,
            "line": line_num,
            "message": f"术语不一致：应为「{expected_term}」，但发现「{wrong_term}」",
            "context": f"...{ctx}...",
            "suggestion": f"全文统一使用「{expected_term}」。",
        })
    return issues


def check_claim_format(claim_text, claim_number, patent_type):
    """检查单条权利要求格式"""
    issues = []

    # 独立权利要求（第1条）必须包含"其特征在于"
    if claim_number == 1:
        if "其特征在于" not in claim_text:
            issues.append({
                "rule": "independent-claim-format",
                "severity": "error",
                "location": f"权利要求 {claim_number}",
                "line": None,
                "message": "独立权利要求缺少「其特征在于」连接用语",
                "context": claim_text[:100],
                "suggestion": "独立权利要求应包含前序部分和特征部分，用「其特征在于」连接。",
            })

    # 检查开放/封闭式连接词
    if "由...组成" in claim_text or "由……组成" in claim_text:
        # 仅化学组合物可用封闭式，非化学领域应用开放式
        if patent_type == "发明专利":
            # 宽松处理，仅对明显非化学的做警告
            pass

    # 从属权利要求格式
    if claim_number > 1:
        if not re.search(r'根据权利要求\d+', claim_text):
            issues.append({
                "rule": "dependent-claim-format",
                "severity": "error",
                "location": f"权利要求 {claim_number}",
                "line": None,
                "message": "从属权利要求缺少引用部分",
                "context": claim_text[:100],
                "suggestion": "从属权利要求应以「根据权利要求X所述的[主题名称]，其特征在于」开头。",
            })

    # 多项从属权利要求不能引用另一多项从属
    multi_refs = re.findall(r'根据权利要求(\d+)或(\d+)', claim_text)
    if multi_refs and claim_number > 1:
        issues.append({
            "rule": "multi-dependent-claim",
            "severity": "warning",
            "location": f"权利要求 {claim_number}",
            "line": None,
            "message": "发现多项从属权利要求，注意不得作为另一多项从属权利要求的基础",
            "context": claim_text[:100],
            "suggestion": "确认该多项从属权利要求未被其他多项从属引用。",
        })

    # 每项权利要求结尾应为句号
    if claim_text.strip() and not claim_text.strip().endswith('。'):
        issues.append({
            "rule": "claim-ending-period",
            "severity": "warning",
            "location": f"权利要求 {claim_number}",
            "line": None,
            "message": "权利要求结尾应为中文句号「。」",
            "context": claim_text[-50:] if len(claim_text) > 50 else claim_text,
            "suggestion": "在权利要求末尾添加句号。",
        })

    return issues


def parse_section_paragraphs(text):
    """按空行分隔段落，返回段落列表"""
    if not text:
        return []
    paragraphs = re.split(r'\n\s*\n', text)
    return [p.strip() for p in paragraphs if p.strip()]


def validate_patent_json(data, strict=False):
    """
    验证专利 JSON 数据，返回审查报告。

    参数：
        data: 专利 JSON 对象
        strict: True 时将 warnings 升级为 errors

    返回：
        {
            "pass": bool,
            "errors": [...],
            "warnings": [...],
            "stats": {...},
            "summary": "..."
        }
    """
    errors = []
    warnings = []
    stats = {}

    patent_type = data.get("patent_type", "")
    invention_name = data.get("invention_name", "")
    sections = data.get("sections", {})

    expected_term = "本发明" if "发明" in patent_type and "实用新型" not in patent_type else "本实用新型"

    # ============ 1. 基本信息检查 ============

    # 专利类型
    if patent_type not in ("发明专利", "实用新型专利"):
        errors.append({
            "rule": "patent-type-valid",
            "severity": "error",
            "location": "根字段",
            "line": None,
            "message": f"专利类型无效或缺失：'{patent_type}'",
            "context": None,
            "suggestion": "patent_type 必须为「发明专利」或「实用新型专利」。",
        })

    # 发明名称
    if not invention_name:
        errors.append({
            "rule": "invention-name-missing",
            "severity": "error",
            "location": "根字段",
            "line": None,
            "message": "缺少发明名称",
            "context": None,
            "suggestion": "添加 invention_name 字段。",
        })
    else:
        stats["name_length"] = len(invention_name)
        if len(invention_name) > 40:
            errors.append({
                "rule": "invention-name-length",
                "severity": "error",
                "location": "发明名称",
                "line": None,
                "message": f"发明名称过长（{len(invention_name)}字，建议≤25字，不应超过40字）",
                "context": invention_name,
                "suggestion": "精简发明名称，去除冗余修饰词。",
            })
        for forbidden in NAME_FORBIDDEN:
            if forbidden in invention_name:
                errors.append({
                    "rule": "invention-name-forbidden",
                    "severity": "error",
                    "location": "发明名称",
                    "line": None,
                    "message": f"发明名称含禁用词「{forbidden}」",
                    "context": invention_name,
                    "suggestion": f"从发明名称中删除「{forbidden}」。",
                })

    # 名称中的术语一致性
    if expected_term and invention_name:
        wrong_term = "本实用新型" if expected_term == "本发明" else "本发明"
        if wrong_term in invention_name:
            errors.append({
                "rule": "terminology-consistency",
                "severity": "error",
                "location": "发明名称",
                "line": None,
                "message": f"名称中术语不一致：应为「{expected_term}」，发现「{wrong_term}」",
                "context": invention_name,
                "suggestion": "名称应与专利类型匹配。",
            })

    # ============ 2. 权利要求书检查 ============

    claims = sections.get("claims", [])
    stats["claims_count"] = len(claims)

    if not claims:
        errors.append({
            "rule": "claims-empty",
            "severity": "error",
            "location": "权利要求书",
            "line": None,
            "message": "权利要求书为空",
            "context": None,
            "suggestion": "至少撰写一项独立权利要求和一项从属权利要求。",
        })
    else:
        # 检查从属权利要求
        has_independent = False
        has_dependent = False
        for i, claim_text in enumerate(claims, 1):
            if not isinstance(claim_text, str) or not claim_text.strip():
                errors.append({
                    "rule": "claim-empty",
                    "severity": "error",
                    "location": f"权利要求 {i}",
                    "line": None,
                    "message": f"第{i}项权利要求为空",
                    "context": None,
                    "suggestion": "填写完整的权利要求内容。",
                })
                continue

            # 模糊用语检查
            errors.extend(check_fuzzy_words(claim_text, f"权利要求 {i}", patent_type))

            # 权利要求格式检查
            claim_issues = check_claim_format(claim_text, i, patent_type)
            errors.extend([ci for ci in claim_issues if ci["severity"] == "error"])
            warnings.extend([ci for ci in claim_issues if ci["severity"] == "warning"])

            if i == 1:
                has_independent = True
            if i > 1 and "根据权利要求" in claim_text:
                has_dependent = True

        if not has_independent:
            errors.append({
                "rule": "no-independent-claim",
                "severity": "error",
                "location": "权利要求书",
                "line": None,
                "message": "未找到独立权利要求",
                "context": None,
                "suggestion": "至少需要一项独立权利要求。",
            })
        if not has_dependent:
            warnings.append({
                "rule": "no-dependent-claim",
                "severity": "warning",
                "location": "权利要求书",
                "line": None,
                "message": "未找到从属权利要求",
                "context": None,
                "suggestion": "建议撰写2-5项从属权利要求，形成层次化保护梯度。",
            })

        # 方法类发明：检查权利要求是否使用了分步格式
        if patent_type == "发明专利" and claims:
            has_method_claim = any(
                re.search(r'方法|步骤|S\d|流程|过程', c) for c in claims
            )
            if has_method_claim:
                has_step_format = any(
                    re.search(r'S\d', c) for c in claims
                )
                if not has_step_format:
                    warnings.append({
                        "rule": "method-claim-step-format",
                        "severity": "warning",
                        "location": "权利要求书",
                        "line": None,
                        "message": "方法类权利要求建议使用 S1/S2/S3 格式逐步骤撰写",
                        "context": claims[0][:100] if claims else None,
                        "suggestion": "方法类权利要求用 S1/S2/S3 分步，每步一个关键技术动作。",
                    })

    # ============ 3. 说明书检查 ============

    spec = sections.get("specification", {})
    if not spec:
        errors.append({
            "rule": "specification-missing",
            "severity": "error",
            "location": "说明书",
            "line": None,
            "message": "说明书内容缺失",
            "context": None,
            "suggestion": "必须提供说明书完整内容。",
        })
    else:
        # 3.1 技术领域
        tech_field = spec.get("tech_field", "")
        if not tech_field:
            errors.append({
                "rule": "tech-field-missing",
                "severity": "error",
                "location": "说明书 → 技术领域",
                "line": None,
                "message": "技术领域为空",
                "context": None,
                "suggestion": "添加技术领域（1-2句话即可）。",
            })
        else:
            stats["tech_field_length"] = len(tech_field)
            errors.extend(check_terminology_consistency(tech_field, expected_term, "技术领域"))

        # 3.2 背景技术
        background = spec.get("background", "")
        if not background:
            errors.append({
                "rule": "background-missing",
                "severity": "error",
                "location": "说明书 → 背景技术",
                "line": None,
                "message": "背景技术为空",
                "context": None,
                "suggestion": "添加背景技术（1-3段，200-400字）。",
            })
        else:
            stats["background_length"] = len(background)
            bg_paras = parse_section_paragraphs(background)
            stats["background_paragraphs"] = len(bg_paras)

            if len(bg_paras) > 3:
                warnings.append({
                    "rule": "background-too-long",
                    "severity": "warning",
                    "location": "说明书 → 背景技术",
                    "line": None,
                    "message": f"背景技术有 {len(bg_paras)} 段，建议控制在 1-3 段",
                    "context": None,
                    "suggestion": "精简背景技术，只写最接近的现有技术及其缺陷，不写行业发展/市场背景。",
                })
            if len(background) < 100:
                warnings.append({
                    "rule": "background-too-short",
                    "severity": "warning",
                    "location": "说明书 → 背景技术",
                    "line": None,
                    "message": f"背景技术仅 {len(background)} 字，可能过于简略",
                    "context": None,
                    "suggestion": "背景技术应充分说明现有技术的缺陷，帮助审查员理解发明的技术贡献。",
                })

            # 检查行业宏观分析
            macro_keywords = ['随着.*发展', '近年来', '国民经济', '市场', '消费者', '政策']
            for kw in macro_keywords:
                if re.search(kw, background):
                    warnings.append({
                        "rule": "no-macro-background",
                        "severity": "warning",
                        "location": "说明书 → 背景技术",
                        "line": None,
                        "message": f"背景技术可能包含行业宏观分析（发现「{kw}」相关表述）",
                        "context": None,
                        "suggestion": "背景技术不应写行业/市场/政策背景，应直接描述现有技术方案及其缺陷。",
                    })
                    break  # 只报一次

            # 检查背景技术中的术语
            errors.extend(check_terminology_consistency(background, expected_term, "背景技术"))
            # 检查项目符号
            errors.extend(check_bullet_lists(background, "背景技术"))

        # 3.3 发明内容
        invention_content = spec.get("invention_content", {})
        if not invention_content:
            errors.append({
                "rule": "invention-content-missing",
                "severity": "error",
                "location": "说明书 → 发明内容",
                "line": None,
                "message": "发明内容缺失",
                "context": None,
                "suggestion": "必须包含技术问题、技术方案、有益效果三个子节。",
            })
        else:
            # 3.3.1 技术问题
            problem = invention_content.get("problem", "")
            if not problem:
                errors.append({
                    "rule": "problem-missing",
                    "severity": "error",
                    "location": "说明书 → 技术问题",
                    "line": None,
                    "message": "技术问题为空",
                    "context": None,
                    "suggestion": "必须明确写出要解决的技术问题。",
                })
            else:
                errors.extend(check_prohibited_problem_patterns(problem, "技术问题"))
                errors.extend(check_terminology_consistency(problem, expected_term, "技术问题"))

            # 3.3.2 技术方案
            solution = invention_content.get("solution", "")
            if not solution:
                errors.append({
                    "rule": "solution-missing",
                    "severity": "error",
                    "location": "说明书 → 技术方案",
                    "line": None,
                    "message": "技术方案为空",
                    "context": None,
                    "suggestion": "必须写出技术方案。方法类用 S1/S2/S3 分步阐述。",
                })
            else:
                # 检查是否使用了项目符号
                errors.extend(check_bullet_lists(solution, "技术方案"))
                errors.extend(check_terminology_consistency(solution, expected_term, "技术方案"))
                errors.extend(check_sequence_words(solution, "技术方案"))

            # 3.3.3 有益效果
            effects = invention_content.get("effects", "")
            if not effects:
                errors.append({
                    "rule": "effects-missing",
                    "severity": "error",
                    "location": "说明书 → 有益效果",
                    "line": None,
                    "message": "有益效果为空",
                    "context": None,
                    "suggestion": "必须写出有益效果，与背景技术的缺陷一一对应。",
                })
            else:
                # 检查序号词
                errors.extend(check_sequence_words(effects, "有益效果"))
                # 检查"由于"开头
                warnings.extend(check_leading_because(effects, "有益效果"))
                # 检查商业用语
                errors.extend(check_commercial_language(effects, "有益效果"))
                # 检查术语
                errors.extend(check_terminology_consistency(effects, expected_term, "有益效果"))

                # 检查是否有推导逻辑
                effect_paras = parse_section_paragraphs(effects)
                has_derivation = False
                for ep in effect_paras:
                    if re.search(r'(?:通过|采用|利用|因为|由于).*(?:实现|使得|从而|有效|降低|提高|提升|减少|避免)', ep):
                        has_derivation = True
                        break
                if not has_derivation and effect_paras:
                    warnings.append({
                        "rule": "effects-no-derivation",
                        "severity": "warning",
                        "location": "说明书 → 有益效果",
                        "line": None,
                        "message": "有益效果可能缺少推导逻辑链（通过X特征→实现Y效果→因为Z原理）",
                        "context": effect_paras[0][:100] if effect_paras else None,
                        "suggestion": "每个效果都应有推导逻辑，不能是纯断言。模板：「本发明通过[区别特征]，实现了[效果]，因为[原理]。」",
                    })

                # 检查效果是否量化
                has_quantified = bool(re.search(r'\d+%|\d+倍|\d+小时|\d+℃|[提高降低减少增加缩短延长].*\d', effects))
                if not has_quantified:
                    warnings.append({
                        "rule": "effects-not-quantified",
                        "severity": "warning",
                        "location": "说明书 → 有益效果",
                        "line": None,
                        "message": "有益效果建议尽量量化（百分比、数值、倍数），避免纯定性描述",
                        "context": effects[:200],
                        "suggestion": "用具体数据替代形容词。如「效率显著提高」改为「效率提高30%」。",
                    })

        # 3.4 附图说明
        figure_desc = spec.get("figure_desc", "")
        if patent_type == "实用新型专利" and not figure_desc:
            warnings.append({
                "rule": "figure-desc-missing-utility",
                "severity": "warning",
                "location": "说明书 → 附图说明",
                "line": None,
                "message": "实用新型专利必须有附图说明",
                "context": None,
                "suggestion": "添加附图说明。实用新型专利说明书必须有附图。",
            })

        # 3.5 具体实施方式
        embodiment = spec.get("embodiment", "")
        if not embodiment:
            errors.append({
                "rule": "embodiment-missing",
                "severity": "error",
                "location": "说明书 → 具体实施方式",
                "line": None,
                "message": "具体实施方式为空",
                "context": None,
                "suggestion": "至少提供一个完整实施例，含 S步骤展开和效果验证。",
            })
        else:
            stats["embodiment_length"] = len(embodiment)

            # 检查是否有分步格式
            has_steps = bool(re.search(r'S\d', embodiment))
            if not has_steps:
                warnings.append({
                    "rule": "embodiment-no-steps",
                    "severity": "warning",
                    "location": "具体实施方式",
                    "line": None,
                    "message": "实施例未使用 S1/S2/S3 分步格式",
                    "context": embodiment[:200],
                    "suggestion": "实施例应使用 S1：/S2：/S3： 逐步骤展开，每步独占段落。",
                })

            # 检查是否有效果验证
            verification_keywords = [
                '效果验证', '测试结果', '实验数据', '性能测试',
                '测试表明', '结果表明', '数据表明', '对比',
                '验证', '测试条件', '实验条件',
            ]
            has_verification = any(kw in embodiment for kw in verification_keywords)
            if not has_verification:
                warnings.append({
                    "rule": "embodiment-no-verification",
                    "severity": "warning",
                    "location": "具体实施方式",
                    "line": None,
                    "message": "实施例缺少效果验证部分",
                    "context": None,
                    "suggestion": "实施例应包含效果验证：实验条件、测试数据、对比结果、结论。",
                })

            # 检查术语
            errors.extend(check_terminology_consistency(embodiment, expected_term, "具体实施方式"))
            # 检查项目符号
            errors.extend(check_bullet_lists(embodiment, "具体实施方式"))
            # 检查模糊用语
            errors.extend(check_fuzzy_words(embodiment, "具体实施方式", patent_type))

    # ============ 4. 摘要检查 ============

    abstract_data = sections.get("abstract", {})
    abstract_text = abstract_data.get("text", "")
    if not abstract_text:
        errors.append({
            "rule": "abstract-missing",
            "severity": "error",
            "location": "摘要",
            "line": None,
            "message": "摘要为空",
            "context": None,
            "suggestion": "必须提供摘要（≤300字）。",
        })
    else:
        stats["abstract_length"] = len(abstract_text)
        if len(abstract_text) > 300:
            errors.append({
                "rule": "abstract-too-long",
                "severity": "error",
                "location": "摘要",
                "line": None,
                "message": f"摘要字数 {len(abstract_text)} 字，超过300字限制",
                "context": None,
                "suggestion": "精简摘要至300字以内。",
            })

        # 检查是否包含必要元素
        if "技术领域" not in abstract_text and "涉及" not in abstract_text and "属于" not in abstract_text:
            warnings.append({
                "rule": "abstract-missing-elements",
                "severity": "warning",
                "location": "摘要",
                "line": None,
                "message": "摘要应包含发明名称、技术领域、技术方案要点和主要用途",
                "context": abstract_text[:200],
                "suggestion": "确保摘要包含完整的四要素。",
            })

        # 检查商业用语
        errors.extend(check_commercial_language(abstract_text, "摘要"))
        # 检查术语
        errors.extend(check_terminology_consistency(abstract_text, expected_term, "摘要"))

    # ============ 5. 整体一致性检查 ============

    # 5.1 术语交叉校验：所有正文中不应出现与专利类型相反的术语
    # 由于各章节已做术语检查，此处仅做汇总性校验
    all_body_text = ""
    for key in ["tech_field", "background"]:
        all_body_text += spec.get(key, "") + "\n"
    for key in ["problem", "solution", "effects"]:
        all_body_text += invention_content.get(key, "") if invention_content else ""
    all_body_text += spec.get("embodiment", "")
    all_body_text += abstract_text

    # 全局商业用语检查（各章节未单独检查此项）
    commercial_issues = check_commercial_language(all_body_text, "说明书正文（全局）")
    already_found_comm = set()
    for e in errors:
        if e["rule"] == "no-commercial-language":
            already_found_comm.add(e.get("message", "")[:50])
    for issue in commercial_issues:
        if issue.get("message", "")[:50] not in already_found_comm:
            errors.append(issue)

    # 5.2 方法类发明：S步骤一致性检查
    if patent_type == "发明专利":
        solution = invention_content.get("solution", "") if invention_content else ""
        solution_steps = re.findall(r'S(\d+)', solution)
        embodiment_steps = re.findall(r'S(\d+)', embodiment if embodiment else "")

        if solution_steps and embodiment_steps:
            if set(solution_steps) != set(embodiment_steps):
                warnings.append({
                    "rule": "step-consistency",
                    "severity": "warning",
                    "location": "跨章节",
                    "line": None,
                    "message": f"技术方案中的步骤 S{set(solution_steps)} 与实施例中的步骤 S{set(embodiment_steps)} 不完全一致",
                    "context": None,
                    "suggestion": "确保技术方案和实施例中的步骤编号一致。",
                })

    # ============ 6. strict 模式处理 ============

    if strict:
        for w in warnings:
            w_copy = dict(w)
            w_copy["severity"] = "error"
            errors.append(w_copy)
        warnings = []

    # ============ 汇总 ============

    total_issues = len(errors) + len(warnings)
    pass_check = len(errors) == 0

    if total_issues == 0:
        summary = "[PASS] 所有检查通过，专利内容符合规范要求。"
    elif pass_check:
        summary = f"[WARN] 发现 {len(warnings)} 个建议项，{len(errors)} 个错误。建议优化后再次检查。"
    else:
        summary = f"[FAIL] 发现 {len(errors)} 个错误，{len(warnings)} 个警告。需要修复后重新检查。"

    return {
        "pass": pass_check,
        "errors": errors,
        "warnings": warnings,
        "stats": stats,
        "summary": summary,
        "total_issues": total_issues,
    }


def format_report(report, color=False):
    """格式化审查报告为可读文本"""
    lines = []
    lines.append("=" * 60)
    lines.append("  专利申请内容自动审查报告")
    lines.append("=" * 60)
    lines.append("")

    # 统计信息
    stats = report.get("stats", {})
    if stats:
        lines.append("【基本信息】")
        if "name_length" in stats:
            lines.append(f"  发明名称字数: {stats['name_length']}")
        if "claims_count" in stats:
            lines.append(f"  权利要求数: {stats['claims_count']}")
        if "background_length" in stats:
            lines.append(f"  背景技术字数: {stats['background_length']}")
        if "background_paragraphs" in stats:
            lines.append(f"  背景技术段落数: {stats['background_paragraphs']}")
        if "abstract_length" in stats:
            lines.append(f"  摘要字数: {stats['abstract_length']}")
        lines.append("")

    # 错误
    if report["errors"]:
        lines.append(f"[ERROR] 共 {len(report['errors'])} 项")
        lines.append("-" * 40)
        for i, e in enumerate(report["errors"], 1):
            lines.append(f"  #{i} [{e['location']}] {e['message']}")
            if e.get("context"):
                ctx = e["context"]
                if len(ctx) > 80:
                    ctx = ctx[:77] + "..."
                lines.append(f"     context: {ctx}")
            if e.get("suggestion"):
                lines.append(f"     fix: {e['suggestion']}")
            lines.append("")
    else:
        lines.append("[ERROR] 无")
        lines.append("")

    # 警告
    if report["warnings"]:
        lines.append(f"[WARNING] 共 {len(report['warnings'])} 项")
        lines.append("-" * 40)
        for i, w in enumerate(report["warnings"], 1):
            lines.append(f"  #{i} [{w['location']}] {w['message']}")
            if w.get("suggestion"):
                lines.append(f"     fix: {w['suggestion']}")
            lines.append("")
    else:
        lines.append("[WARNING] 无")
        lines.append("")

    # 总结
    lines.append("=" * 60)
    lines.append(report["summary"])
    lines.append("=" * 60)

    return "\n".join(lines)


def main():
    # 在 Windows 上尝试设置 UTF-8 输出
    if sys.platform == 'win32':
        try:
            import io
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        except Exception:
            pass

    if len(sys.argv) < 2:
        print("用法: python validate_patent_json.py <input.json> [--output report.json] [--strict]")
        print("")
        print("选项:")
        print("  --output <path>  将审查报告保存为 JSON 文件")
        print("  --strict         将警告升级为错误")
        print("  --quiet          仅输出 JSON 报告（不打印可读格式）")
        sys.exit(1)

    input_path = Path(sys.argv[1])
    output_path = None
    strict = False
    quiet = False

    # 解析选项
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == '--output' and i + 1 < len(args):
            output_path = Path(args[i + 1])
            i += 2
        elif args[i] == '--strict':
            strict = True
            i += 1
        elif args[i] == '--quiet':
            quiet = True
            i += 1
        else:
            i += 1

    if not input_path.exists():
        print(f"错误: 输入文件不存在: {input_path}")
        sys.exit(1)

    with open(input_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    report = validate_patent_json(data, strict=strict)

    # 输出
    if output_path:
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"审查报告已保存: {output_path}")

    if not quiet:
        print(format_report(report))

    # 返回码：有错误时非零
    if not report["pass"]:
        sys.exit(1)


if __name__ == '__main__':
    main()
