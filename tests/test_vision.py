"""看图：格式嗅探、失败诊断、载荷形状。

不碰真网络：`OpenAI` 换成假客户端，只验"我们发出去的是什么、拿回来的怎么翻"。
"""

from types import SimpleNamespace

import pytest

from agent_harness import vision
from agent_harness.settings import Settings

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def _fake_client(create):
    """造一个假的 OpenAI：chat.completions.create 走传入的函数。"""

    class _Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=create)
            )

    return _Client


def _settings_with_key() -> Settings:
    s = Settings()
    s.models["default"].api_key = "test-key"  # 免得去读环境变量
    return s


class _LLM:
    current_profile = "default"


def test_sniff_mime_reads_bytes_not_extension():
    assert vision.sniff_mime(PNG) == "image/png"
    assert vision.sniff_mime(b"\xff\xd8\xff\xe0rest") == "image/jpeg"
    assert vision.sniff_mime(b"RIFF0000WEBPVP8 ") == "image/webp"
    assert vision.sniff_mime(b"<html>not an image</html>") is None


def test_view_image_sends_low_detail_and_returns_text(monkeypatch, tmp_path):
    seen = {}

    def create(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="一只猫"))]
        )

    monkeypatch.setattr(vision, "OpenAI", _fake_client(create))
    img = tmp_path / "shot.png"
    img.write_bytes(PNG)

    out = vision.make_view_image(_settings_with_key(), _LLM())(str(img), "图里是什么")

    assert out.startswith("一只猫")
    assert 'detail="high"' in out  # 缩略图要自报家门：模型才知道还能要高清
    parts = seen["messages"][0]["content"]
    assert parts[0] == {"type": "text", "text": "图里是什么"}
    assert parts[1]["image_url"]["detail"] == "low"  # 判断"渲染对不对"够用了
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_detail_is_chosen_from_the_question(monkeypatch, tmp_path):
    """问文字/报错 → high；问布局/颜色 → low；显式传的优先。"""
    assert vision.pick_detail("右上角报错信息写的是什么") == "high"
    assert vision.pick_detail("标题栏的文字") == "high"
    assert vision.pick_detail("整体布局对不对，主色调是什么") == "low"
    assert vision.pick_detail("随便看看", "high") == "high"

    seen = {}

    def create(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    monkeypatch.setattr(vision, "OpenAI", _fake_client(create))
    img = tmp_path / "a.png"
    img.write_bytes(PNG)
    out = vision.make_view_image(_settings_with_key(), _LLM())(
        str(img), "报错是什么", "high"
    )
    assert seen["messages"][0]["content"][1]["image_url"]["detail"] == "high"
    assert "detail=low" not in out  # high 不该再提示"还能更清楚"


def test_unsupported_model_becomes_actionable_advice(monkeypatch, tmp_path):
    def create(**kwargs):
        raise RuntimeError("Error code: 400 - This model does not support image")

    monkeypatch.setattr(vision, "OpenAI", _fake_client(create))
    img = tmp_path / "shot.png"
    img.write_bytes(PNG)

    out = vision.make_view_image(_settings_with_key(), _LLM())(str(img))

    assert "不支持图片" in out and "vision_profile" in out  # 告诉人下一步怎么做


def test_missing_file_is_reported_not_raised(tmp_path):
    out = vision.make_view_image(_settings_with_key(), _LLM())(str(tmp_path / "nope.png"))
    assert "不存在" in out


def test_non_image_is_rejected_before_any_request(monkeypatch, tmp_path):
    def create(**kwargs):
        raise AssertionError("不是图片就不该发请求")

    monkeypatch.setattr(vision, "OpenAI", _fake_client(create))
    f = tmp_path / "a.txt"
    f.write_text("hello", encoding="utf-8")

    out = vision.make_view_image(_settings_with_key(), _LLM())(str(f))
    assert "不像是支持的图片格式" in out


def test_vision_off_profile_is_respected(tmp_path):
    s = _settings_with_key()
    s.models["default"].vision = False
    img = tmp_path / "a.png"
    img.write_bytes(PNG)

    out = vision.make_view_image(s, _LLM())(str(img))
    assert "vision = false" in out
