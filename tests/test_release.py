"""scripts/release.py 的纯函数单测：只测版本号校验与「已发布」判定，不真去 build / publish。

关键不变式：
- 版本号要形如 x.y.z
- 「能不能发」看目标 index 上有没有这个版本（404 = 没有，能发；200 = 有，拦下）
- bump_version 只改 version 这一行，不碰别的

只测纯函数、路径显式传入，不 monkeypatch 模块全局。
"""

import sys
from pathlib import Path

# `uv run pytest` 跑的是 console 脚本，不会把仓库根加进 sys.path；
# 但 `scripts` 是顶层目录，得显式加进去才能 `from scripts import release`。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from scripts import release


@pytest.fixture
def proj(tmp_path):
    p = tmp_path / "pyproject.toml"
    p.write_text('[project]\nname = "lingagent"\nversion = "0.1.0"\ndescription = "x"\n', encoding="utf-8")
    return p


def test_current_and_bump(proj):
    assert release.current_version(proj) == "0.1.0"
    release.bump_version("0.2.0", proj)
    assert release.current_version(proj) == "0.2.0"
    text = proj.read_text(encoding="utf-8")
    assert 'name = "lingagent"' in text  # 别的行原样不动
    assert 'description = "x"' in text


def test_validate_shape_rejects_bad_version():
    with pytest.raises(SystemExit) as e:
        release._validate_shape("0.2")
    assert "形如 0.2.0" in str(e.value)


def test_validate_shape_accepts_clean_version():
    release._validate_shape("0.1.0")  # 不抛 = 通过


def test_already_published_blocks_on_200(monkeypatch):
    class _Resp:  # opener.open() 返回一个上下文管理器
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Opener:  # build_opener 返回一个带 .open() 的对象
        def open(self, url, timeout=5):
            return _Resp()

    monkeypatch.setattr(release.urllib.request, "build_opener", lambda *a, **k: _Opener())
    assert release._already_published("lingagent", "0.1.0", "https://pypi.org") is True


def test_already_published_allows_404(monkeypatch):
    import urllib.error

    class _Opener:
        def open(self, url, timeout=5):
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)

    monkeypatch.setattr(release.urllib.request, "build_opener", lambda *a, **k: _Opener())
    assert release._already_published("lingagent", "0.1.0", "https://pypi.org") is False


def test_already_published_does_not_block_on_network_error(monkeypatch):
    class _Opener:
        def open(self, url, timeout=5):
            raise OSError("断网了")

    monkeypatch.setattr(release.urllib.request, "build_opener", lambda *a, **k: _Opener())
    # 网络不通 = 不拦（让 uv publish 自己报）
    assert release._already_published("lingagent", "0.1.0", "https://pypi.org") is False
