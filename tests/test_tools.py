"""MCP 도구 Lambda(src/tools/tools.py) 단위 테스트. AWS를 부르지 않습니다."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import tools


def make_context(tool="club___ping"):
    return SimpleNamespace(client_context=SimpleNamespace(custom={"bedrockAgentCoreToolName": tool}))


def load_schema():
    return json.loads((Path(tools.__file__).parent / "tool_schema.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("full, expected", [
    ("club___ping", "ping"),
    ("club___read_document", "read_document"),   # 도구 이름 안의 밑줄은 남김
    ("ping", "ping"),
])
def test_tool_name_strips_target_prefix(full, expected):
    assert tools.tool_name(make_context(full)) == expected


def test_tool_name_without_client_context():
    assert tools.tool_name(SimpleNamespace(client_context=None)) == ""


def test_ping_reports_groups_from_interceptor():
    result = tools.handler({"message": "안녕", "caller_groups": ["ana"]}, make_context())
    assert result == {"status": "connected", "service": "club-rag-hub", "echo": "안녕", "login": {"groups": ["ana"]}}


@pytest.mark.parametrize("value", [None, "ana", {"ana": True}, [1, None]])
def test_missing_or_malformed_groups_mean_no_groups(value):
    event = {} if value is None else {"caller_groups": value}
    assert tools.handler(event, make_context())["login"]["groups"] == []


def test_caller_groups_is_not_passed_as_tool_argument():
    args, groups = tools.split_caller({"query": "회비", "caller_groups": ["ana", "ana"]})
    assert args == {"query": "회비"}
    assert groups == ["ana"]


def test_unknown_tool():
    assert "error" in tools.handler({}, make_context("club___nope"))


def test_tool_schema_matches_handlers():
    """Gateway에 등록하는 도구 설명(tool_schema.json)과 Lambda가 처리하는 도구가 같아야 합니다."""
    schema = load_schema()
    assert {t["name"] for t in schema} == set(tools.TOOLS)
    for t in schema:
        assert t["description"].strip()
        assert t["inputSchema"]["type"] == "object"


def test_tool_schema_does_not_expose_caller_groups():
    """caller_groups는 인터셉터가 채우는 값이라 LLM에게 보이면 안 됩니다."""
    for t in load_schema():
        assert tools.CALLER_GROUPS not in t["inputSchema"].get("properties", {})
