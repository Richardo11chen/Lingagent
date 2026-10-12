"""S7 单测：配置加载（settings——技术超参 + 多模型 profiles，~/.agent_harness.toml）。"""

from types import SimpleNamespace

from agent_harness.settings import Settings, load_settings


def test_defaults_when_no_file(tmp_path):  # 无配置文件 → 全默认（单个 default profile）
    s = load_settings(tmp_path / "nope.toml")
    assert s.default_model == "default"
    assert s.model().name == "deepseek-v4-flash"


def test_multi_profiles_and_default(tmp_path):  # 多 profile：default 选取 + 按名取
    p = tmp_path / "c.toml"
    p.write_text(
        """
[model]
default = "proxy"

[models.deepseek]
name = "deepseek-v4-flash"
temperature = 0.3
thinking = "enabled"

[models.proxy]
name = "gpt-4o"
base_url = "https://proxy.example.com/v1"
api_key_env = "PROXY_KEY"
""",
        encoding="utf-8",
    )
    s = load_settings(p)
    assert s.default_model == "proxy"
    assert s.model().name == "gpt-4o"                 # default 选中 proxy
    assert s.model("deepseek").temperature == 0.3     # 按名取
    assert s.model("deepseek").thinking == "enabled"
    assert s.model("proxy").thinking == "off"         # 不写 = 不传（跨厂商安全）
    assert s.model("proxy").api_key_env == "PROXY_KEY"
    assert s.model("不存在").name == "gpt-4o"          # 未知名字回退 default


def test_default_must_exist(tmp_path):  # default 指向不存在的 profile → 当场退出（别等 KeyError）
    import pytest

    p = tmp_path / "c.toml"
    p.write_text('[model]\ndefault = "nope"\n\n[models.a]\nname = "m"\n', encoding="utf-8")
    with pytest.raises(SystemExit):
        load_settings(p)


def test_legacy_single_model_section(tmp_path):  # 老格式 [model] 单节自动兼容
    p = tmp_path / "c.toml"
    p.write_text('[model]\nname = "deepseek-v4-pro"\n', encoding="utf-8")
    s = load_settings(p)
    assert s.default_model == "default"
    assert s.model().name == "deepseek-v4-pro"


def test_config_syntax_error_fails_fast(tmp_path):  # TOML 语法错（如值没加引号）→ 干净报错，不是 traceback
    import pytest

    p = tmp_path / "c.toml"
    p.write_text("[models.x]\napi_key_env = sk-abc\n", encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        load_settings(p)
    assert "语法错误" in str(e.value)


def test_global_sections(tmp_path):  # 全局节（loop/context/server）照旧
    p = tmp_path / "c.toml"
    p.write_text(
        "[loop]\nmax_steps = 5\n\n[context]\ncompact_threshold = 1234\n\n"
        "[server]\nport = 9999\n\n[web]\nautostart = false\n",
        encoding="utf-8",
    )
    s = load_settings(p)
    assert s.max_steps == 5
    assert s.compact_threshold == 1234
    assert s.server_port == 9999
    assert s.web_autostart is False


def test_private_params_fallback(tmp_path):  # 私有参数非法值一律回退 off（= 不传）——别瞎猜厂商语义
    p = tmp_path / "c.toml"
    p.write_text('[models.x]\nreasoning_effort = "none"\nthinking = true\n', encoding="utf-8")
    s = load_settings(p).model("x")
    assert s.reasoning_effort == "off"  # none 不是 chat 的合法值；bool 也不是合法状态
    assert s.thinking == "off"
    p.write_text('[models.x]\nreasoning_effort = "max"\nthinking = "disabled"\n', encoding="utf-8")
    s = load_settings(p).model("x")
    assert s.reasoning_effort == "max"
    assert s.thinking == "disabled"


def _stub_client(captured: dict):
    """假 OpenAI client：把 create 的 kwargs 抄下来，返回空流。"""

    def create(**kwargs):
        captured.update(kwargs)
        return iter([])

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def test_api_key_direct(tmp_path, monkeypatch):  # api_key 直写配置：优先于 api_key_env
    from agent_harness.llm import DeepSeekLLM

    p = tmp_path / "c.toml"
    p.write_text('[models.x]\nname = "m"\napi_key = "sk-direct"\n', encoding="utf-8")
    prof = load_settings(p).model("x")
    assert prof.api_key == "sk-direct"
    monkeypatch.delenv("X_KEY", raising=False)  # 环境变量没设也不影响——直写优先
    llm = DeepSeekLLM("sys", api_key=prof.api_key, api_key_env="X_KEY")
    assert llm._client.api_key == "sk-direct"


def test_api_key_missing_fail_fast(monkeypatch):  # 直写和 env 都缺 → 启动时当场退出（别等首次请求 401）
    import pytest

    from agent_harness.llm import DeepSeekLLM

    monkeypatch.delenv("NO_SUCH_KEY", raising=False)
    with pytest.raises(SystemExit):
        DeepSeekLLM("sys", api_key_env="NO_SUCH_KEY")


def test_request_omits_private_params_when_off(monkeypatch):
    # 跨厂商安全：off（默认）= 请求体里一个 DeepSeek 私有参数都没有
    from agent_harness.llm import DeepSeekLLM
    from agent_harness.messages import Msg

    monkeypatch.setenv("PROXY_KEY", "x")
    llm = DeepSeekLLM("sys", model="gpt-4o", api_key_env="PROXY_KEY")
    captured: dict = {}
    llm._client = _stub_client(captured)
    llm([Msg("user", "hi")], [])

    assert "extra_body" not in captured
    assert "reasoning_effort" not in captured


def test_stream_tolerates_none_delta(monkeypatch):
    # 有的网关在收尾 chunk 里 delta=null（minimax-m3 实测）——解析不能崩
    from types import SimpleNamespace

    from agent_harness.llm import DeepSeekLLM
    from agent_harness.messages import Msg

    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")
    llm = DeepSeekLLM("sys")

    def chunk(delta):
        return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])

    chunks = [
        chunk(SimpleNamespace(content="你好", tool_calls=None, reasoning_content=None)),
        chunk(None),  # ← 实测撞到的那片
        SimpleNamespace(choices=[]),  # usage 类 chunk：无 choices
    ]
    llm._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: iter(chunks)))
    )
    text, actions, reasoning = llm([Msg("user", "hi")], [])
    assert text == "你好"
    assert actions == []


def test_request_carries_private_params_when_set(monkeypatch):
    # 显式声明才传（DeepSeek 语义原样保留）
    from agent_harness.llm import DeepSeekLLM
    from agent_harness.messages import Msg

    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")
    llm = DeepSeekLLM("sys", thinking="enabled", reasoning_effort="high")
    captured: dict = {}
    llm._client = _stub_client(captured)
    llm([Msg("user", "hi")], [])

    assert captured["extra_body"] == {"thinking": {"type": "enabled"}}
    assert captured["reasoning_effort"] == "high"
