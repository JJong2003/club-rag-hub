"""POST /ask: 로그인한 사람의 그룹으로 KB를 검색하고 Claude가 출처 번호를 붙여 답합니다.

HTTP API(페이로드 2.0)가 부릅니다. 토큰 서명과 만료는 앞단의 JWT 권한 부여자가 확인하므로
여기까지 온 요청은 로그인한 사람의 것입니다. 답변 만드는 일은 kb_search.answer가 합니다.
Lambda에서는 이 파일이 ask.ask로 불리므로 같은 폴더의 이웃 파일을 import하지 않습니다.

요청/응답 형식은 노션 '웹 챗봇 설계 (AWS) > 인터페이스 약속 > POST /ask'를 따릅니다.
- 400: 사용자에게 그대로 보여도 되는 문구
- 500: 일반 문구. 원문 오류는 로그에만 (계정 ID는 가림)
로그에는 그룹, 검색한 KB, 출처 수, 답하지 못함 여부, 걸린 시간만 남깁니다.
질문 원문은 답하지 못한 경우에만 남기고(빠진 자료 찾기용), 토큰·메일·계정 ID는 남기지 않습니다.
"""

import base64
import binascii
import json
import logging
import os
import time

import kb_search
from kb_search import answer as qa

LOG = logging.getLogger()
LOG.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

MAX_QUESTION_CHARS = 1_000
GENERIC_ERROR = "답변을 만들지 못했습니다. 잠시 후 다시 시도해 주세요."
GROUPS_CLAIM = "cognito:groups"


def caller_groups(event):
    """JWT 권한 부여자가 넘긴 클레임에서 그룹을 꺼냅니다.

    HTTP API는 배열 클레임을 ["ana", "other"]가 아니라 "[ana other]" 같은 문자열로 줄 수 있어서 둘 다 받습니다.
    Cognito 그룹 이름에는 공백이 들어갈 수 없으므로 공백으로 나눠도 됩니다.
    """
    claims = (((event.get("requestContext") or {}).get("authorizer") or {}).get("jwt") or {}).get("claims") or {}
    raw = claims.get(GROUPS_CLAIM)
    items = []
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, str):
        text = raw.strip()
        try:
            parsed = json.loads(text)
            items = parsed if isinstance(parsed, list) else [parsed]
        except ValueError:
            items = text.strip("[]").split()
    return sorted({g.strip() for g in items if isinstance(g, str) and g.strip()})


class BadRequest(ValueError):
    pass


def parse_body(event):
    """본문에서 (질문, mode)를 꺼냅니다. 잘못되면 BadRequest(보여 줘도 되는 문구)."""
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try:
            body = base64.b64decode(body).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            raise BadRequest("요청 본문은 JSON 객체로 보내 주세요.") from None
    try:
        data = json.loads(body) if body else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise BadRequest("요청 본문은 JSON 객체로 보내 주세요.")

    question = data.get("question")
    if not isinstance(question, str) or not question.strip():
        raise BadRequest("질문을 적어 주세요.")
    question = question.strip()
    if len(question) > MAX_QUESTION_CHARS:
        raise BadRequest(f"질문을 {MAX_QUESTION_CHARS:,}자 안으로 적어 주세요.")

    mode = data.get("mode") or "qa"
    if mode not in qa.MODES:
        raise BadRequest("mode는 qa 또는 event로 보내 주세요.")
    return question, mode


def _response(status, body):
    # CORS 헤더는 HTTP API의 CORS 설정이 붙입니다 (Lambda가 넣으면 무시됨).
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json; charset=utf-8", "cache-control": "no-store"},
        "body": json.dumps(body, ensure_ascii=False),
    }


def handler(event, context):
    start = time.perf_counter()
    groups = caller_groups(event)
    log = {"ask": "ok", "groups": groups}

    def done(status, body, **extra):
        log.update(extra, status=status, ms=round((time.perf_counter() - start) * 1000))
        (LOG.error if status >= 500 else LOG.info)(json.dumps(log, ensure_ascii=False))
        return _response(status, body)

    try:
        question, mode = parse_body(event)
    except BadRequest as e:
        return done(400, {"error": str(e)}, ask="bad_request", reason=str(e))
    log["mode"] = mode

    # Lambda가 시간 초과로 끊기기 전에 일반 오류라도 돌려주도록 남은 시간을 넘깁니다.
    deadline = None
    remaining_ms = getattr(context, "get_remaining_time_in_millis", None)
    if callable(remaining_ms):
        deadline = time.monotonic() + remaining_ms() / 1000

    try:
        result = qa.answer(question, groups, mode, deadline=deadline)
    except kb_search.InvalidRequest as e:
        return done(400, {"error": str(e)}, ask="bad_request", reason=str(e))
    except Exception as e:  # 검색 전부 실패, Bedrock 오류, 예상 못 한 오류: 원문은 로그에만
        return done(500, {"error": GENERIC_ERROR}, ask="error", **qa.error_summary(e))

    info = result["info"]
    log.update(
        searched=result["searched"],
        failed=info.get("failed", []),
        evidence=info.get("evidence", 0),
        sources=len(result["sources"]),
        no_answer=result["no_answer"],
        search_ms=info.get("search_ms"),
        model_ms=info.get("model_ms"),
        stop=info.get("stop"),
        tokens=info.get("tokens"),
    )
    if result["no_answer"]:
        log["question"] = question  # 답하지 못한 질문만 남겨서 빠진 자료를 찾습니다.
    ms = round((time.perf_counter() - start) * 1000)
    body = {
        "answer": result["answer"],
        "sources": result["sources"],
        "searched": result["searched"],
        "no_answer": result["no_answer"],
        "ms": ms,
    }
    return done(200, body)
