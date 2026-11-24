"""POST /ask 핸들러(src/ask/ask.py) 단위 테스트. AWS를 부르지 않습니다.

Lambda에서는 Handler가 ask/ask.handler라서 이 파일이 ask.ask로 불립니다. 테스트도 같은 이름으로 불러옵니다.
"""

import base64
import json
import logging
import time
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

import kb_search
from ask import ask
from kb_search import answer as qa
from kb_search import core

TOKEN = "eyJraWQiOiJzZWNyZXQtdG9rZW4ifQ.payload.signature"
EMAIL = "member@example.com"
ACCOUNT = "123456789012"


def make_event(body=None, groups="[ana]", raw_body=None, b64=False):
    claims = {"sub": "abc-123", "email": EMAIL, "username": EMAIL, "client_id": "webclient"}
    if groups is not None:
        claims["cognito:groups"] = groups
    text = raw_body if raw_body is not None else json.dumps(body, ensure_ascii=False)
    if b64:
        text = base64.b64encode(text.encode("utf-8")).decode()
    return {
        "version": "2.0",
        "routeKey": "POST /ask",
        "headers": {"authorization": f"Bearer {TOKEN}", "content-type": "application/json"},
        "requestContext": {"authorizer": {"jwt": {"claims": claims, "scopes": None}}, "http": {"method": "POST"}},
        "body": text,
        "isBase64Encoded": b64,
    }


def context(remaining_ms=29_000):
    return SimpleNamespace(get_remaining_time_in_millis=lambda: remaining_ms)


def call(event, ctx=None):
    resp = ask.handler(event, ctx or context())
    return resp["statusCode"], json.loads(resp["body"]), resp


GOOD = {
    "answer": "사용할 수 없습니다 [1].",
    "sources": [{"n": 1, "kind": "school_rule", "title": "제4조", "detail": "제1042호", "url": "https://x", "text": "t"}],
    "searched": ["school", "club-ana"],
    "no_answer": False,
    "info": {"failed": [], "evidence": 8, "search_ms": 300, "model_ms": 2000, "stop": "end_turn",
             "tokens": {"in": 3000, "out": 100}},
}


@pytest.fixture
def fake_answer(monkeypatch):
    calls = []

    def fake(question, groups, mode="qa", deadline=None):
        calls.append({"question": question, "groups": groups, "mode": mode, "deadline": deadline})
        return fake.result

    fake.result = GOOD
    fake.calls = calls
    monkeypatch.setattr(qa, "answer", fake)
    return fake


# ---------------------------------------------------------------- 그룹 클레임

@pytest.mark.parametrize("claim, expected", [
    (["ana", "other"], ["ana", "other"]),       # 배열
    ("[ana other]", ["ana", "other"]),          # HTTP API가 줄인 문자열
    ("[ana]", ["ana"]),
    ('["ana","other"]', ["ana", "other"]),      # JSON 문자열
    ("ana", ["ana"]),
    ("[]", []),
    ("", []),
    (None, []),
    ([1, None, " ", "ana"], ["ana"]),
])
def test_groups_claim_formats(claim, expected):
    assert ask.caller_groups(make_event({"question": "q"}, groups=claim)) == expected


def test_groups_without_authorizer_context():
    assert ask.caller_groups({"body": "{}"}) == []


def test_groups_are_passed_to_answer(fake_answer):
    call(make_event({"question": "전기난로?"}, groups="[ana other]"))
    assert fake_answer.calls[0]["groups"] == ["ana", "other"]


# ---------------------------------------------------------------- 요청 검증

@pytest.mark.parametrize("raw, message", [
    ("", "요청 본문은 JSON 객체로 보내 주세요."),
    ("not json", "요청 본문은 JSON 객체로 보내 주세요."),
    ('["question"]', "요청 본문은 JSON 객체로 보내 주세요."),
    ("{}", "질문을 적어 주세요."),
    ('{"question": "   "}', "질문을 적어 주세요."),
    ('{"question": 3}', "질문을 적어 주세요."),
    ('{"question": "q", "mode": "chat"}', "mode는 qa 또는 event로 보내 주세요."),
])
def test_bad_requests(fake_answer, raw, message):
    status, body, _ = call(make_event(raw_body=raw))
    assert (status, body) == (400, {"error": message})
    assert fake_answer.calls == []


def test_question_length_limit(fake_answer):
    status, body, _ = call(make_event({"question": "가" * 1001}))
    assert (status, body) == (400, {"error": "질문을 1,000자 안으로 적어 주세요."})
    status, _, _ = call(make_event({"question": "가" * 1000}))
    assert status == 200


def test_question_is_trimmed_and_mode_defaults_to_qa(fake_answer):
    call(make_event({"question": "  전기난로?  "}))
    assert fake_answer.calls[0]["question"] == "전기난로?" and fake_answer.calls[0]["mode"] == "qa"


def test_event_mode_and_base64_body(fake_answer):
    status, _, _ = call(make_event({"question": "[행사 기획] 행사 종류: MT", "mode": "event"}, b64=True))
    assert status == 200 and fake_answer.calls[0]["mode"] == "event"


def test_invalid_request_from_search_is_400(monkeypatch):
    def raise_invalid(*a, **kw):
        raise kb_search.InvalidRequest("검색할 질문을 적어 주세요.")
    monkeypatch.setattr(qa, "answer", raise_invalid)
    status, body, _ = call(make_event({"question": "q"}))
    assert (status, body) == (400, {"error": "검색할 질문을 적어 주세요."})


# ---------------------------------------------------------------- 응답

def test_success_response_shape(fake_answer):
    status, body, resp = call(make_event({"question": "전기난로?"}))
    assert status == 200
    assert set(body) == {"answer", "sources", "searched", "no_answer", "ms"}
    assert body["answer"] == GOOD["answer"] and body["sources"] == GOOD["sources"]
    assert body["searched"] == ["school", "club-ana"] and body["no_answer"] is False
    assert isinstance(body["ms"], int)
    assert resp["headers"]["content-type"].startswith("application/json")
    assert resp["headers"]["cache-control"] == "no-store"


def test_deadline_comes_from_lambda_remaining_time(fake_answer):
    before = time.monotonic()
    call(make_event({"question": "q"}), context(20_000))
    deadline = fake_answer.calls[0]["deadline"]
    assert before + 19.5 < deadline < time.monotonic() + 20.5


@pytest.mark.parametrize("error", [
    ClientError({"Error": {"Code": "AccessDeniedException",
                           "Message": f"arn:aws:iam::{ACCOUNT}:role/ask is not authorized"}}, "Converse"),
    qa.AnswerError("all retrieve failed: ['school']"),
    RuntimeError(f"boom {ACCOUNT}"),
])
def test_errors_return_generic_500_and_log_redacted(monkeypatch, caplog, error):
    def boom(*a, **kw):
        raise error
    monkeypatch.setattr(qa, "answer", boom)
    with caplog.at_level(logging.INFO):
        status, body, _ = call(make_event({"question": "q"}))
    assert (status, body) == (500, {"error": ask.GENERIC_ERROR})
    logs = caplog.text
    assert '"ask": "error"' in logs and ACCOUNT not in logs


# ---------------------------------------------------------------- 로그

def last_log(caplog):
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith('{"ask"')]
    return json.loads(lines[-1])


def test_log_fields_without_question_when_answered(fake_answer, caplog):
    with caplog.at_level(logging.INFO):
        call(make_event({"question": "전기난로를 써도 되나요?"}))
    log = last_log(caplog)
    assert log["groups"] == ["ana"] and log["searched"] == ["school", "club-ana"]
    assert log["sources"] == 1 and log["no_answer"] is False and "ms" in log
    assert log["search_ms"] == 300 and log["model_ms"] == 2000
    assert "question" not in log
    assert "전기난로" not in caplog.text


def test_question_logged_only_when_no_answer(fake_answer, caplog):
    fake_answer.result = {**GOOD, "answer": qa.NO_ANSWER, "sources": [], "no_answer": True}
    with caplog.at_level(logging.INFO):
        call(make_event({"question": "외부 MT 신청은?"}))
    log = last_log(caplog)
    assert log["no_answer"] is True and log["question"] == "외부 MT 신청은?"


def test_logs_never_contain_token_email_or_account(monkeypatch, caplog):
    # 실제 answer 모듈을 가짜 검색·가짜 Bedrock과 함께 돌려서 전체 로그를 확인합니다.
    monkeypatch.setattr(core, "search", lambda q, g, sources=None, per_kb=4: {
        "query": q, "skipped": [],
        "groups": [{"kb_key": "school", "name": "학교", "results": [
            {"doc_id": "school/kb/a.md", "kind": "school_rule", "title": "제4조", "detail": "", "url": "",
             "text": "근거", "score": 0.6}]}]})

    class FakeBedrock:
        def converse(self, **kw):
            return {"output": {"message": {"content": [{"text": qa.NO_ANSWER}]}}, "stopReason": "end_turn",
                    "usage": {"inputTokens": 1, "outputTokens": 1}}

    monkeypatch.setattr(qa, "_bedrock", lambda read_timeout: FakeBedrock())
    with caplog.at_level(logging.DEBUG):
        status, body, _ = call(make_event({"question": "외부 MT 신청은?"}))
    assert status == 200 and body["no_answer"] is True and body["sources"] == []
    for secret in (TOKEN, "eyJ", EMAIL, ACCOUNT, "abc-123"):
        assert secret not in caplog.text
