"""
定时任务管理工具：供 Agent 调用，实现 Cron Job 的创建/查询/删除/启停
LLM 直接生成 cron 表达式传入 create_job，无需独立的 nl2cron 转换
"""
from __future__ import annotations

import json
from typing import Optional

from memory import (
    JobStatus,
    create_job as db_create_job,
    get_job,
    list_jobs,
    update_job_status,
)
from scheduler.engine import add_job, pause_job, remove_job, resume_job
from tools.registry import tool


@tool(
    description=(
        "创建一个新的定时任务。"
        "cron 参数为标准 5 字段 cron 表达式（分 时 日 月 周），例如：'0 9 * * *' 表示每天9点，'*/5 * * * *' 表示每5分钟。"
        "skill_name 为要执行的技能名称（与 SKILL.md 的 name 字段对应）。"
        "params 为传递给技能的额外参数（JSON 对象）。"
        "name 为任务的人类可读描述。"
        "返回创建的任务 ID。"
    )
)
def create_job(
    cron: str,
    skill_name: str,
    name: str,
    params: Optional[str] = None,
) -> str:
    """
    创建并注册定时任务。
    cron 表达式由 LLM 根据用户自然语言描述直接生成。
    """
    try:
        params_dict = json.loads(params) if params else {}
    except json.JSONDecodeError:
        params_dict = {}

    # 1. 写入业务表
    job = db_create_job(
        name=name,
        cron_expr=cron,
        skill_name=skill_name,
        params=params_dict,
    )

    # 2. 注册到 APScheduler（SQLite 持久化）
    try:
        add_job(job_id=job.id, cron_expr=cron)
    except Exception as e:
        return f"任务已创建（ID: {job.id}），但调度注册失败: {e}。请检查 cron 表达式格式。"

    return (
        f"✅ 定时任务创建成功！\n"
        f"- ID: {job.id}\n"
        f"- 名称: {name}\n"
        f"- Cron: {cron}\n"
        f"- 技能: {skill_name}\n"
        f"- 状态: 运行中（进程重启后自动恢复）"
    )


@tool(description="列出所有定时任务（不含已删除的任务）。返回任务列表，包括 ID、名称、cron 表达式、状态、上次执行时间等信息。")
def list_scheduled_jobs() -> str:
    jobs = list_jobs(include_deleted=False)
    if not jobs:
        return "当前没有定时任务。"

    lines = [f"共 {len(jobs)} 个定时任务：\n"]
    for job in jobs:
        status_icon = {"active": "▶️", "paused": "⏸️", "deleted": "🗑️"}.get(job.status.value, "?")
        last_run = job.last_run_at.strftime("%Y-%m-%d %H:%M") if job.last_run_at else "从未执行"
        lines.append(
            f"{status_icon} [{job.id[:8]}] {job.name}\n"
            f"   Cron: {job.cron_expr} | 技能: {job.skill_name} | 上次执行: {last_run}\n"
        )
    return "\n".join(lines)


@tool(description="删除指定的定时任务。job_id 为任务 ID（可通过 list_scheduled_jobs 获取）。删除后任务将从调度器中移除，不可恢复。")
def delete_job(job_id: str) -> str:
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


@tool(description="暂停或恢复定时任务。job_id 为任务 ID，enabled=true 启用任务，enabled=false 暂停任务。暂停后任务不会执行，但保留在系统中。")
def toggle_job(job_id: str, enabled: bool) -> str:
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
