"""S2 单测：反馈校验器（判红绿）+ 触发接线（changed_code）。规格硬标准：脱离 LLM 可验证。"""

from agent_harness.llm import Action, LLM
from agent_harness.loop import agent_loop
from agent_harness.sensors import run_sensors
from agent_harness.tools import write_file


def test_1():  # sensors 判红绿：退出码 0 绿、非 0 红（假命令构造，不依赖真实测试状态）
    green = run_sensors(["python", "-c", "print('ok')"])
    red = run_sensors(["python", "-c", "import sys; sys.exit(1)"])
    assert "测试全部通过" in green
    assert "测试失败" in red


def test_2():  # 动态截断：绿报告不贴输出；红 + 长输出只留尾部 20 行
    green = run_sensors(["python", "-c", "print('x' * 1000)"])
    assert "x" * 10 not in green  # 绿了不贴输出
    red_long = run_sensors(
        ["python", "-c", "import sys; [print('line', i, file=sys.stderr) for i in range(100)]; sys.exit(1)"]
    )
    assert len(red_long.splitlines()) <= 21  # 标题 1 行 + 尾部 20 行


def test_3(tmp_path):  # changed_code=True → 触发 sensor，报告进历史
    sensor = lambda: "SENSOR_REPORT"
    tools = {"write_file": write_file}
    llm = LLM(
        [Action("call_tool", "write_file", {"path": str(tmp_path / "x.py"), "content": "y"}, changed_code=True)]
    )
    _, history = agent_loop("干活", llm, tools, "sys", sensor=sensor)
    assert any("SENSOR_REPORT" in c for r, c in history if r == "user")


def test_4(tmp_path):  # 反向：不声明 changed_code → 不触发
    sensor = lambda: "SENSOR_REPORT"
    tools = {"write_file": write_file}
    llm = LLM([Action("call_tool", "write_file", {"path": str(tmp_path / "x.py"), "content": "y"})])
    _, history = agent_loop("干活", llm, tools, "sys", sensor=sensor)
    assert not any("SENSOR_REPORT" in c for r, c in history)
