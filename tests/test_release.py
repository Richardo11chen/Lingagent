"""scripts/release.py 的纯函数单测：只测版本号校验，不真去 build / publish。

关键不变式：
- 版本号必须 +1（PyPI 不能重传同名文件）
- dist/ 里已有该版本产物 → 拦下
- bump_version 只改 version 这一行，不碰别的

只测 `_validate` / `current_version` / `bump_version` 三个纯函数，路径全部显式传入——
不 monkeypatch 模块全局，避免踩「默认参数在 import 时就绑死了真路径」的坑。
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


def test_validate_requires_a_bump(proj, tmp_path):
    with pytest.raises(SystemExit) as e:
        release._validate("0.1.0", proj, tmp_path / "dist")
    assert "必须 +1" in str(e.value)


def test_validate_refuses_an_already_built_version(proj, tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "lingagent-0.2.0-py3-none-any.whl").write_text("", encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        release._validate("0.2.0", proj, dist)
    assert "换一个版本号" in str(e.value)


def test_validate_rejects_a_bad_version_shape(proj, tmp_path):
    with pytest.raises(SystemExit) as e:
        release._validate("0.2", proj, tmp_path / "dist")
    assert "形如 0.2.0" in str(e.value)


def test_validate_passes_a_clean_bump(proj, tmp_path):
    release._validate("0.2.0", proj, tmp_path / "dist")  # 不抛 = 通过
