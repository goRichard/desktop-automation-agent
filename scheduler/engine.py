"""
APScheduler 引擎：初始化调度器，启动时从 SQLite 恢复所有持久化任务
使用 SQLAlchemyJobStore 确保任务跨 session 存活
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from config import get_settings
from memory import JobStatus, list_jobs, store, update_job_status

logger = logging.getLogger(__name__)

_scheduler: Optional[BackgroundScheduler] = None


def get_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = _create_scheduler()
    return _scheduler


def _create_scheduler() -> BackgroundScheduler:
    settings = get_settings()
    db_path = settings.memory_db
    db_path.parent.mkdir(parents=True, exist_ok=True)

    jobstores = {
        "default": SQLAlchemyJobStore(url=f"sqlite:///{db_path}"),
    }
    executors = {
        "default": ThreadPoolExecutor(max_workers=4),
    }
    job_defaults = {
        "coalesce": True,       # 错过的任务只执行一次
        "max_instances": 1,     # 同一任务同时只运行一个实例
        "misfire_grace_time": 60,  # 允许60秒的延迟
    }

    scheduler = BackgroundScheduler(
        jobstores=jobstores,
        executors=executors,
        job_defaults=job_defaults,
    )
    return scheduler


def start_scheduler(task_dispatcher=None, event_loop=None) -> int:
    """
    启动调度器并从数据库恢复已有的 active Job。
    返回恢复的任务数量。
    """
    from .job_runner import (
        configure_task_dispatcher,
        run_automation_task_job,
    )

    if task_dispatcher is not None:
        configure_task_dispatcher(task_dispatcher, event_loop)

    scheduler = get_scheduler()
    if scheduler.running:
        return 0

    scheduler.start()
    logger.info("APScheduler 已启动")

    _disable_legacy_jobs()
    restored = _sync_tasks_from_db(run_automation_task_job)
    logger.info(f"已恢复 {restored} 个定时任务")
    return restored


def _disable_legacy_jobs() -> None:
    """Pause prompt-driven legacy Jobs so they cannot run unattended after upgrade."""
    scheduler = get_scheduler()
    for job in list_jobs():
        if job.status != JobStatus.active:
            continue
        try:
            scheduler.remove_job(job.id)
        except Exception:
            pass
        update_job_status(job.id, JobStatus.paused)
        logger.warning("已暂停旧版提示词定时任务，需迁移为 Task: %s", job.id)


def _sync_tasks_from_db(runner_func) -> int:
    scheduler = get_scheduler()
    count = 0
    existing_job_ids = {job.id for job in scheduler.get_jobs()}
    for task in store.list_task_records():
        scheduler_id = f"task:{task.id}"
        if task.status == "active" and scheduler_id not in existing_job_ids:
            try:
                document = json.loads(task.document)
                misfire_policy = document.get("schedule", {}).get(
                    "misfirePolicy", "run_once"
                )
                _add_task_job(
                    task.id,
                    task.cron_expr,
                    task.timezone,
                    runner_func,
                    misfire_policy,
                )
                count += 1
            except Exception as error:
                logger.error("恢复 Task 失败 %s: %s", task.id, error)
    return count


def _sync_jobs_from_db(runner_func) -> int:
    """
    将业务表中 active 状态的任务同步到 APScheduler
    （处理 APScheduler jobstore 中不存在但业务表中存在的任务）
    """
    scheduler = get_scheduler()
    count = 0

    active_jobs = [j for j in list_jobs() if j.status == JobStatus.active]
    existing_job_ids = {job.id for job in scheduler.get_jobs()}

    for db_job in active_jobs:
        if db_job.id not in existing_job_ids:
            try:
                scheduler.add_job(
                    runner_func,
                    "cron",
                    id=db_job.id,
                    args=[db_job.id],
                    **_parse_cron(db_job.cron_expr),
                    replace_existing=True,
                )
                count += 1
                logger.info(f"恢复任务: {db_job.name} ({db_job.cron_expr})")
            except Exception as e:
                logger.error(f"恢复任务失败 {db_job.name}: {e}")

    return count


def add_job(job_id: str, cron_expr: str) -> None:
    """向调度器注册新任务"""
    from .job_runner import run_skill_job

    scheduler = get_scheduler()
    scheduler.add_job(
        run_skill_job,
        "cron",
        id=job_id,
        args=[job_id],
        **_parse_cron(cron_expr),
        replace_existing=True,
    )
    logger.info(f"已注册任务 {job_id}: {cron_expr}")


def add_task(
    task_id: str,
    cron_expr: str,
    timezone: str,
    misfire_policy: str = "run_once",
) -> None:
    from .job_runner import run_automation_task_job

    _add_task_job(
        task_id, cron_expr, timezone, run_automation_task_job, misfire_policy
    )


def _add_task_job(
    task_id: str,
    cron_expr: str,
    timezone: str,
    runner_func,
    misfire_policy: str = "run_once",
) -> None:
    trigger = CronTrigger.from_crontab(cron_expr, timezone=timezone)
    scheduled = get_scheduler().add_job(
        runner_func,
        trigger=trigger,
        id=f"task:{task_id}",
        args=[task_id],
        replace_existing=True,
        coalesce=misfire_policy == "run_once",
        misfire_grace_time=60 if misfire_policy == "run_once" else 1,
    )
    store.set_task_next_run(task_id, scheduled.next_run_time)
    logger.info("已注册 Task %s: %s (%s)", task_id, cron_expr, timezone)


def remove_task(task_id: str) -> None:
    _safe_task_control("remove", task_id)


def pause_task(task_id: str) -> None:
    _safe_task_control("pause", task_id)


def resume_task(task_id: str) -> None:
    _safe_task_control("resume", task_id)


def _safe_task_control(action: str, task_id: str) -> None:
    scheduler = get_scheduler()
    method = getattr(scheduler, f"{action}_job")
    try:
        method(f"task:{task_id}")
        scheduled = scheduler.get_job(f"task:{task_id}")
        store.set_task_next_run(
            task_id,
            scheduled.next_run_time if scheduled else None,
        )
    except Exception:
        pass


def remove_job(job_id: str) -> None:
    """从调度器移除任务"""
    scheduler = get_scheduler()
    try:
        scheduler.remove_job(job_id)
        logger.info(f"已移除任务 {job_id}")
    except Exception:
        pass


def pause_job(job_id: str) -> None:
    scheduler = get_scheduler()
    try:
        scheduler.pause_job(job_id)
    except Exception:
        pass


def resume_job(job_id: str) -> None:
    scheduler = get_scheduler()
    try:
        scheduler.resume_job(job_id)
    except Exception:
        pass


def _parse_cron(cron_expr: str) -> dict:
    """
    解析 cron 表达式（5 字段：分 时 日 月 周）
    返回 APScheduler CronTrigger 的参数字典
    """
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        raise ValueError(f"无效的 cron 表达式（需要5个字段）: {cron_expr}")

    minute, hour, day, month, day_of_week = parts
    return {
        "minute": minute,
        "hour": hour,
        "day": day,
        "month": month,
        "day_of_week": day_of_week,
    }


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("APScheduler 已关闭")
    _scheduler = None
    from .job_runner import configure_task_dispatcher

    configure_task_dispatcher(None, None)
