"""
CLI 命令入口：基于 Click 的命令行界面
实现 chat（REPL）、jobs、skills、sessions 等子命令
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Optional

import click
from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.history import FileHistory

from . import display


def _bootstrap() -> None:
    """初始化所有模块（数据库、技能、工具、调度器）"""
    from config import get_settings
    from memory import init_db
    from scheduler import start_scheduler
    from skills import load_skills
    import tools  # noqa: F401 触发工具注册
    # 注册调度器工具
    from tools import scheduler_tool  # noqa: F401

    settings = get_settings()

    # 1. 初始化数据库
    init_db(settings.memory_db)

    # 2. 加载 Skills
    skills_count = load_skills(settings.skills_dir)

    # 3. 启动调度器
    job_count = start_scheduler()

    return settings, skills_count, job_count


@click.group(invoke_without_command=True)
@click.option("--config", "-c", default="config.yaml", help="配置文件路径", show_default=True)
@click.pass_context
def cli(ctx, config):
    """Desktop Agent — Windows 桌面自动化 Agent（类 Claude Code 风格）"""
    # 切换工作目录到配置文件所在目录
    config_path = Path(config).resolve()
    os.environ["DESKTOP_AGENT_CONFIG"] = str(config_path)
    if config_path.exists():
        os.chdir(config_path.parent)

    if ctx.invoked_subcommand is None:
        # 默认启动 chat
        ctx.invoke(chat)


@cli.command()
@click.option("--session", "-s", default=None, help="复用指定会话 ID")
def chat(session):
    """启动交互式对话（REPL 模式）"""
    try:
        settings, skills_count, job_count = _bootstrap()
    except Exception as e:
        display.print_error(f"初始化失败: {e}")
        sys.exit(1)

    display.print_welcome(
        profile=settings.active_profile,
        llm_model=settings.llm.get("model", "N/A"),
        vision_model=settings.vision.get("model", "N/A"),
        skills_count=skills_count,
    )

    if job_count > 0:
        display.print_info(f"已恢复 {job_count} 个定时任务")

    # 屏蔽 Windows ProactorEventLoop 关闭时的 transport __del__ 噪音
    # （Python 3.12 + asyncio 子进程已知问题，不影响功能）
    import sys
    _orig_unraisablehook = sys.unraisablehook
    def _suppress_loop_closed(unraisable):
        if isinstance(unraisable.exc_value, RuntimeError) and \
                "Event loop is closed" in str(unraisable.exc_value):
            return
        _orig_unraisablehook(unraisable)
    sys.unraisablehook = _suppress_loop_closed

    try:
        asyncio.run(_chat_loop(session_id=session))
    finally:
        sys.unraisablehook = _orig_unraisablehook


async def _chat_loop(session_id: Optional[str] = None) -> None:
    """异步 REPL 主循环"""
    from agent import AgentLoop

    # 创建或复用 Agent Loop
    loop = AgentLoop(session_id=session_id)
    display.print_info(f"会话 ID: {loop.session_id[:8]}...")

    def _show_usage(event) -> None:
        if event.type == "run.usage":
            display.print_token_usage(
                event.data["cumulative"],
                label="Token 累计",
            )

    unsubscribe_usage = loop.event_bus.subscribe(_show_usage)

    # 历史记录文件
    history_dir = Path("./.agent_history")
    history_dir.mkdir(exist_ok=True)
    prompt_session = PromptSession(
        history=FileHistory(str(history_dir / "history.txt")),
        auto_suggest=AutoSuggestFromHistory(),
    )

    display.print_separator()
    display.print_info("开始对话（输入 /help 查看命令，/exit 退出）")
    display.print_separator()

    while True:
        try:
            user_input = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: prompt_session.prompt("\n> "),
            )
        except (KeyboardInterrupt, EOFError):
            display.print_info("\n退出程序...")
            break

        user_input = user_input.strip()
        if not user_input:
            continue

        # ── 内置命令处理 ──────────────────────────────
        if user_input.startswith("/"):
            handled = await _handle_builtin_command(user_input, loop)
            if handled == "exit":
                break
            continue

        # ── 正常 Agent 对话 ───────────────────────────
        display.print_user_input(user_input)
        display.print_separator()

        # ── Plan-First 拦截：Skill 匹配时生成计划并等待确认 ──
        confirmed_plan: str | None = None
        plan_cancelled = False
        from skills.registry import find_matching_skill_async
        matched_skill = await find_matching_skill_async(user_input)
        if matched_skill:
            display.print_info(f"匹配到 Skill [{matched_skill.name}]，正在生成执行计划...")
            try:
                plan_text = await loop.generate_skill_plan(user_input, matched_skill.content)
                display.print_plan_for_confirmation(matched_skill.name, plan_text)

                # 第一次确认循环（支持修改出新计划再确认）
                for _retry in range(2):
                    answer = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: prompt_session.prompt("\n> "),
                    )
                    answer = answer.strip()

                    if answer.lower() in ("n", "no", "取消", "cancel"):
                        display.print_info("已取消")
                        confirmed_plan = None
                        plan_cancelled = True
                        break

                    if answer == "" or answer.lower() in ("y", "yes", "是", "确认"):
                        confirmed_plan = plan_text
                        display.print_success("计划已确认，开始执行...")
                        break

                    # 用户提供了修改意见，重新生成计划
                    display.print_info("正在根据您的意见重新生成计划...")
                    revised_input = f"{user_input}\n\n用户对计划的调整意见：{answer}"
                    plan_text = await loop.generate_skill_plan(revised_input, matched_skill.content)
                    display.print_plan_for_confirmation(matched_skill.name, plan_text)

                else:
                    # 两轮后仍未确认，默认使用最后一版计划
                    confirmed_plan = plan_text
                    display.print_success("计划已确认，开始执行...")

            except Exception as e:
                display.print_error(f"计划生成失败（{e}），回退到默认模式执行")
                confirmed_plan = None

            # 用户取消了计划，结束本轮
            if plan_cancelled:
                display.print_separator()
                continue

        try:
            display.console.print("\n[agent]Agent:[/agent] ", end="")

            full_response = ""
            async for token in loop.run_stream(
                user_input=user_input,
                on_tool_call=_on_tool_call,
                on_tool_result=_on_tool_result,
                confirmed_plan=confirmed_plan,
            ):
                display.console.print(token, end="", markup=False)
                full_response += token

            display.console.print()  # 换行

        except Exception as e:
            display.print_error(f"执行出错: {e}")
            import traceback
            display.console.print(traceback.format_exc(), style="dim red")

        display.print_separator()

    # 关闭调度器
    unsubscribe_usage()
    from scheduler import shutdown_scheduler
    shutdown_scheduler()

    # 清理所有挂起的异步任务，给 transport 时间正常关闭
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    await asyncio.sleep(0.15)  # 给 ProactorEventLoop transport 时间清理


async def _on_tool_call(name: str, args: dict) -> None:
    display.console.print()  # 换行
    display.print_tool_call(name, args)


async def _on_tool_result(name: str, result: str) -> None:
    display.print_tool_result(name, result)


async def _handle_builtin_command(cmd: str, loop) -> Optional[str]:
    """处理内置斜杠命令，返回 'exit' 表示退出"""
    parts = cmd.split(maxsplit=1)
    command = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    if command in ("/exit", "/quit", "/q"):
        return "exit"

    elif command == "/help":
        display.print_help()

    elif command == "/clear":
        os.system("cls" if os.name == "nt" else "clear")

    elif command == "/new":
        from agent import AgentLoop
        # 重新创建新会话的 AgentLoop
        new_loop = AgentLoop()
        # 更新当前循环的会话 ID
        loop.session_id = new_loop.session_id
        loop._turn_count = 0
        display.print_success(f"已创建新会话: {loop.session_id[:8]}...")

    elif command == "/history":
        from memory import get_messages, messages_to_openai_format
        msgs = get_messages(loop.session_id)
        if not msgs:
            display.print_info("当前会话暂无历史消息")
        else:
            for m in msgs:
                role_color = {"user": "green", "assistant": "cyan", "tool": "yellow"}.get(
                    m.role.value, "white"
                )
                content_preview = (m.content or "")[:100]
                display.console.print(
                    f"[{role_color}]{m.role.value}[/{role_color}]: {content_preview}"
                )

    elif command == "/sessions":
        from memory import list_sessions
        sessions = list_sessions(limit=10)
        display.print_sessions_table(sessions)

    elif command == "/skills":
        from skills import list_skills
        skills = list_skills()
        display.print_skills_table(skills)

    elif command == "/jobs":
        from memory import list_jobs
        jobs = list_jobs()
        display.print_jobs_table(jobs)

    elif command == "/memory":
        from memory import list_memories
        memories = list_memories()
        display.print_memory_table(memories)

    elif command == "/tools":
        from tools import get_all_schemas, list_tools
        display.print_tools_table(list_tools(), get_all_schemas())

    elif command == "/config":
        from config import get_settings
        s = get_settings()
        display.console.print(f"[bold]当前配置：[/bold]\n{s.summary()}")
        display.console.print(f"DB: {s.memory_db} | Skills: {s.skills_dir}")

    else:
        display.print_error(f"未知命令: {command}，输入 /help 查看可用命令")

    return None


# ── 独立子命令 ────────────────────────────────────────

@cli.command()
def jobs():
    """查看和管理定时任务"""
    _bootstrap()
    from memory import list_jobs
    jobs_list = list_jobs()
    display.print_jobs_table(jobs_list)


@cli.command()
def skills():
    """列出所有已加载的技能"""
    _bootstrap()
    from skills import list_skills
    skills_list = list_skills()
    display.print_skills_table(skills_list)


@cli.command()
def sessions():
    """列出最近的会话历史"""
    _bootstrap()
    from memory import list_sessions
    sessions_list = list_sessions(limit=20)
    display.print_sessions_table(sessions_list)
