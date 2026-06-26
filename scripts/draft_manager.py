#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
专利草稿管理器

管理分部写作过程中的草稿文件（JSON + stage 追踪 + 过程笔记）。

用法：
    python draft_manager.py init <slug> <专利类型>          # 创建新草稿
    python draft_manager.py save <slug> <阶段号> <json>     # 保存阶段产出
    python draft_manager.py load <slug>                     # 读取完整草稿
    python draft_manager.py list                            # 列出所有草稿
    python draft_manager.py delete <slug>                   # 删除草稿
    python draft_manager.py status <slug>                   # 查看当前阶段

草稿文件结构（drafts/{slug}/）:
    draft.json   — 累积草稿（完整 JSON，未完成章节留空）
    stage.txt    — 当前阶段号（1-6，'done' 表示全部完成）
    notes.md     — 阶段产出摘要 + 用户反馈记录
"""

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure UTF-8 output on Windows to avoid UnicodeEncodeError with Chinese characters
if sys.platform == 'win32':
    try:
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass


# 草稿目录（相对于 skill 根目录）
DRAFTS_DIR = Path(__file__).parent.parent / "drafts"

# 初始草稿模板
EMPTY_DRAFT_TEMPLATE = {
    "patent_type": "",
    "invention_name": "",
    "sections": {
        "claims": [],
        "specification": {
            "tech_field": "",
            "background": "",
            "invention_content": {
                "problem": "",
                "solution": "",
                "effects": "",
            },
            "figure_desc": "",
            "embodiment": "",
        },
        "abstract": {
            "text": "",
            "figure": "",
        },
    },
}

STAGE_NAMES = {
    1: "理解技术方案",
    2: "构建权利要求",
    3: "确定名称 + 背景技术 + 技术问题",
    4: "发明内容（技术方案 + 有益效果）",
    5: "附图说明 + 具体实施方式",
    6: "撰写摘要",
}


def ensure_draft_dir(slug: str) -> Path:
    """确保草稿目录存在"""
    draft_dir = DRAFTS_DIR / slug
    draft_dir.mkdir(parents=True, exist_ok=True)
    return draft_dir


def init_draft(slug: str, patent_type: str = "发明专利") -> Path:
    """创建新草稿，返回草稿目录路径"""
    draft_dir = ensure_draft_dir(slug)

    draft = dict(EMPTY_DRAFT_TEMPLATE)
    draft["patent_type"] = patent_type

    with open(draft_dir / "draft.json", "w", encoding="utf-8") as f:
        json.dump(draft, f, ensure_ascii=False, indent=2)

    with open(draft_dir / "stage.txt", "w", encoding="utf-8") as f:
        f.write("1")

    with open(draft_dir / "notes.md", "w", encoding="utf-8") as f:
        f.write(f"# 草稿笔记: {slug}\n\n")
        f.write(f"创建时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"专利类型: {patent_type}\n\n")

    return draft_dir


def merge_stage_data(draft: dict, stage_data: dict, stage: int) -> dict:
    """将阶段产出合并入草稿 JSON（浅合并：只覆盖非空值）"""
    # 顶层字段
    for key in ("patent_type", "invention_name"):
        if key in stage_data and stage_data[key]:
            draft[key] = stage_data[key]

    # sections 层
    if "sections" not in stage_data:
        return draft

    sections = stage_data["sections"]
    draft_sections = draft.setdefault("sections", {})

    # 权利要求
    if "claims" in sections and sections["claims"]:
        draft_sections["claims"] = sections["claims"]

    # 摘要
    abstract = sections.get("abstract", {})
    if abstract:
        draft_abs = draft_sections.setdefault("abstract", {})
        if abstract.get("text"):
            draft_abs["text"] = abstract["text"]
        if abstract.get("figure"):
            draft_abs["figure"] = abstract["figure"]

    # 说明书子章节
    spec = sections.get("specification", {})
    if not spec:
        return draft

    draft_spec = draft_sections.setdefault("specification", {})
    for field in ("tech_field", "background", "figure_desc", "embodiment"):
        if field in spec and spec[field]:
            draft_spec[field] = spec[field]

    # 发明内容
    ic = spec.get("invention_content", {})
    if ic:
        draft_ic = draft_spec.setdefault("invention_content", {})
        for field in ("problem", "solution", "effects"):
            if field in ic and ic[field]:
                draft_ic[field] = ic[field]

    return draft


def save_stage(slug: str, stage: int, stage_data: dict, notes: str = "") -> Path:
    """
    保存阶段产出到草稿。

    stage_data: 该阶段产出的 JSON 片段（可以是完整 JSON 的局部）。
    notes: 该阶段的摘要说明，会追加到 notes.md。
    """
    draft_dir = ensure_draft_dir(slug)

    # 读取现有草稿
    draft_path = draft_dir / "draft.json"
    if draft_path.exists():
        with open(draft_path, "r", encoding="utf-8") as f:
            draft = json.load(f)
    else:
        draft = dict(EMPTY_DRAFT_TEMPLATE)

    # 合并阶段数据
    draft = merge_stage_data(draft, stage_data, stage)

    # 写回草稿
    with open(draft_path, "w", encoding="utf-8") as f:
        json.dump(draft, f, ensure_ascii=False, indent=2)

    # 更新阶段号
    # 语义：stage.txt 记录"当前处理到哪个阶段"。
    # 只有保存的恰好是当前阶段时才自动推进（首次完成该阶段）；
    # 重新保存更早阶段时不应回退当前阶段号。
    # 读取当前阶段号
    current_stage_num = 1
    stage_path = draft_dir / "stage.txt"
    if stage_path.exists():
        current_str = stage_path.read_text(encoding="utf-8").strip()
        if current_str == "done":
            current_stage_num = None  # 已完成，不再推进
        else:
            try:
                current_stage_num = int(current_str)
            except (ValueError, TypeError):
                current_stage_num = 1

    if current_stage_num is not None and stage >= current_stage_num:
        # 完成当前阶段或之后阶段 → 自动推进到下一阶段
        next_stage = stage + 1
        stage_label = "done" if next_stage > 6 else str(next_stage)
    elif current_stage_num is None:
        # 草稿已完成，不再修改阶段号
        stage_label = "done"
    else:
        # 重新保存更早的阶段 → 保持当前阶段号不变
        stage_label = str(current_stage_num)

    with open(stage_path, "w", encoding="utf-8") as f:
        f.write(stage_label)

    # 追加笔记
    stage_name = STAGE_NAMES.get(stage, f"阶段{stage}")
    with open(draft_dir / "notes.md", "a", encoding="utf-8") as f:
        f.write(f"## 阶段{stage}: {stage_name}\n\n")
        f.write(f"完成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        if notes:
            f.write(notes + "\n\n")
        f.write("---\n\n")

    return draft_dir


def load_draft(slug: str) -> Optional[Dict[str, Any]]:
    """加载草稿，返回 {draft, stage, slug} 或 None"""
    draft_dir = DRAFTS_DIR / slug
    draft_path = draft_dir / "draft.json"
    stage_path = draft_dir / "stage.txt"

    if not draft_path.exists():
        return None

    with open(draft_path, "r", encoding="utf-8") as f:
        draft = json.load(f)

    stage_str = "1"
    if stage_path.exists():
        stage_str = stage_path.read_text(encoding="utf-8").strip()

    if stage_str == "done":
        stage = None
    else:
        try:
            stage = int(stage_str)
        except (ValueError, TypeError):
            stage = 1  # 回退到阶段1

    notes = ""
    notes_path = draft_dir / "notes.md"
    if notes_path.exists():
        notes = notes_path.read_text(encoding="utf-8")

    return {
        "slug": slug,
        "draft": draft,
        "stage": stage,
        "stage_label": STAGE_NAMES.get(stage, "已完成") if stage else "已完成",
        "notes": notes,
    }


def list_drafts() -> List[Dict[str, Any]]:
    """列出所有草稿，返回 [{slug, stage, name, ...}]"""
    if not DRAFTS_DIR.exists():
        return []

    drafts = []
    for draft_dir in sorted(DRAFTS_DIR.iterdir(), reverse=True):
        if not draft_dir.is_dir():
            continue
        info = load_draft(draft_dir.name)
        if info:
            name = info["draft"].get("invention_name", "") or draft_dir.name
            drafts.append({
                "slug": draft_dir.name,
                "name": name,
                "patent_type": info["draft"].get("patent_type", ""),
                "stage": info["stage"],
                "stage_label": info["stage_label"],
            })
    return drafts


def delete_draft(slug: str) -> bool:
    """删除草稿目录"""
    draft_dir = DRAFTS_DIR / slug
    if draft_dir.exists():
        shutil.rmtree(draft_dir)
        return True
    return False


def get_stage(slug: str) -> Optional[int]:
    """读取当前所处阶段号"""
    stage_path = DRAFTS_DIR / slug / "stage.txt"
    if not stage_path.exists():
        return None
    stage_str = stage_path.read_text(encoding="utf-8").strip()
    if stage_str == "done":
        return None
    try:
        return int(stage_str)
    except (ValueError, TypeError):
        return None  # 格式错误时回退，与 load_draft 行为一致


def get_incomplete_drafts() -> List[Dict[str, Any]]:
    """获取所有未完成的草稿（stage 为 1-6）"""
    all_drafts = list_drafts()
    return [d for d in all_drafts if d["stage"] is not None and 1 <= d["stage"] <= 6]


def main():
    parser = argparse.ArgumentParser(description="专利草稿管理器")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="创建新草稿")
    p_init.add_argument("slug", help="草稿标识（英文 slug）")
    p_init.add_argument("patent_type", nargs="?", default="发明专利", help="专利类型")

    p_save = sub.add_parser("save", help="保存阶段产出")
    p_save.add_argument("slug")
    p_save.add_argument("stage", type=int, choices=range(1, 7), metavar="STAGE",
                        help="阶段号（1-6）")
    p_save.add_argument("json_file", help="阶段产出 JSON 文件路径")
    p_save.add_argument("--notes", default="", help="阶段摘要说明")

    p_load = sub.add_parser("load", help="读取草稿")
    p_load.add_argument("slug")

    p_list = sub.add_parser("list", help="列出所有草稿")

    p_delete = sub.add_parser("delete", help="删除草稿")
    p_delete.add_argument("slug")

    p_status = sub.add_parser("status", help="查看当前阶段")
    p_status.add_argument("slug")

    args = parser.parse_args()

    if args.command == "init":
        draft_dir = init_draft(args.slug, args.patent_type)
        print(f"草稿已创建：{draft_dir}")
        print(f"当前阶段：1 - {STAGE_NAMES[1]}")

    elif args.command == "save":
        with open(args.json_file, "r", encoding="utf-8") as f:
            stage_data = json.load(f)
        draft_dir = save_stage(args.slug, args.stage, stage_data, args.notes)
        stage_name = STAGE_NAMES.get(args.stage, f"阶段{args.stage}")
        print(f"阶段{args.stage}已保存：{stage_name}")
        print(f"草稿目录：{draft_dir}")

    elif args.command == "load":
        info = load_draft(args.slug)
        if info:
            print(json.dumps(info["draft"], ensure_ascii=False, indent=2))
            if info["stage"]:
                print(f"\n当前阶段：{info['stage']} - {info['stage_label']}")
            else:
                print("\n状态：已完成")
        else:
            print(f"错误：草稿不存在 '{args.slug}'", file=sys.stderr)
            sys.exit(1)

    elif args.command == "list":
        drafts = list_drafts()
        if not drafts:
            print("（无草稿）")
        for d in drafts:
            marker = "✓" if d["stage"] is None else f"阶段{d['stage']}"
            print(f"  [{marker}] {d['slug']} — {d['name']} ({d['patent_type']})")

    elif args.command == "delete":
        if delete_draft(args.slug):
            print(f"草稿 '{args.slug}' 已删除。")
        else:
            print(f"错误：草稿不存在 '{args.slug}'", file=sys.stderr)
            sys.exit(1)

    elif args.command == "status":
        stage = get_stage(args.slug)
        if stage:
            print(f"草稿 '{args.slug}' 当前处于：阶段{stage} - {STAGE_NAMES.get(stage, '')}")
        else:
            print(f"草稿 '{args.slug}' 已完成或不存在。")


if __name__ == "__main__":
    main()
