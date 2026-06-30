"""
SKILL.md 解析器：将 Markdown 格式的技能文件解析为 SkillDefinition 对象
SKILL.md 格式：YAML frontmatter + Markdown 正文
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class SkillDefinition:
    """解析后的技能定义"""
    name: str
    description: str
    version: str = "1.0"
    triggers: list[str] = field(default_factory=list)  # 触发关键词列表
    content: str = ""                                   # 完整 Markdown 正文
    file_path: Optional[Path] = None

    def to_summary(self) -> str:
        """生成注入 System Prompt 的摘要文本"""
        triggers_str = "、".join(self.triggers) if self.triggers else "（无特定触发词）"
        return f"- **{self.name}**: {self.description}（触发：{triggers_str}）"

    def to_full_text(self) -> str:
        """返回完整的技能描述（用于详细注入）"""
        lines = [
            f"### Skill: {self.name}",
            f"描述: {self.description}",
        ]
        if self.triggers:
            lines.append(f"触发词: {', '.join(self.triggers)}")
        if self.content:
            lines.append("")
            lines.append(self.content)
        return "\n".join(lines)


def parse_skill_file(path: Path) -> Optional[SkillDefinition]:
    """
    解析 SKILL.md 文件，返回 SkillDefinition。
    支持两种格式：
    1. YAML frontmatter（---）+ Markdown 正文
    2. 纯 Markdown（从一级标题推断信息）
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    name = None
    description = ""
    version = "1.0"
    triggers: list[str] = []
    content = raw

    # ── 尝试解析 YAML frontmatter ─────────────────────
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            try:
                import yaml
                meta = yaml.safe_load(parts[1]) or {}
                name = meta.get("name")
                description = str(meta.get("description", ""))
                version = str(meta.get("version", "1.0"))
                raw_triggers = meta.get("triggers", [])
                if isinstance(raw_triggers, list):
                    # 支持两种写法：
                    # 1. 每个列表项是独立触发词（推荐）
                    # 2. 单个列表项内用 / 分隔多个触发词（宿主写法兼容）
                    for item in raw_triggers:
                        item_str = str(item)
                        if " / " in item_str or "/" in item_str:
                            # 将 "a / b / c" 拆分为 ["a", "b", "c"]
                            parts = [t.strip() for t in item_str.split("/") if t.strip()]
                            triggers.extend(parts)
                        else:
                            triggers.append(item_str.strip())
                elif isinstance(raw_triggers, str):
                    triggers = [t.strip() for t in raw_triggers.split(",")]
                content = parts[2].strip()
            except Exception:
                pass

    # ── 从 Markdown 内容中提取触发词 ──────────────────
    if not triggers:
        # 查找 "## 触发条件" 或 "## 触发词" 下的内容
        trigger_match = re.search(
            r"##\s*触发[条件词]?\s*\n(.+?)(?=\n##|\Z)",
            content,
            re.DOTALL,
        )
        if trigger_match:
            trigger_text = trigger_match.group(1).strip()
            # 提取引号内的词汇
            quoted = re.findall(r'["""]([^"""]+)["""]', trigger_text)
            if quoted:
                triggers = quoted

    # ── fallback：从文件名推断 name ───────────────────
    if not name:
        name = path.stem.replace("_", " ").replace("-", " ").replace(".skill", "")

    if not description:
        # 取正文第一个非空行作为描述
        for line in content.splitlines():
            line = line.strip().lstrip("#").strip()
            if line:
                description = line[:100]
                break

    return SkillDefinition(
        name=name,
        description=description,
        version=version,
        triggers=triggers,
        content=content,
        file_path=path,
    )
