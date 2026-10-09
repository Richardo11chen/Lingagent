"""S4c 单测：规则文件加载（配置维——声明式约束的机制化）。"""

from agent_harness.config import RULE_FILES, load_rules


def test_loads_existing_rule_file(tmp_path):  # 有文件：内容拼上且带来源标题
    (tmp_path / "AGENTS.md").write_text("约定：测试用 pytest", encoding="utf-8")
    out = load_rules(tmp_path)
    assert "# AGENTS.md" in out
    assert "测试用 pytest" in out


def test_empty_when_no_file(tmp_path):  # 无文件：空串（正常状态，不报错）
    assert load_rules(tmp_path) == ""


def test_multiple_files_titled_and_ordered(tmp_path, monkeypatch):  # 多文件：标题分隔、保持声明顺序
    (tmp_path / "AGENTS.md").write_text("A 约定", encoding="utf-8")
    (tmp_path / "RULES.md").write_text("B 约定", encoding="utf-8")
    monkeypatch.setattr("agent_harness.config.RULE_FILES", ("AGENTS.md", "RULES.md"))
    out = load_rules(tmp_path)
    assert "# AGENTS.md" in out and "# RULES.md" in out
    assert out.index("A 约定") < out.index("B 约定")


def test_only_reads_workspace(tmp_path):  # 隔离：别的目录有同名文件也不读
    other = tmp_path / "other"
    other.mkdir()
    (other / "AGENTS.md").write_text("不该读到", encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir()
    assert load_rules(ws) == ""
