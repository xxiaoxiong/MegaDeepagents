#!/usr/bin/env python3
"""独立可运行的 CLI 入口脚本。

用法:
    python scripts/run_cli.py run "帮我写一个 Python 函数"
    python scripts/run_cli.py task-list
    python scripts/run_cli.py task-show <task_id>
    python scripts/run_cli.py config
    python scripts/run_cli.py memory search "关键词"
    python scripts/run_cli.py skills list
    python scripts/run_cli.py tools list
    python scripts/run_cli.py --help
"""

import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中（支持从任意位置运行）
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))


def main():
    from app.cli_tool import cli
    cli()


if __name__ == "__main__":
    main()
