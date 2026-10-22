"""应用目录：配置、存档、状态都收在 ~/.agent-harness 下——不往家目录撒点文件。

单独成一个模块的理由：settings / session / trace 都要用这个路径，
各自写一遍 `Path.home() / ".agent-harness"` 迟早会漂。
"""

from pathlib import Path

APP_DIR = Path.home() / ".agent-harness"
