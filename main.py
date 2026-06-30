"""
Desktop Agent 主入口
"""
import sys
from pathlib import Path

# 将项目根目录加入 sys.path（确保所有模块可以相对导入）
sys.path.insert(0, str(Path(__file__).parent))

from cli.app import cli

if __name__ == "__main__":
    cli()
