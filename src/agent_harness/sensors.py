"""反馈校验器：找到本项目的测试入口 → 跑 → 判红绿 → 生成报告（反馈是代码机制，规格 A.4(B)）。

为什么不写死 pytest：这个 agent 不只用来改这个仓库。项目类型不同，测试入口就不同，
而「改完必须验证」这条纪律不该只在 Python 项目里成立。

判定仍然是代码，不是提示词：退出码定红绿；绿了只回一句话（不贴几十行点号），
红了才挑跟失败有关的部分——模型需要知道哪个红了、为什么，不需要看跑测试的过程。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from .tools import _decode  # 同一个坑：子进程按自己的编码写，父进程不能按 locale 硬解

# Python 项目的标记文件（有一个就按 pytest 算）
_PY_MARKERS = ("pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg")

PYTEST_NO_TESTS = 5  # pytest 的「没收集到测试」退出码——那不是失败，别报红


def detect_runner(root: Path, path: str = ".") -> tuple[str, list[str]] | None:
    """根据项目根的标记文件挑测试入口；一个都认不出就返回 None。

    顺序即优先级：Python 项目常同时有 Makefile，所以先认 pyproject 这类。
    path 只在能当过滤条件的 runner 上传下去（pytest / go / npm / cargo），make 用不上。
    """
    extra = [] if path in ("", ".") else [path]

    if any((root / m).exists() for m in _PY_MARKERS):
        return "pytest", [sys.executable, "-m", "pytest", "-q", path or "."]

    pkg = root / "package.json"
    if pkg.exists() and _has_npm_test(pkg):  # 有 package.json 但没有 test 脚本 → 往下试别的
        npm = shutil.which("npm") or "npm"
        return "npm", [npm, "test", "--silent", *(["--", *extra] if extra else [])]

    if (root / "go.mod").exists():
        return "go", ["go", "test", *(extra or ["./..."])]

    if (root / "Cargo.toml").exists():
        return "cargo", ["cargo", "test", "--quiet", *extra]

    makefile = root / "Makefile"
    if makefile.exists() and _has_make_target(makefile, "test"):
        return "make", ["make", "test"]

    return None


def _has_npm_test(pkg: Path) -> bool:
    """package.json 里有没有 scripts.test——没有的话 `npm test` 必然报错，别拿它当红。"""
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool((data.get("scripts") or {}).get("test"))


def _has_make_target(makefile: Path, target: str) -> bool:
    """Makefile 里有没有这个目标（`test:` / `test :`）——没有就别拿 `make test` 去撞。"""
    try:
        for line in makefile.read_text(encoding="utf-8", errors="replace").splitlines():
            head = line.split("#")[0].strip()
            if head.startswith((f"{target}:", f"{target} :")):
                return True
    except OSError:
        pass
    return False


def run_tests(path: str = ".") -> str:
    """跑本项目的测试，返回红绿报告（工具入口，schema 只暴露 path）。"""
    found = detect_runner(Path.cwd(), path)
    if found is None:
        return (
            "没找到测试入口：项目根下没有 pyproject.toml / package.json（含 test 脚本）/ "
            "go.mod / Cargo.toml，Makefile 里也没有 test 目标。\n"
            "用 run_shell 跑本项目自己的测试命令（例如 `npm run test:unit`）——别猜。"
        )
    runner, command = found
    return run_sensors(command, runner=runner)


def _label(runner: str) -> str:
    return f"（{runner}）" if runner else ""


def _section(lines: list[str], marker: str) -> list[str]:
    """按 ===== 标题行分段：取标题含 marker 那段的正文（标题行不含）。

    pytest 输出结构：===== 标题 ===== 行分隔各段。找不到 marker 返回空。
    """
    starts = [i for i, line in enumerate(lines) if line.startswith("=")]
    for k, i in enumerate(starts):
        if marker in lines[i]:
            end = starts[k + 1] if k + 1 < len(starts) else len(lines)
            return lines[i + 1 : end]
    return []


def run_sensors(test_command: list[str], runner: str = "") -> str:
    """跑命令判红绿，生成报告字符串（回灌给模型）。

    绿：一句话（绿了不贴输出）。红：pytest 走结构化提取（失败清单 + 失败详情），
    其余 runner 格式各异，只给尾部 20 行——所有测试框架都把结论放在最后。
    """
    result = subprocess.run(test_command, capture_output=True)  # 抓字节，自己解码
    out = _decode((result.stdout or b"") + (result.stderr or b""))

    if result.returncode == 0:
        return f"测试全部通过{_label(runner)}。"

    if runner == "pytest" and result.returncode == PYTEST_NO_TESTS:
        return f"没有测试{_label(runner)}：这个路径下没收集到测试（退出码 5），不当作失败。"

    lines = out.splitlines()
    parts: list[str] = []
    if runner == "pytest":  # 只有 pytest 的输出结构值得按段解析
        failures = _section(lines, "FAILURES")
        summary = _section(lines, "short test summary info")
        if summary:
            parts.append("失败清单：\n" + "\n".join(summary[:15]))  # 一行一个失败，超 15 个截前
        if failures:
            parts.append("失败详情（关键在前）：\n" + "\n".join(failures[:25]))  # 详情块开头是定位/断言

    body = "\n\n".join(parts) if parts else "\n".join(lines[-20:])
    return f"测试失败{_label(runner)}（退出码 {result.returncode}）：\n{body}"
