"""
数据库 CRUD 操作封装
提供对 5 张表的完整读写接口
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

from sqlmodel import Session as DBSession
from sqlmodel import SQLModel, create_engine, select

from .models import (
    AgentMemory,
    AutomationTaskRecord,
    ExecutionStatus,
    JobExecutionLog,
    JobStatus,
    MemoryCategory,
    Message,
    MessageRole,
    RuntimeEventRecord,
    RuntimeEvidenceRecord,
    RuntimeRun,
    RuntimeRunContext,
    RuntimeStepRun,
    ScheduledJob,
    SchemaMigration,
    Session,
    SkillRecord,
    SkillVersionRecord,
    TaskExecutionRecord,
)

# ──────────────────────────────────────────────────────
# 引擎初始化
# ──────────────────────────────────────────────────────

_engine = None


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


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
    _record_schema_version()


def get_engine():
    if _engine is None:
        init_db()
    return _engine


_SCHEMA_VERSION = 5


def _record_schema_version() -> None:
    """记录当前 create-all Schema 版本，为后续增量迁移提供基线。"""
    with DBSession(_engine) as db:
        current = db.get(SchemaMigration, _SCHEMA_VERSION)
        if current is None:
            db.add(SchemaMigration(version=_SCHEMA_VERSION))
            db.commit()


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
            s.updated_at = _utc_now()
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
            log.finished_at = _utc_now()
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
            existing.updated_at = _utc_now()
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
    now = _utc_now()
    with DBSession(get_engine()) as db:
        mem = db.exec(select(AgentMemory).where(AgentMemory.key == key)).first()
        if mem and mem.expires_at and mem.expires_at < now:
            return None
        return mem


def list_memories(category: Optional[MemoryCategory] = None) -> list[AgentMemory]:
    now = _utc_now()
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


# ──────────────────────────────────────────────────────
# Runtime Run / Step / Event CRUD
# ──────────────────────────────────────────────────────

def upsert_runtime_run(value: dict[str, Any]) -> RuntimeRun:
    with DBSession(get_engine()) as db:
        record = db.get(RuntimeRun, value["id"]) or RuntimeRun(
            id=value["id"],
            session_id=value["session_id"],
            user_input=value["user_input"],
            status=value["status"],
            created_at=value["created_at"],
        )
        for field_name in (
            "session_id", "user_input", "status", "created_at",
            "started_at", "finished_at", "error", "output",
        ):
            setattr(record, field_name, value.get(field_name))
        db.add(record)
        context = db.get(RuntimeRunContext, value["id"]) or RuntimeRunContext(
            run_id=value["id"]
        )
        context.run_type = value.get("run_type", "agent")
        context.skill_id = value.get("skill_id")
        context.skill_version = value.get("skill_version")
        context.execution_mode = value.get("execution_mode")
        context.inputs = json.dumps(value.get("inputs", {}), ensure_ascii=False)
        db.add(context)
        db.commit()
        db.refresh(record)
        return record


def upsert_runtime_step(value: dict[str, Any]) -> RuntimeStepRun:
    with DBSession(get_engine()) as db:
        record = db.get(RuntimeStepRun, value["id"]) or RuntimeStepRun(
            id=value["id"],
            run_id=value["run_id"],
            name=value["name"],
            status=value["status"],
        )
        record.run_id = value["run_id"]
        record.name = value["name"]
        record.tool_names = json.dumps(value.get("tool_names", []), ensure_ascii=False)
        record.status = value["status"]
        record.started_at = value.get("started_at")
        record.finished_at = value.get("finished_at")
        record.result = value.get("result")
        record.error = value.get("error")
        db.add(record)
        db.commit()
        db.refresh(record)
        return record


def save_runtime_event(value: dict[str, Any]) -> RuntimeEventRecord:
    record = RuntimeEventRecord(
        id=value["id"],
        run_id=value["run_id"],
        sequence=value["sequence"],
        type=value["type"],
        data=json.dumps(value.get("data", {}), ensure_ascii=False, default=str),
        timestamp=value["timestamp"],
    )
    with DBSession(get_engine()) as db:
        existing = db.get(RuntimeEventRecord, record.id)
        if existing is None:
            db.add(record)
            db.commit()
            db.refresh(record)
            return record
        return existing


def get_runtime_run(run_id: str) -> Optional[RuntimeRun]:
    with DBSession(get_engine()) as db:
        return db.get(RuntimeRun, run_id)


def get_runtime_run_context(run_id: str) -> Optional[RuntimeRunContext]:
    with DBSession(get_engine()) as db:
        return db.get(RuntimeRunContext, run_id)


def list_runtime_run_contexts(run_ids: list[str]) -> list[RuntimeRunContext]:
    if not run_ids:
        return []
    with DBSession(get_engine()) as db:
        statement = select(RuntimeRunContext).where(RuntimeRunContext.run_id.in_(run_ids))
        return list(db.exec(statement).all())


def list_runtime_runs(limit: int = 50) -> list[RuntimeRun]:
    with DBSession(get_engine()) as db:
        statement = select(RuntimeRun).order_by(RuntimeRun.created_at.desc()).limit(limit)
        return list(db.exec(statement).all())


def list_runtime_steps(run_id: str) -> list[RuntimeStepRun]:
    with DBSession(get_engine()) as db:
        statement = (
            select(RuntimeStepRun)
            .where(RuntimeStepRun.run_id == run_id)
            .order_by(RuntimeStepRun.started_at.asc())
        )
        return list(db.exec(statement).all())


def list_runtime_events(
    run_id: str,
    after_sequence: int = 0,
) -> list[RuntimeEventRecord]:
    with DBSession(get_engine()) as db:
        statement = (
            select(RuntimeEventRecord)
            .where(RuntimeEventRecord.run_id == run_id)
            .where(RuntimeEventRecord.sequence > after_sequence)
            .order_by(RuntimeEventRecord.sequence.asc())
        )
        return list(db.exec(statement).all())


def save_runtime_evidence(value: dict[str, Any]) -> RuntimeEvidenceRecord:
    record = RuntimeEvidenceRecord(
        id=value["id"],
        run_id=value["run_id"],
        step_id=value["step_id"],
        kind=value.get("kind", "failure"),
        path=value["path"],
        metadata_json=json.dumps(value.get("metadata", {}), ensure_ascii=False, default=str),
        created_at=value["created_at"],
    )
    with DBSession(get_engine()) as db:
        db.add(record)
        db.commit()
        db.refresh(record)
        return record


def list_runtime_evidence(run_id: str) -> list[RuntimeEvidenceRecord]:
    with DBSession(get_engine()) as db:
        statement = (
            select(RuntimeEvidenceRecord)
            .where(RuntimeEvidenceRecord.run_id == run_id)
            .order_by(RuntimeEvidenceRecord.created_at.asc())
        )
        return list(db.exec(statement).all())


# ──────────────────────────────────────────────────────
# Versioned Skills CRUD
# ──────────────────────────────────────────────────────

def create_skill_version(
    *,
    skill_id: str,
    name: str,
    description: str,
    version: str,
    status: str,
    document: dict[str, Any],
    source_format: str = "yaml",
) -> SkillVersionRecord:
    with DBSession(get_engine()) as db:
        duplicate = db.exec(
            select(SkillVersionRecord)
            .where(SkillVersionRecord.skill_id == skill_id)
            .where(SkillVersionRecord.version == version)
        ).first()
        if duplicate:
            raise ValueError(f"Skill version already exists: {skill_id}@{version}")

        now = _utc_now()
        skill = db.get(SkillRecord, skill_id)
        if skill is None:
            skill = SkillRecord(
                id=skill_id,
                name=name,
                description=description,
                latest_version=version,
            )
        else:
            skill.name = name
            skill.description = description
            skill.latest_version = version
            skill.updated_at = now
        record = SkillVersionRecord(
            skill_id=skill_id,
            version=version,
            status=status,
            document=json.dumps(document, ensure_ascii=False),
            source_format=source_format,
            created_at=now,
            updated_at=now,
        )
        db.add(skill)
        db.add(record)
        db.commit()
        db.refresh(record)
        return record


def update_skill_version(
    skill_id: str,
    version: str,
    *,
    status: Optional[str] = None,
    document: Optional[dict[str, Any]] = None,
    name: Optional[str] = None,
    description: Optional[str] = None,
    validated: bool = False,
    published: bool = False,
    deprecated: bool = False,
) -> Optional[SkillVersionRecord]:
    with DBSession(get_engine()) as db:
        record = db.exec(
            select(SkillVersionRecord)
            .where(SkillVersionRecord.skill_id == skill_id)
            .where(SkillVersionRecord.version == version)
        ).first()
        if record is None:
            return None
        now = _utc_now()
        if status is not None:
            record.status = status
        if document is not None:
            record.document = json.dumps(document, ensure_ascii=False)
        if name is not None or description is not None:
            skill = db.get(SkillRecord, skill_id)
            if skill:
                if name is not None:
                    skill.name = name
                if description is not None:
                    skill.description = description
                skill.updated_at = now
                db.add(skill)
        if validated:
            record.validated_at = now
        if published:
            record.published_at = now
            skill = db.get(SkillRecord, skill_id)
            if skill:
                skill.published_version = version
                skill.updated_at = now
                db.add(skill)
        if deprecated:
            skill = db.get(SkillRecord, skill_id)
            if skill and skill.published_version == version:
                skill.published_version = None
                skill.updated_at = now
                db.add(skill)
        record.updated_at = now
        db.add(record)
        db.commit()
        db.refresh(record)
        return record


def get_skill_record(skill_id: str) -> Optional[SkillRecord]:
    with DBSession(get_engine()) as db:
        return db.get(SkillRecord, skill_id)


def list_skill_records() -> list[SkillRecord]:
    with DBSession(get_engine()) as db:
        return list(db.exec(select(SkillRecord).order_by(SkillRecord.name.asc())).all())


def get_skill_version(skill_id: str, version: str) -> Optional[SkillVersionRecord]:
    with DBSession(get_engine()) as db:
        return db.exec(
            select(SkillVersionRecord)
            .where(SkillVersionRecord.skill_id == skill_id)
            .where(SkillVersionRecord.version == version)
        ).first()


def list_skill_versions(skill_id: str) -> list[SkillVersionRecord]:
    with DBSession(get_engine()) as db:
        statement = (
            select(SkillVersionRecord)
            .where(SkillVersionRecord.skill_id == skill_id)
            .order_by(SkillVersionRecord.created_at.desc())
        )
        return list(db.exec(statement).all())


# ──────────────────────────────────────────────────────
# Versioned Automation Tasks
# ──────────────────────────────────────────────────────

def create_task_record(document: dict[str, Any]) -> AutomationTaskRecord:
    metadata = document["metadata"]
    schedule = document["schedule"]
    skill = document["skill"]
    record = AutomationTaskRecord(
        id=metadata["id"],
        name=metadata["name"],
        document=json.dumps(document, ensure_ascii=False),
        cron_expr=schedule["cron"],
        timezone=schedule["timezone"],
        skill_id=skill["id"],
        skill_version=skill["version"],
    )
    with DBSession(get_engine()) as db:
        if db.get(AutomationTaskRecord, record.id):
            raise ValueError(f"Task already exists: {record.id}")
        db.add(record)
        db.commit()
        db.refresh(record)
        return record


def update_task_record(task_id: str, document: dict[str, Any]) -> Optional[AutomationTaskRecord]:
    with DBSession(get_engine()) as db:
        record = db.get(AutomationTaskRecord, task_id)
        if record is None:
            return None
        metadata = document["metadata"]
        schedule = document["schedule"]
        skill = document["skill"]
        record.name = metadata["name"]
        record.document = json.dumps(document, ensure_ascii=False)
        record.cron_expr = schedule["cron"]
        record.timezone = schedule["timezone"]
        record.skill_id = skill["id"]
        record.skill_version = skill["version"]
        record.updated_at = _utc_now()
        db.add(record)
        db.commit()
        db.refresh(record)
        return record


def get_task_record(task_id: str) -> Optional[AutomationTaskRecord]:
    with DBSession(get_engine()) as db:
        return db.get(AutomationTaskRecord, task_id)


def list_task_records(include_deleted: bool = False) -> list[AutomationTaskRecord]:
    with DBSession(get_engine()) as db:
        statement = select(AutomationTaskRecord).order_by(AutomationTaskRecord.created_at.desc())
        if not include_deleted:
            statement = statement.where(AutomationTaskRecord.status != "deleted")
        return list(db.exec(statement).all())


def set_task_status(task_id: str, task_status: str) -> Optional[AutomationTaskRecord]:
    with DBSession(get_engine()) as db:
        record = db.get(AutomationTaskRecord, task_id)
        if record is None:
            return None
        record.status = task_status
        record.updated_at = _utc_now()
        db.add(record)
        db.commit()
        db.refresh(record)
        return record


def update_task_run_info(
    task_id: str,
    *,
    result: str,
    next_run_at: Optional[datetime],
) -> None:
    with DBSession(get_engine()) as db:
        record = db.get(AutomationTaskRecord, task_id)
        if record:
            record.last_run_at = _utc_now()
            record.next_run_at = next_run_at
            record.run_count += 1
            record.last_result = result[:500]
            record.updated_at = _utc_now()
            db.add(record)
            db.commit()


def set_task_next_run(task_id: str, next_run_at: Optional[datetime]) -> None:
    with DBSession(get_engine()) as db:
        record = db.get(AutomationTaskRecord, task_id)
        if record:
            record.next_run_at = next_run_at
            record.updated_at = _utc_now()
            db.add(record)
            db.commit()


def start_task_execution(
    task_id: str,
    attempt: int,
    run_id: Optional[str] = None,
) -> TaskExecutionRecord:
    record = TaskExecutionRecord(task_id=task_id, attempt=attempt, run_id=run_id)
    with DBSession(get_engine()) as db:
        db.add(record)
        db.commit()
        db.refresh(record)
        return record


def attach_task_execution_run(execution_id: str, run_id: str) -> None:
    with DBSession(get_engine()) as db:
        record = db.get(TaskExecutionRecord, execution_id)
        if record:
            record.run_id = run_id
            db.add(record)
            db.commit()


def finish_task_execution(
    execution_id: str,
    execution_status: str,
    *,
    result: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    with DBSession(get_engine()) as db:
        record = db.get(TaskExecutionRecord, execution_id)
        if record:
            record.status = execution_status
            record.finished_at = _utc_now()
            record.result = result[:1000] if result else None
            record.error = error[:1000] if error else None
            db.add(record)
            db.commit()


def list_task_executions(task_id: str, limit: int = 50) -> list[TaskExecutionRecord]:
    with DBSession(get_engine()) as db:
        statement = (
            select(TaskExecutionRecord)
            .where(TaskExecutionRecord.task_id == task_id)
            .order_by(TaskExecutionRecord.started_at.desc())
            .limit(limit)
        )
        return list(db.exec(statement).all())
