from pathlib import Path
from dataclasses import dataclass

def read_file(path: str) -> str:
    """读取文件内容"""
    return Path(path).read_text(encoding="utf-8")

def write_file(path: str, content: str):
    """写入文件内容"""
    Path(path).write_text(content, encoding="utf-8")
    return f'写入文件 {path} {len(content)} 字符。'

# @dataclass
# class Tool:
#     name: str
#     description: str
#     func: callable

# @dataclass
# class read_file_tool(Tool):
#     name: str = "read_file"
#     description: str = "读取文件内容，参数为文件路径。"
#     func: callable = read_file

# @dataclass
# class write_file_tool(Tool):
#     name: str = "write_file"
#     description: str = "写入文件内容，参数为文件路径和内容。"
#     func: callable = write_file