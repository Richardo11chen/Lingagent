"""S7 单测：配置加载（settings——技术超参，~/.agent_harness.toml）。"""

from agent_harness.settings import Settings, load_settings


def test_defaults_when_no_file(tmp_path):  # 无配置文件 → 全默认
    s = load_settings(tmp_path / "nope.toml")
    assert s == Settings()


def test_overrides_merge_with_defaults(tmp_path):  # 有文件 → 覆盖对应字段、其余保持默认
    p = tmp_path / "c.toml"
    p.write_text(
        '[model]\nname = "deepseek-v4-pro"\ntemperature = 0.3\n\n[context]\ncompact_threshold = 1234\n',
        encoding="utf-8",
    )
    s = load_settings(p)
    assert s.model_name == "deepseek-v4-pro"
    assert s.temperature == 0.3
    assert s.compact_threshold == 1234
    assert s.max_steps == 20  # 未覆盖的保持默认
    assert s.server_port == 8000


def test_partial_sections(tmp_path):  # 只给部分节也不炸
    p = tmp_path / "c.toml"
    p.write_text("[server]\nport = 9999\n", encoding="utf-8")
    s = load_settings(p)
    assert s.server_port == 9999
    assert s.model_name == "deepseek-v4-flash"


def test_reasoning_effort_fallback(tmp_path):  # 非法强度值（含 none）回退 high——开关归 thinking
    p = tmp_path / "c.toml"
    p.write_text('[model]\nreasoning_effort = "none"\n', encoding="utf-8")
    assert load_settings(p).reasoning_effort == "high"
    p.write_text('[model]\nreasoning_effort = "max"\n', encoding="utf-8")
    assert load_settings(p).reasoning_effort == "max"
