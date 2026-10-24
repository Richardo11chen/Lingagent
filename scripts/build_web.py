"""把前端构建产物搬进包里：`web/dist` → `src/agent_harness/web_dist/`。

发布前跑一次（`uv run python scripts/build_web.py`）。之后 `uv build` 出的 wheel 自带网页界面：
装完不需要 Node、不需要 vite，后端一个端口就把界面和 WS 一起发出去。

为什么不直接让后端去读仓库里的 `web/dist`：装成 wheel 之后没有仓库。前端必须跟着包走。
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
DIST = WEB / "dist"
TARGET = ROOT / "src" / "agent_harness" / "web_dist"


def main() -> None:
    if not (WEB / "package.json").exists():
        raise SystemExit(f"找不到前端工程：{WEB}")
    if not (WEB / "node_modules").is_dir():
        print("先装依赖：npm install（在 web/ 下）")
        raise SystemExit(1)

    npm = shutil.which("npm")
    if not npm:
        raise SystemExit("找不到 npm（构建前端需要 Node）")
    cmd = [npm, "run", "build"]
    if npm.lower().endswith((".cmd", ".bat")):
        cmd = [__import__("os").environ.get("COMSPEC", "cmd.exe"), "/c", *cmd]
    print(f"构建前端：{' '.join(cmd)}")
    subprocess.run(cmd, cwd=WEB, check=True)

    if not (DIST / "index.html").exists():
        raise SystemExit(f"构建完没看到 {DIST / 'index.html'}")

    if TARGET.exists():
        shutil.rmtree(TARGET)  # 整个换掉：留着旧 hash 的 assets 只会让 wheel 变大
    shutil.copytree(DIST, TARGET)

    files = sorted(p for p in TARGET.rglob("*") if p.is_file())
    size = sum(p.stat().st_size for p in files)
    print(f"已放进包里：{TARGET.relative_to(ROOT)}（{len(files)} 个文件，{size / 1024:.0f} KB）")
    for p in files[:5]:
        print("  ", p.relative_to(TARGET))
    if len(files) > 5:
        print("   …")


if __name__ == "__main__":
    sys.exit(main())
