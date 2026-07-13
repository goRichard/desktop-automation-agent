"""
系统工具：文件操作、剪贴板、系统命令、等待
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Optional

from .registry import tool


@tool(
    description="等待指定秒数。用于 UI 操作之间需要等待页面/窗口响应的场景。seconds 为等待时长（支持小数，如 0.3、1.5），最大不超过 30 秒。",
    risk="read",
)
async def sleep(seconds: float) -> str:
    """异步等待指定秒数"""
    if seconds <= 0:
        return "sleep: seconds 必须大于 0"
    if seconds > 30:
        seconds = 30
    await asyncio.sleep(seconds)
    return f"已等待 {seconds} 秒"


@tool(
    description="读取文本文件内容。file_path 为文件绝对路径，encoding 默认 utf-8。返回文件内容字符串。",
    risk="read",
)
def read_file(file_path: str, encoding: Optional[str] = None) -> str:
    enc = encoding or "utf-8"
    try:
        return Path(file_path).read_text(encoding=enc, errors="replace")
    except FileNotFoundError:
        return f"错误：文件不存在 - {file_path}"
    except Exception as e:
        return f"读取文件失败: {e}"


@tool(
    description="将文本内容写入文件。file_path 为文件路径，content 为写入内容，append=true 时追加而非覆盖。父目录不存在时自动创建。",
    risk="medium",
    side_effect=True,
)
def write_file(
    file_path: str,
    content: str,
    append: Optional[bool] = None,
) -> str:
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8") if not append else \
        path.open("a", encoding="utf-8").write(content)
    return f"已写入文件: {file_path}"


@tool(
    description="列出目录中的文件和子目录。dir_path 为目录路径，pattern 为可选的 glob 过滤模式（如 '*.txt'）。",
    risk="read",
)
def list_dir(dir_path: str, pattern: Optional[str] = None) -> str:
    path = Path(dir_path)
    if not path.exists():
        return f"错误：目录不存在 - {dir_path}"
    try:
        items = list(path.glob(pattern or "*")) if pattern else list(path.iterdir())
        lines = []
        for item in sorted(items):
            prefix = "📁" if item.is_dir() else "📄"
            lines.append(f"{prefix} {item.name}")
        return "\n".join(lines) if lines else "（空目录）"
    except Exception as e:
        return f"列目录失败: {e}"


@tool(
    description="执行系统命令（PowerShell）。command 为要执行的命令字符串，直接写命令即可（如 'Get-Process'、'$p=\"C:\\temp\"; Get-ChildItem $p'），不需要加 powershell 前缀。返回命令输出。注意：谨慎使用，避免执行危险命令。",
    risk="high",
    side_effect=True,
    allowed_modes=["agent", "step", "guided"],
)
async def run_command(command: str) -> str:
    # 自动剥离多余的 powershell 前缀（LLM 有时会生成重复包装）
    cmd = command.strip()
    # 匹配 "powershell ... -Command ..." 模式，提取实际命令
    import re
    ps_match = re.match(
        r'^powershell(?:\.exe)?\s+(?:-[\w\s]+?\s+)?-Command\s+["\']?(.+?)["\']?\s*$',
        cmd,
        re.IGNORECASE | re.DOTALL,
    )
    if ps_match:
        cmd = ps_match.group(1).strip()
        # 去除首尾引号
        if (cmd.startswith('"') and cmd.endswith('"')) or (cmd.startswith("'") and cmd.endswith("'")):
            cmd = cmd[1:-1]

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        output = result.stdout.strip()
        if result.returncode != 0 and result.stderr:
            output += f"\n[stderr]: {result.stderr.strip()}"
        return output or "（命令执行完成，无输出）"
    except subprocess.TimeoutExpired:
        return "错误：命令执行超时（30s）"
    except Exception as e:
        return f"命令执行失败: {e}"


@tool(description="获取剪贴板当前内容（文字）。", risk="read")
def get_clipboard() -> str:
    try:
        result = subprocess.run(
            ["powershell", "-Command", "Get-Clipboard"],
            capture_output=True, text=True, encoding="utf-8",
        )
        return result.stdout.strip() or "（剪贴板为空）"
    except Exception as e:
        return f"获取剪贴板失败: {e}"


@tool(
    description="将文字内容写入剪贴板。text 为要写入的文字内容。",
    risk="medium",
    side_effect=True,
)
def set_clipboard(text: str) -> str:
    try:
        subprocess.run(
            ["powershell", "-Command", f'Set-Clipboard -Value "{text}"'],
            capture_output=True, text=True, encoding="utf-8",
        )
        return f"已写入剪贴板: {text[:50]}..."
    except Exception as e:
        return f"写入剪贴板失败: {e}"
