"""`lingan --init` 向导的单测：只测「写出来的东西对不对」，不碰真实家目录。

关键不变式：
- 已有配置绝不覆盖
- 生成的 config 能被 load_settings 正确读回（base_url / name / key 三个都落对）
- key 走明文、或走环境变量，两条路都写对
"""

import pytest

from agent_harness import config_wizard as cw
from agent_harness.settings import load_settings


@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    monkeypatch.setattr(cw, "CONFIG_PATH", p)
    return p


def _feed(monkeypatch, answers):
    """把一串输入灌给 input()/getpass。"""
    it = iter(answers)

    monkeypatch.setattr("builtins.input", lambda prompt="": next(it))
    monkeypatch.setattr(cw, "_secret", lambda prompt="": next(it))


def test_init_writes_a_loadable_config(monkeypatch, cfg_path, tmp_path):
    _feed(monkeypatch, ["https://api.moonshot.cn/v1", "kimi-k2", "sk-abc123"])
    assert cw.run_init() == 0
    assert cfg_path.exists()

    s = load_settings(cfg_path)  # 能被设置模块读回，而不是写了个语法都过不了的假文件
    assert s.default_model == "main"
    p = s.model("main")
    assert p.base_url == "https://api.moonshot.cn/v1"
    assert p.name == "kimi-k2"
    assert p.api_key == "sk-abc123"


def test_init_defaults_when_blank(monkeypatch, cfg_path):
    _feed(monkeypatch, ["", "", ""])  # 全回车 → 全默认 + 环境变量方式
    assert cw.run_init() == 0
    s = load_settings(cfg_path).model("main")
    assert s.base_url == cw.DEFAULT_BASE_URL
    assert s.name == cw.DEFAULT_MODEL
    assert s.api_key == ""  # 没写 key
    assert s.api_key_env == cw.DEFAULT_KEY_ENV


def test_init_never_overwrites(monkeypatch, cfg_path):
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("# 我自己的配置，别动\n", encoding="utf-8")

    assert cw.run_init() == 0
    assert cfg_path.read_text(encoding="utf-8") == "# 我自己的配置，别动\n"  # 原样未动


def test_init_cancel_writes_nothing(monkeypatch, cfg_path):
    def _boom(prompt="", default=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", _boom)
    assert cw.run_init() == 130
    assert not cfg_path.exists()


def test_toml_string_escaping():
    # 引号、反斜杠都要转义，否则 key / 地址带特殊字符就写坏了
    assert cw._toml_str('a"b\\c') == '"a\\"b\\\\c"'
