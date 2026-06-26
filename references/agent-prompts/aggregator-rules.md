# 多子代理审查结果聚合规则

## 目的

将 4 个专家子代理（LogicChainReviewer、EnablementReviewer、ScopeReviewer、WritingQualityReviewer）的审查输出聚合为一份统一的语义审查报告，合并入现有的 `review_report.json`。

---

## 输入

- 4 个子代理的 JSON 输出，每个包含 `agent`、`issues[]`、`score`、`summary`。
- 可选：自动验证脚本输出的 `review_report.json`。

---

## 聚合步骤

### 1. 标准化

将每个子代理的 issue 转换为统一格式：

```json
{
  "id": "AGG-001",
  "severity": "error",
  "category": "断链",
  "location": "技术问题",
  "description": "...",
  "suggestion": "...",
  "source_agents": ["LogicChainReviewer"],
  "related_sections": ["background", "invention_content.problem"]
}
```

`id` 重新编号为 AGG-001, AGG-002, ...

### 2. 去重

如果两个 issue 满足以下所有条件，则视为重复：

- `location` 相同或在同一章节内
- `description` 的核心内容相似（使用简单字符串相似度，如共同关键词占比 ≥ 60%）

去重时保留 `severity` 最高的一个；若 severity 相同，保留 `source_agents` 更多的一个；若仍相同，保留描述更具体的一个。

### 3. 分级

按以下顺序排序：

1. `severity`：`error` 在前，`warning` 在后。
2. `category` 优先级：必要特征缺失 > 公开不充分 > 逻辑断链 > 范围过窄 > 推导不足 > 其他 warning。
3. `location`：权利要求书 > 发明内容 > 具体实施方式 > 背景技术 > 摘要。

### 4. 冲突检测

如果两个 issue 满足以下条件，则标记为冲突：

- `location` 相同
- `suggestion` 方向相反（如一个建议增加特征，另一个建议删除同一特征）
- 或 `category` 语义相反（如「范围过窄」与「范围过宽」）

冲突处理：

- 不合并冲突 issue，各自保留。
- 在 `conflicts` 数组中记录冲突对，包含双方 `id`、代理来源、冲突原因。
- 由主代理最终综合判断。

### 5. 评分汇总

计算整体语义评分：

```
overall_score = round((score_logic + score_enablement + score_scope + score_quality) / 4)
```

若某个子代理调用失败，则使用剩余代理的平均分，并在 `summary` 中注明。

---

## 输出格式

```json
{
  "semantic_review": {
    "aggregated": true,
    "agents": ["LogicChainReviewer", "EnablementReviewer", "ScopeReviewer", "WritingQualityReviewer"],
    "overall_score": 81,
    "total_issues": 7,
    "errors": 2,
    "warnings": 5,
    "issues": [...],
    "conflicts": [
      {
        "id_a": "AGG-003",
        "id_b": "AGG-004",
        "reason": "ScopeReviewer 建议删除特征 X，WritingQualityReviewer 建议保留并展开描述",
        "source_agents": ["ScopeReviewer", "WritingQualityReviewer"]
      }
    ],
    "summary": "语义审查评分81分，发现2个error、5个warning。主要问题：..."
  }
}
```

---

## 与自动验证报告合并

合并后的 `review_report.json` 结构（向后兼容）：

```json
{
  "pass": true,
  "errors": [...],
  "warnings": [...],
  "stats": {
    "semantic_score": 81,
    "semantic_errors": 2,
    "semantic_warnings": 5
  },
  "semantic_review": { ... },
  "summary": "[PASS] 自动验证通过。语义审查评分81分，发现2个error、5个warning。"
}
```

合并规则：

- 自动验证的 `errors` / `warnings` 保持原样。
- 语义审查的 issues 按 severity 分别计入 `semantic_review.errors` 和 `semantic_review.warnings`。
- `stats` 中新增 `semantic_score`、`semantic_errors`、`semantic_warnings`。
- `summary` 由主代理或脚本重写，整合两类审查结果。

---

## 迭代退出条件

聚合后的报告用于判断是否需要继续迭代修复：

| 条件 | 处理 |
|------|------|
| 0 errors + overall_score ≥ 85 | ✅ 通过，生成 Word |
| 0 errors + 70 ≤ overall_score < 85 | ✅ 通过，但交付时告知剩余 warning |
| 有 errors 或 overall_score < 70，且未到第 3 轮 | ⚠️ 继续迭代修复 |
| 有 errors 或 overall_score < 70，且已到第 3 轮 | ⚠️ 记录问题，交付时告知用户 |
| 语义审查被跳过（overall_score 被强制封顶 69） | ⚠️ 语义审查是质量保障的关键环节，强烈建议完成 |

---

## 异常处理

| 异常 | 处理 |
|------|------|
| 子代理输出非 JSON | 尝试从文本中提取 JSON 块；失败则跳过该代理，在 `summary` 中注明 |
| 子代理超时或调用失败 | 跳过该代理，使用剩余代理结果，并降低 overall_score 的置信度 |
| 所有子代理均失败 | 回退到 SKILL.md 中定义的「人工审查」清单，由主代理执行 |
