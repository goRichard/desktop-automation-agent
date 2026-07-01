"""
定时任务执行器：触发时创建新 Agent Loop 会话执行对应的 Skill
写入 JobExecutionLog 完整记录执行状态
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

from memory import (
    ExecutionStatus,
    create_session,
    finish_execution_log,
    get_job,
    start_execution_log,
    update_job_run_info,
)

logger = logging.getLogger(__name__)
_task_dispatcher: Optional[Callable[[str], Awaitable[dict[str, Any]]]] = None
_runtime_loop: Optional[asyncio.AbstractEventLoop] = None


def configure_task_dispatcher(
    dispatcher: Optional[Callable[[str], Awaitable[dict[str, Any]]]],
    loop: Optional[asyncio.AbstractEventLoop],
) -> None:
    global _task_dispatcher, _runtime_loop
    _task_dispatcher = dispatcher
    _runtime_loop = loop


def run_automation_task_job(task_id: str) -> None:
    """APScheduler entry point for versioned unattended Tasks."""
    try:
        if _task_dispatcher and _runtime_loop and _runtime_loop.is_running():
            future = asyncio.run_coroutine_threadsafe(
                _task_dispatcher(task_id), _runtime_loop
            )
            future.result(timeout=7 * 86400)
        else:
            asyncio.run(_run_task_standalone(task_id))
    except Exception as error:
        logger.error("Automation Task failed %s: %s", task_id, error, exc_info=True)


async def _run_task_standalone(task_id: str) -> None:
    from runtime.manager import RuntimeManager
    from skills.repository import SkillRepository
    from tasks.repository import TaskRepository
    from tasks.runner import TaskRunner

    manager = RuntimeManager()
    try:
        await TaskRunner(manager, TaskRepository(), SkillRepository()).run_scheduled(task_id)
    finally:
        await manager.shutdown()


def run_skill_job(job_id: str) -> None:
    """
    APScheduler 定时触发此函数执行 Skill。
    在新线程中运行（ThreadPoolExecutor），通过 asyncio.run() 执行异步逻辑。
    """
    try:
        asyncio.run(_run_skill_job_async(job_id))
    except Exception as e:
        logger.error(f"定时任务执行异常 {job_id}: {e}", exc_info=True)


async def _run_skill_job_async(job_id: str) -> None:
    """异步执行定时任务"""
    # 查询任务信息
    db_job = get_job(job_id)
    if db_job is None:
        logger.warning(f"定时任务不存在，跳过执行: {job_id}")
        return

    logger.info(f"开始执行定时任务: {db_job.name} ({job_id})")

    # 创建专属会话
    session = create_session(title=f"[定时任务] {db_job.name}")

    # 记录执行开始
    log = start_execution_log(job_id=job_id, session_id=session.id)

    try:
        # 构建执行指令（将 skill 名称和参数注入为用户消息）
        params = json.loads(db_job.params) if db_job.params else {}
        user_input = _build_skill_prompt(db_job.skill_name, params)

        # 调用 Agent Loop 执行
        from agent import AgentLoop
        loop = AgentLoop(session_id=session.id)
        result = await loop.run(user_input=user_input)

        # 记录成功
        finish_execution_log(
            log_id=log.id,
            status=ExecutionStatus.success,
            result=result[:500] if result else None,
        )
        update_job_run_info(
            job_id=job_id,
            last_run_at=datetime.utcnow(),
            next_run_at=_get_next_run_time(job_id),
            last_result=result[:200] if result else None,
        )
        logger.info(f"定时任务完成: {db_job.name}")

    except Exception as e:
        error_msg = f"{type(e).__name__}: {e}"
        logger.error(f"定时任务失败 {db_job.name}: {error_msg}", exc_info=True)
        finish_execution_log(
            log_id=log.id,
            status=ExecutionStatus.failed,
            error=error_msg,
        )
        update_job_run_info(
            job_id=job_id,
            last_run_at=datetime.utcnow(),
            next_run_at=_get_next_run_time(job_id),
            last_result=f"失败: {error_msg}",
        )


def _build_skill_prompt(skill_name: str, params: dict) -> str:
    """构建触发 Skill 的提示词"""
    if params:
        params_str = "、".join(f"{k}={v}" for k, v in params.items())
        return f"请执行技能 [{skill_name}]，参数：{params_str}"
    return f"请执行技能 [{skill_name}]"


def _get_next_run_time(job_id: str):
    """获取下次执行时间"""
    try:
        from scheduler.engine import get_scheduler
        scheduler = get_scheduler()
        job = scheduler.get_job(job_id)
        if job:
            return job.next_run_time
    except Exception:
        pass
    return None
