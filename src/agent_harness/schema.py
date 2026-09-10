"""工具 schema：真模型的工具菜单（name/description/parameters JSON）。

描述质量直接决定模型调用正确率——参数名必须和函数签名一字不差
（**action.args 的键名契约），description 告诉模型何时用、参数什么含义。
"""

from .tools import read_file, write_file, count_lines, run_shell

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取文件全部内容。读代码/配置/任何文本文件前用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径（工作目录内相对或绝对）"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "写入文件（⚠️ 覆盖整个文件——未包含的原内容会丢失）。只有新建文件或整体重写时用；修改现有文件优先用 edit_file。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径（工作目录内）"},
                    "content": {"type": "string", "description": "完整文件内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "精确替换文件里的一段文本（old→new），保留其余内容——修改现有代码的首选。old 必须在文件中唯一出现（先 read_file 拿到准确原文）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径（工作目录内）"},
                    "old": {"type": "string", "description": "要被替换的原文（必须与文件内容一字不差且唯一）"},
                    "new": {"type": "string", "description": "替换后的新文本"},
                },
                "required": ["path", "old", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "count_lines",
            "description": "统计文件行数。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "执行 shell 命令（工作目录内）。查看文件列表、跑构建等。危险命令会被拦截并询问用户。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的完整命令"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": "跑 pytest 并返回红绿报告（失败时只给输出尾部摘要，比 run_shell 更省上下文）。改完代码后用它验证——红了看报告改，绿了才收工。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "测试路径，默认 '.' 跑全部"},
                },
                "required": [],
            },
        },
    },
]

