"""
Rich 渲染组件：终端 UI 展示
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

# 全局控制台，统一主题
THEME = Theme({
    "agent": "bold cyan",
    "user": "bold green",
    "tool": "bold yellow",
    "tool_result": "dim white",
    "error": "bold red",
    "success": "bold green",
    "info": "dim cyan",
    "separator": "dim blue",
})

console = Console(theme=THEME, highlight=False)


def print_welcome(profile: str, llm_model: str, vision_model: str, skills_count: int) -> None:
    """启动欢迎界面"""
    table = Table.grid(padding=(0, 1))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("🤖 Desktop Agent", "Windows 桌面自动化 Agent")
    table.add_row("  Profile", f"[bold]{profile}[/bold]")
    table.add_row("  LLM 模型", f"[cyan]{llm_model}[/cyan]")
    table.add_row("  视觉模型", f"[cyan]{vision_model}[/cyan]")
    table.add_row("  技能数量", f"[green]{skills_count}[/green] 个")
    table.add_row("  帮助", "输入 [bold]/help[/bold] 查看内置命令")

    console.print(Panel(table, border_style="cyan", padding=(1, 2)))


def print_user_input(text: str) -> None:
    """打印用户输入（带格式）"""
    console.print(f"\n[user]You:[/user] {text}")


def print_tool_call(name: str, args: dict[str, Any]) -> None:
    """打印工具调用提示"""
    def _fmt(v: Any) -> str:
        s = str(v)
        if len(s) > 50:
            s = s[:50] + "..."
        # 字符串值用单引号包裹，但不转义反斜杠（路径更可读）
        if isinstance(v, str):
            return f"'{s}'"
        return s
    args_str = ", ".join(f"{k}={_fmt(v)}" for k, v in args.items())
    console.print(f"  [tool]⚙  {name}[/tool]([tool_result]{args_str}[/tool_result])")


def print_tool_result(name: str, result: str) -> None:
    """打印工具返回结果（折叠显示）"""
    # 错误信息显示更多（500字符），普通结果200字符
    is_error = "失败" in result or "error" in result.lower() or "traceback" in result.lower()
    max_len = 500 if is_error else 200
    preview = result[:max_len] + "..." if len(result) > max_len else result
    console.print(f"  [tool_result]  └─ {name}: {preview}[/tool_result]")


def print_agent_response(content: str) -> None:
    """以 Markdown 格式打印 Agent 回复"""
    console.print()
    console.print("[agent]Agent:[/agent]")
    try:
        console.print(Markdown(content))
    except Exception:
        console.print(content)


def print_error(msg: str) -> None:
    console.print(f"[error]✗ {msg}[/error]")


def print_info(msg: str) -> None:
    console.print(f"[info]ℹ {msg}[/info]")


def print_success(msg: str) -> None:
    console.print(f"[success]✓ {msg}[/success]")


def print_separator() -> None:
    console.print("[separator]─[/separator]" * 60)


def print_help() -> None:
    """打印内置命令帮助"""
    table = Table(title="内置命令", border_style="blue", show_header=True)
    table.add_column("命令", style="bold cyan", no_wrap=True)
    table.add_column("说明")

    commands = [
        ("/help", "显示此帮助信息"),
        ("/history", "查看当前会话的对话历史"),
        ("/sessions", "列出最近的会话"),
        ("/new", "开始新会话"),
        ("/skills", "列出所有已加载的技能"),
        ("/jobs", "列出所有定时任务"),
        ("/memory", "查看跨会话记忆"),
        ("/tools", "列出所有可用工具"),
        ("/config", "显示当前配置信息"),
        ("/clear", "清空终端屏幕"),
        ("/exit 或 /quit", "退出程序"),
    ]
    for cmd, desc in commands:
        table.add_row(cmd, desc)

    console.print(table)


def print_sessions_table(sessions: list) -> None:
    """打印会话列表"""
    if not sessions:
        print_info("暂无历史会话")
        return

    table = Table(title="历史会话", border_style="blue")
    table.add_column("ID", style="dim", no_wrap=True)
    table.add_column("标题")
    table.add_column("创建时间")

    for s in sessions:
        table.add_row(
            s.id[:8],
            s.title or "（无标题）",
            s.created_at.strftime("%m-%d %H:%M"),
        )
    console.print(table)


def print_skills_table(skills: list) -> None:
    """打印技能列表"""
    if not skills:
        print_info("暂无已加载的技能，请在 skills/user_skills/ 目录中添加 SKILL.md 文件")
        return

    table = Table(title="已加载技能", border_style="green")
    table.add_column("名称", style="bold")
    table.add_column("描述")
    table.add_column("触发词", style="dim")

    for s in skills:
        triggers = "、".join(s.triggers[:3]) if s.triggers else "-"
        table.add_row(s.name, s.description[:50], triggers)
    console.print(table)


def print_jobs_table(jobs: list) -> None:
    """打印定时任务列表"""
    if not jobs:
        print_info("暂无定时任务")
        return

    table = Table(title="定时任务", border_style="yellow")
    table.add_column("ID", style="dim", no_wrap=True)
    table.add_column("名称")
    table.add_column("Cron")
    table.add_column("技能")
    table.add_column("状态")
    table.add_column("上次执行")

    for j in jobs:
        status_color = {"active": "green", "paused": "yellow", "deleted": "red"}.get(
            j.status.value, "white"
        )
        last_run = j.last_run_at.strftime("%m-%d %H:%M") if j.last_run_at else "从未"
        table.add_row(
            j.id[:8],
            j.name[:30],
            j.cron_expr,
            j.skill_name,
            f"[{status_color}]{j.status.value}[/{status_color}]",
            last_run,
        )
    console.print(table)


def print_memory_table(memories: list) -> None:
    """打印记忆列表"""
    if not memories:
        print_info("暂无跨会话记忆")
        return

    table = Table(title="Agent 记忆", border_style="magenta")
    table.add_column("Key", style="bold")
    table.add_column("Value")
    table.add_column("类别", style="dim")

    for m in memories:
        table.add_row(m.key, m.value[:60], m.category.value)
    console.print(table)


def print_tools_table(tool_names: list[str], schemas: list[dict]) -> None:
    """打印工具列表"""
    table = Table(title=f"可用工具（共 {len(tool_names)} 个）", border_style="cyan")
    table.add_column("工具名称", style="bold yellow")
    table.add_column("描述")

    schema_map = {s["function"]["name"]: s["function"]["description"] for s in schemas}
    for name in sorted(tool_names):
        desc = schema_map.get(name, "")[:70]
        table.add_row(name, desc)
    console.print(table)


class StreamOutput:
    """流式输出辅助类，支持 Live 更新"""

    def __init__(self):
        self._buffer = ""

    def update(self, token: str) -> None:
        self._buffer += token
        console.print(token, end="", markup=False)

    def flush(self) -> None:
        if self._buffer:
            console.print()  # 换行
            self._buffer = ""


# ══════════════════════════════════════════════════════
# 任务计划渲染
# ══════════════════════════════════════════════════════

def render_progress_bar(percent: int, width: int = 20) -> str:
    """渲染文本进度条"""
    filled = int(width * percent / 100)
    return f"{'█' * filled}{'░' * (width - filled)} {percent}%"


def print_plan_header(goal: str) -> None:
    """打印计划头部"""
    console.print(f"\n[agent]📋 任务计划: {goal}[/agent]")


def print_step_status(
    step_id: int,
    description: str,
    status: str,
    tool_used: str = None,
) -> None:
    """打印单个步骤的状态"""
    status_marks = {
        "pending": "○",
        "running": "▶",
        "done": "✓",
        "failed": "✗",
        "skipped": "⊘",
    }
    status_colors = {
        "pending": "dim white",
        "running": "bold yellow",
        "done": "bold green",
        "failed": "bold red",
        "skipped": "bold blue",
    }

    mark = status_marks.get(status, "○")
    color = status_colors.get(status, "white")

    line = f"  {step_id:<2}  [{color}]{mark}[/{color}]    {description}"
    if tool_used:
        line += f" [dim]({tool_used})[/dim]"
    console.print(line)


def print_plan_for_confirmation(skill_name: str, plan_text: str) -> None:
    """
    Plan-First 模式：以面板形式展示执行计划供用户确认
    """
    from rich.panel import Panel
    from rich.text import Text

    lines = Text()
    for line in plan_text.splitlines():
        line = line.strip()
        if not line:
            continue
        # 将数字起始的行用黄色步骤号标注
        if line and line[0].isdigit() and ". " in line:
            num, rest = line.split(". ", 1)
            lines.append(f"  {num}. ", style="bold yellow")
            lines.append(f"{rest}\n")
        else:
            lines.append(f"  {line}\n", style="dim")

    panel = Panel(
        lines,
        title=f"[bold cyan]📋 执行计划：{skill_name}[/bold cyan]",
        border_style="cyan",
        padding=(1, 2),
    )
    console.print()
    console.print(panel)
    console.print(
        "  [dim]回复方式：  [bold green]y[/bold green]/回车 = 确认执行  "
        "[bold red]n[/bold red] = 取消  "
        "直接输入修改意见 = 重新生成计划[/dim]"
    )


def print_plan_progress(
    goal: str,
    steps: list,
    progress_text: str,
    progress_percent: int,
) -> None:
    """
    打印完整的计划进度
    
    参数：
        goal: 任务目标
        steps: [(step_id, description, status, tool_used), ...]
        progress_text: "3/10"
        progress_percent: 30
    """
    console.print(f"\n[agent]📋 任务计划: {goal}[/agent]")
    console.print()
    console.print(f"[bold]步骤  状态  描述[/bold]")
    console.print(f"[dim]{'─' * 60}[/dim]")

    for step_id, description, status, tool_used in steps:
        print_step_status(step_id, description, status, tool_used)

    console.print()
    bar = render_progress_bar(progress_percent)
    console.print(f"[info]进度: {progress_text} {bar}[/info]")
    console.print()
