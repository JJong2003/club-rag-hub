"""MCP 도구 Lambda(src/tools/tools.py) 단위 테스트. AWS를 부르지 않습니다.

검색 도구 테스트는 kb_search 함수를 가짜로 바꿔서, 도구가 무엇을 넘기고 무엇을 돌려주는지만 봅니다.
그룹 필터 끝까지 확인하는 테스트 하나만 진짜 kb_search에 가짜 DynamoDB를 붙입니다.
"""

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

import kb_search
import tools
from kb_search import core


def make_context(tool="club___ping"):
    return SimpleNamespace(client_context=SimpleNamespace(custom={"bedrockAgentCoreToolName": tool}))


def call(tool, event):
    return tools.handler(event, make_context(f"club___{tool}"))


def load_schema():
    return json.loads((Path(tools.__file__).parent / "tool_schema.json").read_text(encoding="utf-8"))


def schema_of(name):
    return next(t for t in load_schema() if t["name"] == name)


def doc(doc_id, text="근거 문단"):
    return {"doc_id": doc_id, "kind": "club_manual", "title": "제목", "detail": "2026", "url": "", "text": text,
            "score": 0.5}


class FakeKbSearch:
    """kb_search의 세 함수를 대신합니다. 받은 인자를 calls에 적고, raises가 있으면 그 예외를 냅니다."""

    def __init__(self):
        self.calls = []
        self.raises = None
        self.search_result = {"query": "q", "groups": [], "skipped": []}
        self.document = {"doc_id": "clubs/ana/kb/a.md", "kind": "club_manual", "title": "회의록", "detail": "2026",
                         "url": "", "text": "본문", "truncated": False}

    def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        if self.raises:
            raise self.raises

    def list_sources(self, caller_groups):
        self._record("list_sources", caller_groups)
        return [{"kb_key": "school", "name": "충남대 학교 규정", "description": "학칙 등"}]

    def search(self, query, caller_groups, **kwargs):
        self._record("search", query, caller_groups, **kwargs)
        return self.search_result

    def read_document(self, doc_id, caller_groups):
        self._record("read_document", doc_id, caller_groups)
        return dict(self.document)


@pytest.fixture
def fake(monkeypatch):
    f = FakeKbSearch()
    for name in ("list_sources", "search", "read_document"):
        monkeypatch.setattr(kb_search, name, getattr(f, name))
    return f


# ---------------------------------------------------------------- 도구 이름, 그룹

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


def test_unknown_tool(fake):
    assert "error" in tools.handler({}, make_context("club___nope"))
    assert fake.calls == []


# ---------------------------------------------------------------- 검색 도구: 입력 전달

def test_list_sources_passes_caller_groups(fake):
    result = call("list_sources", {"caller_groups": ["ana"]})
    assert result == {"sources": [{"kb_key": "school", "name": "충남대 학교 규정", "description": "학칙 등"}]}
    assert fake.calls == [("list_sources", (["ana"],), {})]


def test_search_passes_query_sources_per_kb_and_groups(fake):
    call("search", {"query": "회비 규정", "sources": ["school"], "per_kb": 6, "caller_groups": ["ana"]})
    assert fake.calls == [("search", ("회비 규정", ["ana"]), {"sources": ["school"], "per_kb": 6})]


def test_search_leaves_defaults_to_kb_search(fake):
    call("search", {"query": "회비"})
    # sources는 None(전부), per_kb는 넘기지 않아 kb_search 기본값(4)을 씁니다. 그룹이 없으면 빈 목록.
    assert fake.calls == [("search", ("회비", []), {"sources": None})]


def test_read_document_passes_doc_id_and_groups(fake):
    result = call("read_document", {"doc_id": "clubs/ana/kb/a.md", "caller_groups": ["ana"]})
    assert result == fake.document
    assert fake.calls == [("read_document", ("clubs/ana/kb/a.md", ["ana"]), {})]


@pytest.mark.parametrize("tool, args", [
    ("list_sources", {}),
    ("search", {"query": "회비"}),
    ("read_document", {"doc_id": "clubs/ana/kb/a.md"}),
])
@pytest.mark.parametrize("forged", [
    {"groups": ["admin"]},
    {"allowed_groups": ["admin"], "caller_group": ["admin"], "CALLER_GROUPS": ["admin"]},
])
def test_group_values_in_tool_input_are_ignored(fake, tool, args, forged):
    """그룹은 인터셉터가 넣은 caller_groups만 씁니다. 다른 이름으로 온 그룹 값은 kb_search에 가지 않습니다."""
    call(tool, {**args, **forged, "caller_groups": ["ana"]})
    call(tool, {**args, **forged})
    (_, first, first_kw), (_, second, second_kw) = fake.calls
    groups_arg = 1 if tool != "list_sources" else 0
    assert first[groups_arg] == ["ana"]
    assert second[groups_arg] == []
    assert "admin" not in json.dumps([first_kw, second_kw])


def test_forged_groups_do_not_open_club_kb_end_to_end(monkeypatch):
    """진짜 kb_search + 가짜 레지스트리: 입력에 그룹을 꾸며 넣어도 동아리 KB는 caller_groups가 있어야 보입니다."""

    def item(kb_key, prefix, groups):
        return {"kb_key": {"S": kb_key}, "kb_id": {"S": kb_key.upper()}, "s3_prefix": {"S": prefix},
                "allowed_groups": {"SS": groups}, "enabled": {"BOOL": True}, "name": {"S": kb_key}}

    class FakeDynamo:
        def scan(self, **kw):
            return {"Items": [item("school", "school/kb/", ["*"]), item("club-ana", "clubs/ana/kb/", ["ana"])]}

    core.reset_cache()
    monkeypatch.setattr(core, "_client", lambda name: {"dynamodb": FakeDynamo()}[name])
    monkeypatch.setenv("REGISTRY_TABLE", "registry-table")
    try:
        forged = call("list_sources", {"groups": ["ana"], "allowed_groups": ["*"]})
        real = call("list_sources", {"caller_groups": ["ana"]})
    finally:
        core.reset_cache()
    assert [s["kb_key"] for s in forged["sources"]] == ["school"]
    assert [s["kb_key"] for s in real["sources"]] == ["school", "club-ana"]


# ---------------------------------------------------------------- 오류 처리

@pytest.mark.parametrize("tool", ["list_sources", "search", "read_document"])
def test_invalid_request_message_is_returned(fake, tool):
    fake.raises = kb_search.InvalidRequest("검색할 질문을 적어 주세요.")
    assert call(tool, {"query": "", "doc_id": ""}) == {"error": "검색할 질문을 적어 주세요."}


@pytest.mark.parametrize("error", [
    ClientError({"Error": {"Code": "AccessDeniedException",
                           "Message": "User: arn:aws:sts::123456789012:assumed-role/x is not authorized"}}, "Scan"),
    RuntimeError("arn:aws:s3:::club-rag-hub-docs-123456789012 broke"),
])
@pytest.mark.parametrize("tool", ["list_sources", "search", "read_document"])
def test_unexpected_error_is_generic_and_log_hides_account_id(fake, caplog, tool, error):
    fake.raises = error
    with caplog.at_level(logging.INFO):
        result = call(tool, {"query": "회비", "doc_id": "clubs/ana/kb/a.md"})
    assert result == {"error": tools.GENERIC_ERROR}
    assert "123456789012" not in json.dumps(result)
    assert "123456789012" not in caplog.text
    assert type(error).__name__ in caplog.text   # 원문 대신 가린 내용이 로그에 남음
    assert "************" in caplog.text


# ---------------------------------------------------------------- 길이 제한

def test_search_keeps_at_most_10_results_per_kb(fake):
    fake.search_result = {"query": "q", "skipped": [],
                          "groups": [{"kb_key": "school", "name": "학교", "results": [doc(f"d{i}") for i in range(15)]}]}
    result = call("search", {"query": "q"})
    assert [d["doc_id"] for d in result["groups"][0]["results"]] == [f"d{i}" for i in range(10)]


def test_search_cuts_long_text_and_marks_truncated(fake):
    fake.search_result = {"query": "q", "skipped": [], "groups": [
        {"kb_key": "school", "name": "학교", "results": [doc("long", "가" * 5_000), doc("short", "짧은 문단")]}]}
    long_doc, short_doc = call("search", {"query": "q"})["groups"][0]["results"]
    assert long_doc["text"] == "가" * tools.MAX_RESULT_CHARS and long_doc["truncated"] is True
    assert short_doc["text"] == "짧은 문단" and "truncated" not in short_doc


def test_search_total_text_budget_keeps_top_results_of_each_kb(fake):
    def group(key):
        return {"kb_key": key, "name": key, "results": [doc(f"{key}-{i}", "나" * 3_000) for i in range(10)]}

    fake.search_result = {"query": "q", "skipped": [], "groups": [group("school"), group("club-ana")]}
    result = call("search", {"query": "q"})
    school, club = (g["results"] for g in result["groups"])
    assert sum(len(d["text"]) for d in school + club) <= tools.MAX_TEXT_CHARS
    # 넘쳐도 두 KB의 윗순위 결과는 온전히 남고, 뒤쪽 결과는 doc_id만 남아 read_document로 읽을 수 있습니다.
    assert len(school[0]["text"]) == len(club[0]["text"]) == 3_000
    assert "truncated" not in school[0] and "truncated" not in club[0]
    assert school[-1]["text"] == "" and school[-1]["truncated"] is True and school[-1]["doc_id"] == "school-9"


def test_search_keeps_group_errors_and_skipped(fake):
    fake.search_result = {"query": "q", "skipped": ["club-x"],
                          "groups": [{"kb_key": "school", "name": "학교", "results": [], "error": "ThrottlingException"}]}
    assert call("search", {"query": "q"}) == fake.search_result


def test_read_document_text_is_at_most_20000_chars(fake):
    fake.document = {**fake.document, "text": "다" * 25_000, "truncated": False}
    result = call("read_document", {"doc_id": "clubs/ana/kb/a.md"})
    assert len(result["text"]) == tools.MAX_TEXT_CHARS
    assert result["truncated"] is True


# ---------------------------------------------------------------- Lambda에서 불러오는 방식

def test_lambda_imports_handler_as_tools_tools():
    """Lambda는 CodeUri src/, Handler tools/tools.handler라서 이 파일을 tools.tools로 불러옵니다.

    같은 폴더의 이웃 파일을 `import 이름`으로 부르면 테스트는 되지만 Lambda에서는 깨지므로, 그 방식으로 한 번 불러 봅니다.
    """
    src = Path(tools.__file__).resolve().parents[1]
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    code = "import importlib; m = importlib.import_module('tools.tools'); assert callable(m.handler); print(m.__name__)"
    out = subprocess.run([sys.executable, "-c", code], cwd=src, env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "tools.tools"


# ---------------------------------------------------------------- 도구 설명 (tool_schema.json)

def test_tool_schema_matches_handlers():
    """Gateway에 등록하는 도구 설명(tool_schema.json)과 Lambda가 처리하는 도구가 같아야 합니다."""
    schema = load_schema()
    assert [t["name"] for t in schema] == list(tools.TOOLS)
    for t in schema:
        assert t["description"].strip()
        assert t["inputSchema"]["type"] == "object"


def test_tool_schema_does_not_expose_caller_groups():
    """caller_groups는 인터셉터가 채우는 값이라 LLM에게 보이면 안 됩니다."""
    for t in load_schema():
        assert tools.CALLER_GROUPS not in t["inputSchema"].get("properties", {})


GATEWAY_TOOL_KEYS = {"name", "description", "inputSchema", "outputSchema"}
GATEWAY_SCHEMA_KEYS = {"type", "description", "properties", "required", "items"}
GATEWAY_TYPES = {"string", "number", "integer", "boolean", "array", "object"}


def _check_gateway_schema(schema, path):
    assert set(schema) <= GATEWAY_SCHEMA_KEYS, f"{path}: Gateway가 받지 않는 칸 {set(schema) - GATEWAY_SCHEMA_KEYS}"
    assert schema.get("type") in GATEWAY_TYPES, path
    for key, sub in (schema.get("properties") or {}).items():
        _check_gateway_schema(sub, f"{path}.{key}")
    if schema["type"] == "array":
        _check_gateway_schema(schema["items"], f"{path}[]")
    assert set(schema.get("required") or []) <= set(schema.get("properties") or {}), path


def test_tool_schema_uses_only_gateway_fields():
    """Gateway 도구 스키마는 type, description, properties, required, items만 받습니다 (default, minimum 등 없음)."""
    for t in load_schema():
        assert set(t) <= GATEWAY_TOOL_KEYS
        _check_gateway_schema(t["inputSchema"], t["name"])


def test_search_and_read_document_inputs():
    search = schema_of("search")["inputSchema"]
    assert search["required"] == ["query"]
    assert search["properties"]["sources"]["type"] == "array"
    assert search["properties"]["sources"]["items"]["type"] == "string"
    assert search["properties"]["per_kb"]["type"] == "integer"
    read = schema_of("read_document")["inputSchema"]
    assert read["required"] == ["doc_id"] and read["properties"]["doc_id"]["type"] == "string"
    assert schema_of("list_sources")["inputSchema"]["properties"] == {}
