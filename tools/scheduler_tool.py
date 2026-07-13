"""
定时任务管理工具：供 Agent 调用，实现 Cron Job 的创建/查询/删除/启停
LLM 直接生成 cron 表达式传入 create_job，无需独立的 nl2cron 转换
"""
from __future__ import annotations

import json
import uuid
from typing import Optional

from pydantic import ValidationError

from memory import (
    JobStatus,
    get_job,
    list_jobs,
    update_job_status,
)
from scheduler.engine import (
    add_task,
    pause_job,
    pause_task,
    remove_job,
    remove_task,
    resume_job,
)
from skills.repository import SkillRepository
from skills.schema import SkillStatus
from tasks import (
    TaskConflictError,
    TaskDocument,
    TaskRepository,
    TaskStatus,
    TaskValidationError,
    validate_task_document,
)
from tools.registry import tool


@tool(
    description=(
        "创建一个新的定时任务。"
        "cron 参数为标准 5 字段 cron 表达式（分 时 日 月 周），例如：'0 9 * * *' 表示每天9点，'*/5 * * * *' 表示每5分钟。"
        "skill_name 为要执行的 Skill ID。"
        "skill_version 为固定 published 版本；省略时使用当前 published 版本。"
        "params 为传递给技能的额外参数（JSON 对象）。"
        "timezone 为 IANA 时区；涉及发送等外部副作用时必须明确设置 external_side_effects_approved=true。"
        "name 为任务的人类可读描述。"
        "返回创建的任务 ID。"
    ),
    risk="high",
    side_effect=True,
    allowed_modes=["agent", "step", "guided"],
)
def create_job(
    cron: str,
    skill_name: str,
    name: str,
    params: Optional[str] = None,
    skill_version: Optional[str] = None,
    timezone: str = "Asia/Shanghai",
    external_side_effects_approved: bool = False,
) -> str:
    """
    创建并注册定时任务。
    cron 表达式由 LLM 根据用户自然语言描述直接生成。
    """
    try:
        params_dict = json.loads(params) if params else {}
    except json.JSONDecodeError:
        params_dict = {}

    skills = SkillRepository()
    try:
        if skill_version is None:
            skill_summary = skills.get(skill_name)
            skill_version = skill_summary.get("publishedVersion")
        if not skill_version:
            return f"创建失败：Skill 没有 published 版本: {skill_name}"
        skill_value = skills.get_version(skill_name, skill_version)
        if skill_value["status"] != SkillStatus.PUBLISHED.value:
            return f"创建失败：Skill 版本未发布: {skill_name}@{skill_version}"
    except LookupError as error:
        return f"创建失败：{error}"

    task_id = f"task-{uuid.uuid4().hex[:12]}"
    try:
        document = TaskDocument.model_validate(
            {
                "metadata": {"id": task_id, "name": name},
                "schedule": {"cron": cron, "timezone": timezone},
                "skill": {"id": skill_name, "version": skill_version},
                "parameters": params_dict,
                "permissions": {
                    "externalSideEffectsApproved": external_side_effects_approved
                },
            }
        )
        validate_task_document(document, skills)
        TaskRepository().create(document)
    except (ValidationError, TaskConflictError, TaskValidationError) as error:
        return f"创建失败：{error}"

    try:
        add_task(task_id, cron, timezone)
    except Exception as e:
        return f"任务已创建（ID: {task_id}），但调度注册失败: {e}。"

    return (
        f"✅ 定时任务创建成功！\n"
        f"- ID: {task_id}\n"
        f"- 名称: {name}\n"
        f"- Cron: {cron}\n"
        f"- 技能: {skill_name}@{skill_version}\n"
        f"- 状态: 运行中（进程重启后自动恢复）"
    )


@tool(
    description="列出所有定时任务（不含已删除的任务）。返回任务列表，包括 ID、名称、cron 表达式、状态、上次执行时间等信息。",
    risk="read",
)
def list_scheduled_jobs() -> str:
    tasks = TaskRepository().list()
    jobs = list_jobs(include_deleted=False)
    if not tasks and not jobs:
        return "当前没有定时任务。"

    lines = [f"共 {len(tasks) + len(jobs)} 个定时任务：\n"]
    for task in tasks:
        status_icon = {"active": "▶️", "paused": "⏸️", "deleted": "🗑️"}.get(
            task["status"], "?"
        )
        lines.append(
            f"{status_icon} [{task['id']}] {task['name']}\n"
            f"   Cron: {task['cron']} | 技能: {task['skillId']}@{task['skillVersion']}"
            f" | 上次结果: {task['lastResult'] or '从未执行'}\n"
        )
    for job in jobs:
        status_icon = {"active": "▶️", "paused": "⏸️", "deleted": "🗑️"}.get(job.status.value, "?")
        last_run = job.last_run_at.strftime("%Y-%m-%d %H:%M") if job.last_run_at else "从未执行"
        lines.append(
            f"{status_icon} [{job.id[:8]}] {job.name}\n"
            f"   Cron: {job.cron_expr} | 技能: {job.skill_name} | 上次执行: {last_run}\n"
        )
    return "\n".join(lines)


@tool(
    description="删除指定的定时任务。job_id 为任务 ID（可通过 list_scheduled_jobs 获取）。删除后任务将从调度器中移除，不可恢复。",
    risk="high",
    side_effect=True,
    allowed_modes=["agent", "step", "guided"],
)
def delete_job(job_id: str) -> str:
    task_repo = TaskRepository()
    try:
        task = task_repo.get(job_id)
        remove_task(task["id"])
        task_repo.set_status(task["id"], TaskStatus.DELETED)
        return f"✅ 已删除定时任务: {task['name']}（ID: {task['id']}）"
    except (LookupError, TaskConflictError):
        pass
    job = get_job(job_id)
    if not job:
        # 尝试短 ID 匹配
        all_jobs = list_jobs()
        matched = [j for j in all_jobs if j.id.startswith(job_id)]
        if len(matched) == 1:
            job = matched[0]
        elif len(matched) > 1:
            return f"找到多个匹配的任务，请提供更长的 ID: {[j.id for j in matched]}"
        else:
            return f"未找到任务: {job_id}"

    # 从调度器移除
    remove_job(job.id)
    # 更新业务表状态
    update_job_status(job.id, JobStatus.deleted)

    return f"✅ 已删除定时任务: {job.name}（ID: {job.id}）"


@tool(
    description="暂停或恢复定时任务。job_id 为任务 ID，enabled=true 启用任务，enabled=false 暂停任务。暂停后任务不会执行，但保留在系统中。",
    risk="medium",
    side_effect=True,
    requires_confirmation=True,
    allowed_modes=["agent", "step", "guided"],
)
def toggle_job(job_id: str, enabled: bool) -> str:
    task_repo = TaskRepository()
    try:
        task = task_repo.get(job_id)
        if enabled:
            document = TaskDocument.model_validate(task["document"])
            validate_task_document(document, SkillRepository())
            task_repo.set_status(task["id"], TaskStatus.ACTIVE)
            add_task(
                task["id"],
                document.schedule.cron,
                document.schedule.timezone,
                document.schedule.misfire_policy,
            )
            return f"✅ 已启用任务: {task['name']}"
        pause_task(task["id"])
        task_repo.set_status(task["id"], TaskStatus.PAUSED)
        return f"⏸️ 已暂停任务: {task['name']}"
    except (TaskConflictError, TaskValidationError) as error:
        return f"操作失败：{error}"
    except LookupError:
        pass
    job = get_job(job_id)
    if not job:
        all_jobs = list_jobs()
        matched = [j for j in all_jobs if j.id.startswith(job_id)]
        if len(matched) == 1:
            job = matched[0]
        else:
            return f"未找到任务: {job_id}"

    if enabled:
        resume_job(job.id)
        update_job_status(job.id, JobStatus.active)
        return f"✅ 已启用任务: {job.name}"
    else:
        pause_job(job.id)
        update_job_status(job.id, JobStatus.paused)
        return f"⏸️ 已暂停任务: {job.name}"
