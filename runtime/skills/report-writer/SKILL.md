---
name: report-writer
description: 当用户要求生成结构化 Markdown 报告、调研总结、项目分析或实施计划时使用此技能。
allowed-tools: read_file, write_file, edit_file, ls, grep, glob
---

# Report Writer Skill

当使用此技能时，生成清晰的结构化 Markdown 报告。

## 工作流程

1. 从用户请求中明确报告目标。
2. 在写作前创建简明的提纲。
3. 在合适的地方使用标题、表格和清单。
4. 如果写入文件，将报告保存在 /workspace 下。
5. 以简短的总结和后续行动结束。

## 输出风格

- 默认使用中文。
- 偏向实用、注重实现导向。.
- 避免模糊的结论。
