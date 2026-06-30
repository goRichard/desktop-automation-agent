"""
Skills 注册与加载：扫描 user_skills 目录，加载所有 SKILL.md
提供 Skills 摘要注入 System Prompt
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from config import get_settings
from .parser import SkillDefinition, parse_skill_file

# 已加载的技能（名称 → SkillDefinition）
_skills: dict[str, SkillDefinition] = {}


def load_skills(skills_dir: Optional[Path] = None) -> int:
    """
    扫描 skills_dir 目录，加载所有 SKILL.md 文件。
    返回加载成功的数量。
    """
    global _skills
    _skills.clear()

    if skills_dir is None:
        skills_dir = get_settings().skills_dir

    skills_dir = Path(skills_dir)
    if not skills_dir.exists():
        skills_dir.mkdir(parents=True, exist_ok=True)
        return 0

    count = 0
    # 同时搜索 *.skill.md 和 SKILL.md（大小写不敏感）
    patterns = ["*.skill.md", "*.md", "SKILL.md"]
    found_files: set[Path] = set()

    for pattern in patterns:
        found_files.update(skills_dir.rglob(pattern))

    # 也搜索子目录中的 SKILL.md
    for subdir in skills_dir.iterdir():
        if subdir.is_dir():
            skill_file = subdir / "SKILL.md"
            if skill_file.exists():
                found_files.add(skill_file)

    for path in sorted(found_files):
        skill = parse_skill_file(path)
        if skill and skill.name:
            _skills[skill.name] = skill
            count += 1

    return count


def get_skill(name: str) -> Optional[SkillDefinition]:
    """根据名称获取技能"""
    return _skills.get(name)


def list_skills() -> list[SkillDefinition]:
    """返回所有已加载的技能列表"""
    return list(_skills.values())


def get_skills_summary() -> str:
    """
    生成注入 System Prompt 的技能摘要文本
    当没有技能时返回空字符串
    """
    if not _skills:
        return ""

    lines = ["## 可用技能（Skills）", "当用户请求匹配以下技能时，请参考技能步骤执行："]
    for skill in _skills.values():
        lines.append(skill.to_summary())

    lines.append("")
    lines.append("执行技能时，请严格按照技能文件中的步骤调用相应工具。")
    return "\n".join(lines)


def get_skill_detail(name: str) -> str:
    """获取技能的详细内容（含执行步骤）"""
    skill = get_skill(name)
    if not skill:
        return f"未找到技能: {name}"
    return skill.to_full_text()


def find_matching_skill(user_input: str) -> Optional[SkillDefinition]:
    """
    根据用户输入尝试匹配技能。
    快速路径：触发词子串匹配（零模型调用）。
    注意：语义兜底需异步调用，请使用 find_matching_skill_async。
    """
    user_lower = user_input.lower()

    for skill in _skills.values():
        for trigger in skill.triggers:
            if trigger.lower() in user_lower:
                return skill

    return None


async def find_matching_skill_async(user_input: str) -> Optional[SkillDefinition]:
    """
    异步版 Skill 匹配：先触发词子串匹配，失败后用 LLM 语义兜底。
    """
    # 第一层：触发词子串匹配（快速，零模型调用）
    result = find_matching_skill(user_input)
    if result:
        return result

    # 第二层：LLM 语义匹配兜底
    if _skills:
        return await _llm_match_skill_async(user_input)

    return None


async def _llm_match_skill_async(user_input: str) -> Optional[SkillDefinition]:
    """
    用 LLM 判断用户意图是否匹配某个 Skill（异步版）。
    """
    candidates = []
    for skill in _skills.values():
        triggers_str = "、".join(skill.triggers) if skill.triggers else "无"
        candidates.append(f"- {skill.name}: {skill.description}（触发词示例：{triggers_str}）")
    candidates_text = "\n".join(candidates)

    prompt = (
        f"判断用户意图是否匹配以下某个技能。\n\n"
        f"## 候选技能\n{candidates_text}\n\n"
        f"## 用户输入\n{user_input}\n\n"
        f"规则：\n"
        f"1. 如果匹配某个技能，只返回该技能名称（如 send_email）\n"
        f"2. 如果不匹配任何技能，返回：NONE\n"
        f"3. 只返回名称或 NONE，不要其他内容"
    )

    try:
        from llm import get_llm_client
        client = get_llm_client()

        messages = [
            {"role": "system", "content": "你是意图分类器，只返回技能名称或 NONE。"},
            {"role": "user", "content": prompt},
        ]

        response = await client.chat(messages)
        result = (response.content or "").strip()

        if result and result != "NONE" and result in _skills:
            return _skills[result]
    except Exception:
        pass  # LLM 匹配失败时静默，不影响主流程

    return None
