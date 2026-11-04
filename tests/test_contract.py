"""契约测试：schema（模型看到的那份菜单）与工具的真实签名必须对得上。

分工：
- `tests/test_tools.py:16` 守「菜单名字 ↔ 注册表键」同集合；
- 本文件守更细的一层——**参数名**。

为什么值得单测：schema 是手写的字面量，函数签名是另一处真相。把 `path` 写成 `dir`、
把 `old` 写成 `old_text`，静态检查看不出来，运行时表现为「模型按菜单传参 → TypeError」。
这类错一旦漏进版本，代价是每次调用都失败，而现象看起来像是模型不会用工具。

本文件守静态契约；运行期那条路（异常怎么回灌给模型）由 `tests/test_s1.py:test_3b` 守。
"""

import inspect

import pytest

from agent_harness import assembly
from agent_harness.schema import TOOL_SCHEMAS
from agent_harness.subagent import make_spawn_tool

SCHEMA_NAMES = [s["function"]["name"] for s in TOOL_SCHEMAS]


def _callables() -> dict:
    """模型实际能调到的工具表：基础注册表 + 会话级注入的 spawn_subagent。

    spawn_subagent 不在 assembly.tools 里（它要闭包持有会话装配），所以这里现造一个
    只用于取签名的空壳——`make_spawn_tool` 不调用就不会碰 llm / loop。
    """
    tools = dict(assembly.tools)
    tools["spawn_subagent"] = make_spawn_tool(llm=None, tools={}, system_prompt="")
    return tools


def _accepted_params(fn) -> set[str]:
    """函数签名里「能被关键字传进去」的参数名（不含 *args / **kwargs）。"""
    params = inspect.signature(fn).parameters
    return {
        name for name, p in params.items()
        if p.kind not in (p.VAR_KEYWORD, p.VAR_POSITIONAL)
    }


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_schema_params_match_the_signature(name):
    """菜单声明的参数名，必须与函数签名一字不差（多一个 / 少一个都算错）。"""
    schema = next(s for s in TOOL_SCHEMAS if s["function"]["name"] == name)["function"]
    declared = set(schema["parameters"].get("properties", {}))
    accepted = _accepted_params(_callables()[name])
    assert declared == accepted, (
        f"{name}: schema 声明 {sorted(declared)}，函数只接受 {sorted(accepted)}"
        f"（缺的会让模型传不进来，多的会在运行时报 TypeError）"
    )


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_required_is_a_subset_of_properties(name):
    """required 里的键必须都在 properties 里，否则模型无从知道它长什么样。"""
    params = next(s for s in TOOL_SCHEMAS if s["function"]["name"] == name)["function"]["parameters"]
    required = set(params.get("required", []))
    properties = set(params.get("properties", {}))
    assert required <= properties, f"{name}: required 多出 {sorted(required - properties)}"


def test_every_schema_name_is_callable():
    """菜单里出现的每个名字都必须真能调到——否则模型会拿到「未知工具」。"""
    callables = _callables()
    assert set(SCHEMA_NAMES) - set(callables) == set()
