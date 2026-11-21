"""看图：把图片交给支持视觉的模型，换回一段文字。

**为什么是"子调用"而不是"工具返回图片"**：OpenAI 系协议里图片只能出现在 **user 消息**
的 content blocks 里（DeepSeek 文档写得很死——system / assistant 里带图直接 400），
而工具结果的 role 是 `tool`。所以想合规，只有两条路：改 `Msg` 让工具能插一条 user 消息，
或者在这里自己发一次带图的请求。后者不动任何现有结构，主对话历史里只多一段文字。

**为什么没有 provider 抽象**：`image_url` 这套 blocks 已经是事实标准，OpenAI / DeepSeek /
OpenRouter / vLLM / Ollama 的兼容层都认。会变的只有"这个模型看不看得懂图"——那是能力，
用失败诊断回答，不靠一张永远过时的对照表。
"""

import base64
import os
from pathlib import Path

from openai import OpenAI

MAX_IMAGE_BYTES = 32 * 1024 * 1024  # 协议上限（base64 内联时）
_TIMEOUT = 60.0

# auto 判档用的线索：问"读字/看错"就得保原图，问"布局/颜色"下采样就够了。
# 这是**默认值选择**，不是能力判断——猜错了模型还能显式传 detail=high 重来一次，
# 所以关键词启发的代价很低（远低于维护一张"哪些模型支持视觉"的表）。
_FINE_HINTS = (
    "文字", "读", "报错", "错误", "标题", "字号", "数字", "对齐",
    "像素", "代码", "日志", "原文", "具体", "拼写",
)

_SUFFIX_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def sniff_mime(head: bytes) -> str | None:
    """按文件头认格式。扩展名会说谎，服务端也是按字节判的。"""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def explain_failure(exc: BaseException, model: str) -> str:
    """把失败翻成能直接照做的一句话。"""
    msg = str(exc)
    low = msg.lower()
    if "does not support image" in low or ("image" in low and "400" in low):
        return (
            f"当前模型（{model}）不支持图片输入。"
            "换一个能看图的 profile，或给某个 [models.*] 加 vision = true，"
            "再用 [tools] vision_profile 指过去。"
        )
    if "401" in msg or "authentication" in low or "invalid api key" in low:
        return "看图失败：API key 无效或过期。"
    if "timeout" in low:
        return "看图失败：请求超时（图太大或网络慢）。"
    return f"看图失败：{type(exc).__name__}: {msg[:200]}"


def pick_detail(question: str, requested: str = "auto") -> str:
    """决定用哪档 detail。

    low：下采样到 512×512，约一两百 token，判断"整体对不对"够用。
    high / original：保原图，约 1024 token 封顶，能读小字——贵三到五倍。
    """
    req = (requested or "auto").strip().lower()
    if req in ("low", "high", "original"):
        return req
    return "high" if any(k in question for k in _FINE_HINTS) else "low"


def make_view_image(settings, llm):
    """造 `view_image` 工具。看图用的 profile 可以与主模型不同（主模型未必能看图）。"""

    def view_image(path: str, question: str = "描述这张图；如果有文字，照抄关键内容",
                   detail: str = "auto") -> str:
        p = Path(path)
        if not p.is_file():
            return f"读不到图片：{path} 不存在（或不是文件）。"
        data = p.read_bytes()
        if not data:
            return f"图片是空的：{path}"
        if len(data) > MAX_IMAGE_BYTES:
            return f"图片太大（{len(data) // 1024 // 1024}MB，上限 32MB）。先压缩或裁一块再看。"
        mime = sniff_mime(data[:16]) or _SUFFIX_MIME.get(p.suffix.lower())
        if mime is None:
            return f"不像是支持的图片格式（png / jpg / gif / webp）：{path}"

        prof_name = settings.vision_profile or getattr(llm, "current_profile", "")
        prof = settings.model(prof_name)
        vision_flag = getattr(prof, "vision", "auto")
        if vision_flag is False or str(vision_flag).lower() in ("false", "off", "no"):
            return f"profile「{prof_name}」标了不能用图（vision = false）。换一个 profile 再看。"

        key = prof.api_key or os.environ.get(prof.api_key_env, "")
        if not key:
            return f"profile「{prof_name}」没有 API key，看不了图。"

        payload = base64.b64encode(data).decode("ascii")
        level = pick_detail(question, detail)
        client = OpenAI(api_key=key, base_url=prof.base_url, timeout=_TIMEOUT)
        try:
            resp = client.chat.completions.create(
                model=prof.name,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": question},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{payload}", "detail": level},
                        },
                    ],
                }],
            )
        except Exception as e:
            return explain_failure(e, prof.name)

        text = (resp.choices[0].message.content or "").strip()
        if not text:
            return "（模型没有返回内容）"
        if level == "low":
            # 告诉模型"这次看的是缩略图"——它才有机会在有疑问时再要一次高清，
            # 而不是拿着一张 512×512 的图硬猜小字。
            return f"{text}\n（detail=low：512×512 下采样。要看细节就再调一次，传 detail=\"high\"）"
        return text

    return view_image
