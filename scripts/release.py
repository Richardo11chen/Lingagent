"""发布脚本：把「改版本号 → 打前端 → 打包 → 发布」串成一条命令。

用法（在仓库根目录）：

    uv run python scripts/release.py 0.2.0            # 正式发 PyPI
    uv run python scripts/release.py 0.2.0 --test     # 发 TestPyPI（建议先试这个）
    uv run python scripts/release.py 0.2.0 --no-publish  # 只改版本号 + 打包，不发布

为什么要有它：发布有几步是「忘了就出问题」的——`build_web` 不跑，wheel 里就没前端；
PyPI 不允许重传同名文件，版本号忘了 +1 直接拒收。用一个脚本把这些坑都挡住。

令牌：走环境变量 `UV_PUBLISH_TOKEN`；没设的话 `uv publish` 会交互式问你要。
"""

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
TEST_PYPI_URL = "https://test.pypi.org/legacy/"

_VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)


def current_version(pyproject: Path = PYPROJECT) -> str:
    m = _VERSION_RE.search(pyproject.read_text(encoding="utf-8"))
    if not m:
        raise SystemExit(f"在 {pyproject} 里找不到 version = \"...\"")
    return m.group(1)


def bump_version(version: str, pyproject: Path = PYPROJECT) -> None:
    """把 [project] 的 version 改成目标值（原地）。"""
    text = pyproject.read_text(encoding="utf-8")
    new, n = _VERSION_RE.subn(f'version = "{version}"', text, count=1)
    if n != 1:
        raise SystemExit("version 行没匹配上，没改动")
    pyproject.write_text(new, encoding="utf-8")


def _validate(version: str, pyproject: Path, dist_dir: Path) -> None:
    """发布前的两道闸：版本号必须 +1；这个版本不能已经打过包。"""
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SystemExit(f"版本号要形如 0.2.0，收到的是 {version!r}")
    if version == current_version(pyproject):
        raise SystemExit(f"版本号还是 {version}——PyPI 不允许重传同名文件，每次发布必须 +1。")
    if any(dist_dir.glob(f"lingagent-{version}*")):
        raise SystemExit(f"dist/ 里已经有 {version} 的产物了——换一个版本号。")


def _run(argv: list[str], **kw) -> None:
    print(f"\n$ {' '.join(argv)}")
    subprocess.run(argv, cwd=ROOT, check=True, **kw)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="发布 lingagent")
    ap.add_argument("version", help="要发的版本号，如 0.2.0")
    ap.add_argument("--test", action="store_true", help="发到 TestPyPI（不是真 PyPI）")
    ap.add_argument("--no-publish", action="store_true", help="只改版本号 + 打包，不发布")
    args = ap.parse_args(argv)

    _validate(args.version, PYPROJECT, ROOT / "dist")

    # ① 前端打进包里（忘了这步 wheel 就没网页端）
    _run([sys.executable, "scripts/build_web.py"])

    # ② 版本号 + 锁文件一起动（uv.lock 里也记着版本，不同步的话 uv 会报 lockfile 过期）
    bump_version(args.version)
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("找不到 uv——它没在 PATH 里。装一下：https://docs.astral.sh/uv/")
    _run([uv, "lock"])

    # ③ 打包
    _run([uv, "build"])

    if args.no_publish:
        print("\n打包完成（未发布）。要发出去：")
        print(f"  uv publish{' --publish-url ' + TEST_PYPI_URL if args.test else ''}")
        return 0

    # ④ 发布
    publish = [uv, "publish"]
    if args.test:
        publish += ["--publish-url", TEST_PYPI_URL]
    _run(publish)

    print(f"\n完成。装一下试试：uv tool install lingagent=={args.version}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
