"""근거 답변(kb_search.answer) 단위 테스트. AWS를 부르지 않고 가짜 검색과 가짜 Bedrock으로 확인합니다."""

import time

import pytest

import kb_search
from kb_search import answer, core


def doc(title, text="근거 문단", kind="school_rule", detail="제1042호, 2001-10-29 개정"):
    return {"doc_id": f"x/kb/{title}.md", "kind": kind, "title": title, "detail": detail,
            "url": "https://example.com/" + title, "text": text, "score": 0.6}


def search_result(school=None, club=None, club_error=None, school_error=None):
    groups = [{"kb_key": "school", "name": "충남대 학교 규정", "results": school or []}]
    if school_error:
        groups[0]["error"] = school_error
    groups.append({"kb_key": "club-ana", "name": "ANA 동아리 운영 자료", "results": club or []})
    if club_error:
        groups[1]["error"] = club_error
    return {"query": "질문", "groups": groups, "skipped": []}


DEFAULT_RESULT = search_result(
    school=[doc("제4조", "전열기구 승인 범위"), doc("제58조", "공동 운영"), doc("빈 조문", "  ")],
    club=[doc("여름 2차 회의", "MT 장소 후보", kind="club_manual", detail="2026")],
)


class FakeBedrock:
    def __init__(self, text="답 [1].", stop="end_turn"):
        self.text = text
        self.stop = stop
        self.calls = []

    def converse(self, **kw):
        self.calls.append(kw)
        return {"output": {"message": {"role": "assistant", "content": [{"text": self.text}]}},
                "stopReason": self.stop, "usage": {"inputTokens": 100, "outputTokens": 20}}


@pytest.fixture
def fake(monkeypatch):
    state = {"result": DEFAULT_RESULT, "search_calls": [], "bedrock": FakeBedrock(), "read_timeouts": []}

    def fake_search(query, groups, sources=None, per_kb=4):
        state["search_calls"].append({"query": query, "groups": groups, "per_kb": per_kb})
        return {**state["result"], "query": query}

    def fake_bedrock(read_timeout):
        state["read_timeouts"].append(read_timeout)
        return state["bedrock"]

    monkeypatch.setattr(core, "search", fake_search)
    monkeypatch.setattr(answer, "_bedrock", fake_bedrock)
    monkeypatch.delenv("ANSWER_MODEL_ID", raising=False)
    return state


# ---------------------------------------------------------------- 근거와 번호

def test_evidence_numbers_follow_kb_order_and_skip_empty_text():
    ev = answer.collect_evidence(DEFAULT_RESULT)
    assert [(e["n"], e["kb_key"], e["title"]) for e in ev] == [
        (1, "school", "제4조"), (2, "school", "제58조"), (3, "club-ana", "여름 2차 회의")]


def test_evidence_text_is_clipped():
    ev = answer.collect_evidence(search_result(school=[doc("긴 조문", "가" * 5000)]))
    assert len(ev[0]["text"]) == answer.MAX_EVIDENCE_CHARS + 1 and ev[0]["text"].endswith("…")


def test_user_message_has_numbered_evidence_and_question():
    msg = answer.build_user_message("전기난로?", answer.collect_evidence(DEFAULT_RESULT))
    assert "[1] 학교 규정 | 제4조 | 제1042호, 2001-10-29 개정\n전열기구 승인 범위" in msg
    assert "[3] 동아리 자료 | 여름 2차 회의 | 2026" in msg
    assert msg.endswith("<질문>\n전기난로?\n</질문>")


def test_renumber_keeps_only_used_numbers_in_first_use_order():
    ev = answer.collect_evidence(DEFAULT_RESULT)
    text, sources = answer.renumber("동아리 [3]. 규정 [1, 3]. 없는 번호 [9]. 다시 [3][3].", ev)
    assert text == "동아리 [1]. 규정 [2][1]. 없는 번호. 다시 [1][1]."
    assert [(s["n"], s["title"]) for s in sources] == [(1, "여름 2차 회의"), (2, "제4조")]
    assert set(sources[0]) == {"n", "kind", "title", "detail", "url", "text"}


def test_renumber_without_citations():
    text, sources = answer.renumber("번호 없는 답", answer.collect_evidence(DEFAULT_RESULT))
    assert (text, sources) == ("번호 없는 답", [])


# ---------------------------------------------------------------- answer()

def test_answer_uses_only_cited_sources_and_renumbers(fake):
    fake["bedrock"].text = "승인이 필요합니다 [2]. 동아리 기록도 있습니다 [3]."
    r = answer.answer("전기난로?", ["ana"])
    assert r["answer"] == "승인이 필요합니다 [1]. 동아리 기록도 있습니다 [2]."
    assert [(s["n"], s["title"]) for s in r["sources"]] == [(1, "제58조"), (2, "여름 2차 회의")]
    assert r["searched"] == ["school", "club-ana"]
    assert r["no_answer"] is False
    assert fake["search_calls"] == [{"query": "전기난로?", "groups": ["ana"], "per_kb": 4}]


def test_converse_request(fake):
    answer.answer("전기난로?", ["ana"])
    call = fake["bedrock"].calls[0]
    assert call["modelId"] == "global.anthropic.claude-sonnet-4-6"
    assert call["inferenceConfig"]["maxTokens"] == 1200
    assert answer.NO_ANSWER in call["system"][0]["text"]
    assert "관계없는 근거" in call["system"][0]["text"]
    assert "행사 준비" not in call["system"][0]["text"]
    assert "[1] 학교 규정 | 제4조" in call["messages"][0]["content"][0]["text"]


def test_model_id_from_env(fake, monkeypatch):
    monkeypatch.setenv("ANSWER_MODEL_ID", "apac.amazon.nova-lite-v1:0")
    answer.answer("질문", [])
    assert fake["bedrock"].calls[0]["modelId"] == "apac.amazon.nova-lite-v1:0"


def test_event_mode_adds_event_instructions(fake):
    answer.answer("[행사 기획] 행사 종류: MT / 장소: 펜션", ["ana"], mode="event")
    system = fake["bedrock"].calls[0]["system"][0]["text"]
    assert "행사 준비" in system and "비슷한 지난 활동" in system


def test_invalid_mode(fake):
    with pytest.raises(kb_search.InvalidRequest):
        answer.answer("질문", ["ana"], mode="chat")
    assert fake["search_calls"] == []


def test_no_results_skips_model(fake):
    fake["result"] = search_result()
    r = answer.answer("질문", ["ana"])
    assert r == {"answer": answer.NO_ANSWER, "sources": [], "searched": ["school", "club-ana"], "no_answer": True,
                 "info": r["info"]}
    assert fake["bedrock"].calls == []


def test_only_empty_text_results_skip_model(fake):
    fake["result"] = search_result(school=[doc("빈 조문", "")])
    assert answer.answer("질문", ["ana"])["no_answer"] is True
    assert fake["bedrock"].calls == []


def test_model_says_not_found(fake):
    fake["bedrock"].text = answer.NO_ANSWER
    r = answer.answer("외부 MT 신청?", ["ana"])
    assert (r["answer"], r["sources"], r["no_answer"]) == (answer.NO_ANSWER, [], True)


def test_answer_without_valid_citations_is_not_shown(fake):
    # 근거 번호가 없거나 없는 번호만 쓴 답은 근거 없는 답이라 안내 문구로 바꿉니다.
    fake["bedrock"].text = "아마 학생과에 신청서를 내면 됩니다 [7]."
    r = answer.answer("질문", ["ana"])
    assert (r["answer"], r["sources"], r["no_answer"]) == (answer.NO_ANSWER, [], True)


def test_partial_answer_with_not_found_sentence_is_an_answer(fake):
    fake["bedrock"].text = "동아리에서는 펜션을 썼습니다 [3]. 학교 규정에서는 찾지 못했습니다."
    r = answer.answer("질문", ["ana"])
    assert r["no_answer"] is False and len(r["sources"]) == 1


def test_truncated_answer_gets_note(fake):
    fake["bedrock"].text, fake["bedrock"].stop = "긴 답 [1]", "max_tokens"
    assert answer.answer("질문", ["ana"])["answer"].endswith(answer.TRUNCATED_NOTE)


def test_filtered_answer_is_no_answer(fake):
    fake["bedrock"].text, fake["bedrock"].stop = "답 [1]", "content_filtered"
    assert answer.answer("질문", ["ana"])["no_answer"] is True


def test_one_kb_failed_still_answers(fake):
    fake["result"] = search_result(school=[doc("제4조")], club_error="ThrottlingException")
    r = answer.answer("질문", ["ana"])
    assert r["searched"] == ["school"] and r["info"]["failed"] == ["club-ana"]


def test_all_kbs_failed_is_an_error(fake):
    # 모두 실패했는데 '찾지 못했습니다'라고 하면 틀린 안내가 되므로 오류로 처리합니다.
    fake["result"] = search_result(school_error="AccessDeniedException", club_error="AccessDeniedException")
    with pytest.raises(answer.AnswerError):
        answer.answer("질문", ["ana"])
    assert fake["bedrock"].calls == []


def test_no_allowed_kbs_is_no_answer(fake):
    fake["result"] = {"query": "질문", "groups": [], "skipped": []}
    r = answer.answer("질문", [])
    assert r["no_answer"] is True and r["searched"] == []


# ---------------------------------------------------------------- 시간 예산

def test_model_waits_full_limit_when_time_is_enough(fake):
    answer.answer("질문", ["ana"], deadline=time.monotonic() + 29)
    assert fake["read_timeouts"] == [answer.MODEL_READ_LIMIT]


def test_model_read_timeout_shrinks_with_remaining_time(fake):
    # 남은 10초 - 여유 1.5초 - 연결 3초 = 5.5초 → 5초
    answer.answer("질문", ["ana"], deadline=time.monotonic() + 10)
    assert fake["read_timeouts"] == [5]


def test_too_little_time_skips_search_and_model(fake):
    with pytest.raises(answer.AnswerError):
        answer.answer("질문", ["ana"], deadline=time.monotonic() + 3)
    assert fake["search_calls"] == [] and fake["bedrock"].calls == []


def test_slow_search_gives_up(fake, monkeypatch):
    monkeypatch.setattr(answer, "SEARCH_LIMIT", 0.05)

    def slow_search(*a, **kw):
        time.sleep(0.5)
        return DEFAULT_RESULT

    monkeypatch.setattr(core, "search", slow_search)
    started = time.monotonic()
    with pytest.raises(answer.AnswerError):
        answer.answer("질문", ["ana"])
    assert time.monotonic() - started < 0.4
    assert fake["bedrock"].calls == []


def test_error_summary_hides_account_id():
    from botocore.exceptions import ClientError
    e = ClientError({"Error": {"Code": "AccessDeniedException",
                               "Message": "arn:aws:iam::123456789012:role/x is not authorized"}}, "Converse")
    s = answer.error_summary(e)
    assert s["error"] == "AccessDeniedException" and "123456789012" not in s["message"]
