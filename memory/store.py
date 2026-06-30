"""
数据库 CRUD 操作封装
提供对 5 张表的完整读写接口
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from sqlmodel import Session as DBSession
from sqlmodel import SQLModel, create_engine, select

from .models import (
    AgentMemory,
    ExecutionStatus,
    JobExecutionLog,
    JobStatus,
    MemoryCategory,
    Message,
    MessageRole,
    ScheduledJob,
    Session,
)

# ──────────────────────────────────────────────────────
# 引擎初始化
# ──────────────────────────────────────────────────────

_engine = None


def init_db(db_path: Path | str = "./data/agent.db") -> None:
    """初始化数据库，建表（首次启动自动创建）"""
    global _engine
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _engine = create_engine(
        f"sqlite:///{db_path}",
        echo=False,
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(_engine)


def get_engine():
    if _engine is None:
        init_db()
    return _engine


# ──────────────────────────────────────────────────────
# Sessions CRUD
# ──────────────────────────────────────────────────────

def create_session(title: Optional[str] = None) -> Session:
    session_obj = Session(title=title)
    with DBSession(get_engine()) as db:
        db.add(session_obj)
        db.commit()
        db.refresh(session_obj)
    return session_obj


def get_session(session_id: str) -> Optional[Session]:
    with DBSession(get_engine()) as db:
        return db.get(Session, session_id)


def list_sessions(limit: int = 20) -> list[Session]:
    with DBSession(get_engine()) as db:
        stmt = select(Session).order_by(Session.created_at.desc()).limit(limit)
        return db.exec(stmt).all()


def update_session_title(session_id: str, title: str) -> None:
    with DBSession(get_engine()) as db:
        s = db.get(Session, session_id)
        if s:
            s.title = title
            s.updated_at = datetime.utcnow()
            db.add(s)
            db.commit()


# ──────────────────────────────────────────────────────
# Messages CRUD
# ──────────────────────────────────────────────────────

def save_message(
    session_id: str,
    role: MessageRole,
    content: Optional[str] = None,
    tool_calls: Optional[list[dict]] = None,
    tool_call_id: Optional[str] = None,
    tool_name: Optional[str] = None,
    token_count: Optional[int] = None,
) -> Message:
    msg = Message(
        session_id=session_id,
        role=role,
        content=content,
        tool_calls=json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        token_count=token_count,
    )
    with DBSession(get_engine()) as db:
        db.add(msg)
        db.commit()
        db.refresh(msg)
    return msg


def get_messages(session_id: str) -> list[Message]:
    with DBSession(get_engine()) as db:
        stmt = (
            select(Message)
            .where(Message.session_id == session_id)
            .order_by(Message.created_at.asc())
        )
        return list(db.exec(stmt).all())


def messages_to_openai_format(messages: list[Message]) -> list[dict[str, Any]]:
    """将数据库消息列表转换为 OpenAI API 格式"""
    result = []
    for msg in messages:
        m: dict[str, Any] = {"role": msg.role.value}
        if msg.role == MessageRole.assistant:
            if msg.tool_calls:
                tc_list = json.loads(msg.tool_calls)
                m["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc.get("arguments", {}), ensure_ascii=False),
                        },
                    }
                    for tc in tc_list
                ]
                if msg.content:
                    m["content"] = msg.content
                else:
                    m["content"] = None
            else:
                m["content"] = msg.content or ""
        elif msg.role == MessageRole.tool:
            m["tool_call_id"] = msg.tool_call_id or ""
            m["name"] = msg.tool_name or ""
            m["content"] = msg.content or ""
        else:
            m["content"] = msg.content or ""
        result.append(m)
    return result


# ──────────────────────────────────────────────────────
# ScheduledJobs CRUD
# ──────────────────────────────────────────────────────

def create_job(
    name: str,
    cron_expr: str,
    skill_name: str,
    params: dict | None = None,
) -> ScheduledJob:
    job = ScheduledJob(
        name=name,
        cron_expr=cron_expr,
        skill_name=skill_name,
        params=json.dumps(params or {}, ensure_ascii=False),
    )
    with DBSession(get_engine()) as db:
        db.add(job)
        db.commit()
        db.refresh(job)
    return job


def get_job(job_id: str) -> Optional[ScheduledJob]:
    with DBSession(get_engine()) as db:
        return db.get(ScheduledJob, job_id)


def list_jobs(include_deleted: bool = False) -> list[ScheduledJob]:
    with DBSession(get_engine()) as db:
        stmt = select(ScheduledJob).order_by(ScheduledJob.created_at.desc())
        if not include_deleted:
            stmt = stmt.where(ScheduledJob.status != JobStatus.deleted)
        return list(db.exec(stmt).all())


def update_job_status(job_id: str, status: JobStatus) -> None:
    with DBSession(get_engine()) as db:
        job = db.get(ScheduledJob, job_id)
        if job:
            job.status = status
            db.add(job)
            db.commit()


def update_job_run_info(
    job_id: str,
    last_run_at: datetime,
    next_run_at: Optional[datetime],
    last_result: Optional[str],
) -> None:
    with DBSession(get_engine()) as db:
        job = db.get(ScheduledJob, job_id)
        if job:
            job.last_run_at = last_run_at
            job.next_run_at = next_run_at
            job.run_count += 1
            if last_result:
                job.last_result = last_result[:500]  # 截断防止过长
            db.add(job)
            db.commit()


# ──────────────────────────────────────────────────────
# JobExecutionLogs CRUD
# ──────────────────────────────────────────────────────

def start_execution_log(job_id: str, session_id: Optional[str] = None) -> JobExecutionLog:
    log = JobExecutionLog(job_id=job_id, session_id=session_id)
    with DBSession(get_engine()) as db:
        db.add(log)
        db.commit()
        db.refresh(log)
    return log


def finish_execution_log(
    log_id: str,
    status: ExecutionStatus,
    result: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    with DBSession(get_engine()) as db:
        log = db.get(JobExecutionLog, log_id)
        if log:
            log.finished_at = datetime.utcnow()
            log.status = status
            log.result = result[:1000] if result else None
            log.error = error[:1000] if error else None
            db.add(log)
            db.commit()


def list_job_logs(job_id: str, limit: int = 10) -> list[JobExecutionLog]:
    with DBSession(get_engine()) as db:
        stmt = (
            select(JobExecutionLog)
            .where(JobExecutionLog.job_id == job_id)
            .order_by(JobExecutionLog.started_at.desc())
            .limit(limit)
        )
        return list(db.exec(stmt).all())


# ──────────────────────────────────────────────────────
# AgentMemory CRUD
# ──────────────────────────────────────────────────────

def set_memory(
    key: str,
    value: str,
    category: MemoryCategory = MemoryCategory.fact,
    source_session_id: Optional[str] = None,
    expires_at: Optional[datetime] = None,
) -> AgentMemory:
    with DBSession(get_engine()) as db:
        existing = db.exec(select(AgentMemory).where(AgentMemory.key == key)).first()
        if existing:
            existing.value = value
            existing.category = category
            existing.updated_at = datetime.utcnow()
            existing.expires_at = expires_at
            db.add(existing)
            db.commit()
            db.refresh(existing)
            return existing
        else:
            mem = AgentMemory(
                key=key,
                value=value,
                category=category,
                source_session_id=source_session_id,
                expires_at=expires_at,
            )
            db.add(mem)
            db.commit()
            db.refresh(mem)
            return mem


def get_memory(key: str) -> Optional[AgentMemory]:
    now = datetime.utcnow()
    with DBSession(get_engine()) as db:
        mem = db.exec(select(AgentMemory).where(AgentMemory.key == key)).first()
        if mem and mem.expires_at and mem.expires_at < now:
            return None
        return mem


def list_memories(category: Optional[MemoryCategory] = None) -> list[AgentMemory]:
    now = datetime.utcnow()
    with DBSession(get_engine()) as db:
        stmt = select(AgentMemory)
        if category:
            stmt = stmt.where(AgentMemory.category == category)
        mems = list(db.exec(stmt).all())
        return [m for m in mems if not m.expires_at or m.expires_at > now]


def delete_memory(key: str) -> bool:
    with DBSession(get_engine()) as db:
        mem = db.exec(select(AgentMemory).where(AgentMemory.key == key)).first()
        if mem:
            db.delete(mem)
            db.commit()
            return True
        return False


def format_memories_for_prompt(memories: list[AgentMemory]) -> str:
    """将记忆列表格式化为注入 System Prompt 的文本"""
    if not memories:
        return ""
    lines = ["## 已知背景信息（跨会话记忆）"]
    for m in memories:
        lines.append(f"- [{m.category.value}] {m.key}: {m.value}")
    return "\n".join(lines)
