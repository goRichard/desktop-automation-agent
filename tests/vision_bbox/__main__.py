import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中，这样 config、llm 等模块可以被导入
_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from .test_bbox_precision import main

if __name__ == "__main__":
    main()
