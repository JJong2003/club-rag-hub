"""AgentCore Gateway가 부르는 MCP 도구 Lambda.

MCP 통신과 로그인 확인은 Gateway가 맡고, 도구가 불리면 Gateway가 이 함수를 부릅니다.
- event: 도구 입력값. Gateway에 등록한 도구 설명(tool_schema.json)의 속성이 그대로 들어옵니다.
  여기에 요청 인터셉터(src/interceptor/)가 로그인한 사람의 그룹을 caller_groups로 넣어 줍니다.
- context.client_context.custom["bedrockAgentCoreToolName"]: "<대상 이름>___<도구 이름>" 꼴 (밑줄 3개)

도구: ping(연결 시험), list_sources, search, read_document (검색 핵심 src/kb_search/를 부름)

그룹은 caller_groups만 씁니다. caller_groups는 도구 설명에 넣지 않고(LLM이 볼 필요가 없음),
클라이언트가 넣어 보내도 인터셉터가 토큰 값으로 덮어씁니다. 값이 없으면 그룹이 없는 사람으로 봅니다 (공개 자료만).
입력에 다른 이름(groups 등)으로 그룹이 와도 읽지 않습니다.

Lambda에서는 이 파일이 tools.tools 모듈입니다 (CodeUri src/, Handler tools/tools.handler).
테스트에서는 tools라서, 같은 폴더의 다른 파일을 `import 이름`으로 부르지 않습니다. 같이 쓰는 코드는 kb_search처럼 src/ 아래 패키지로 둡니다.

오류: 잘못된 입력(kb_search.InvalidRequest)은 그 문구를 {"error": ...}로 돌려줍니다.
그 밖의 오류는 일반 문구만 돌려주고 원문은 계정 ID를 가려 로그에만 남깁니다.
응답과 로그에 토큰, 메일 주소, 계정 ID를 넣지 않습니다.
"""

import json
import logging
import os
import re
import traceback

import kb_search

LOG = logging.getLogger()
LOG.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

TOOL_DELIMITER = "___"
CALLER_GROUPS = "caller_groups"  # 인터셉터(src/interceptor/interceptor.py)와 같은 이름이어야 합니다.

# 결과 길이 제한: LLM 대화창이 넘치지 않게 합니다. kb_search도 묶음당 10개, 문서 2만 자를 지키지만 여기서 한 번 더 막습니다.
MAX_PER_KB = 10           # search 묶음(KB)당 결과 수
MAX_RESULT_CHARS = 3_000  # search 결과 하나의 본문 (11/24 자료에서 가장 긴 학교 조문이 약 2,600자)
MAX_TEXT_CHARS = 20_000   # 응답 하나의 본문 합계 (search 전체, read_document 문서 하나)
GENERIC_ERROR = "자료를 찾는 중 오류가 났습니다. 잠시 후 다시 시도해 주세요."


def tool_name(context):
    """Gateway가 붙인 대상 이름을 떼고 도구 이름만 돌려줍니다."""
    client_context = getattr(context, "client_context", None)
    custom = (getattr(client_context, "custom", None) or {}) if client_context else {}
    full = custom.get("bedrockAgentCoreToolName", "")
    return full.split(TOOL_DELIMITER, 1)[1] if TOOL_DELIMITER in full else full


def split_caller(event):
    """입력값에서 caller_groups를 떼어 냅니다. (도구 입력값, 그룹 목록)을 돌려줍니다."""
    args = dict(event) if isinstance(event, dict) else {}
    groups = args.pop(CALLER_GROUPS, None)
    if not isinstance(groups, list):
        groups = []
    return args, sorted({g for g in groups if isinstance(g, str)})


def _redact(text):
    """오류 원문에 든 계정 ID(12자리 숫자)를 가립니다."""
    return re.sub(r"\d{12}", "************", str(text))


# ---------------------------------------------------------------- 도구

def ping(args, groups):
    """연결 시험: 입력값이 오는지, 로그인한 사람의 그룹이 Lambda까지 오는지 돌려줍니다."""
    return {
        "status": "connected",
        "service": "club-rag-hub",
        "echo": str(args.get("message", ""))[:200],
        "login": {"groups": groups},
    }


def list_sources(args, groups):
    """볼 수 있는 KB 목록: {"sources": [{"kb_key", "name", "description"}]}"""
    return {"sources": kb_search.list_sources(groups)}


def search(args, groups):
    """KB별로 묶은 검색 결과. 형식은 kb_search.search와 같고 길이만 줄입니다 (fit_search)."""
    options = {"sources": args.get("sources")}
    if args.get("per_kb") is not None:  # 비우면 kb_search 기본값(4)
        options["per_kb"] = args["per_kb"]
    return fit_search(kb_search.search(args.get("query"), groups, **options))


def read_document(args, groups):
    """문서 전체 (최대 2만 자). 형식은 kb_search.read_document와 같습니다."""
    doc = kb_search.read_document(args.get("doc_id"), groups)
    text = str(doc.get("text") or "")
    if len(text) > MAX_TEXT_CHARS:
        doc["text"] = text[:MAX_TEXT_CHARS]
        doc["truncated"] = True
    return doc


def fit_search(result):
    """search 결과 길이를 줄입니다: 묶음당 10개, 결과 하나 3,000자, 본문 합계 2만 자.

    합계는 KB마다 1등부터 번갈아 셉니다. 그래서 넘쳐도 각 KB의 윗순위 결과는 남습니다.
    본문을 자른 결과에는 truncated: true를 붙입니다 (read_document로 전체를 읽을 수 있음).
    """
    groups = result.get("groups") or []
    for g in groups:
        g["results"] = list(g.get("results") or [])[:MAX_PER_KB]
    budget = MAX_TEXT_CHARS
    for rank in range(MAX_PER_KB):
        for g in groups:
            if rank >= len(g["results"]):
                continue
            doc = g["results"][rank]
            text = str(doc.get("text") or "")
            kept = text[:max(0, min(MAX_RESULT_CHARS, budget))]
            if len(kept) < len(text):
                doc["text"] = kept
                doc["truncated"] = True
            budget -= len(kept)
    return result


TOOLS = {
    "ping": ping,
    "list_sources": list_sources,
    "search": search,
    "read_document": read_document,
}


def handler(event, context):
    name = tool_name(context)
    args, groups = split_caller(event)
    LOG.info(json.dumps({"tool": name, "args": sorted(args), "groups": groups}, ensure_ascii=False))
    fn = TOOLS.get(name)
    if fn is None:
        return {"error": f"알 수 없는 도구입니다: {name}"}
    try:
        return fn(args, groups)
    except kb_search.InvalidRequest as e:
        # kb_search가 직접 만든 문구라 사용자에게 그대로 보여도 됩니다.
        LOG.info(json.dumps({"tool": name, "invalid": _redact(e)}, ensure_ascii=False))
        return {"error": str(e)}
    except Exception as e:
        LOG.error(json.dumps({"tool": name, "error": type(e).__name__,
                              "detail": _redact("".join(traceback.format_exception(e)))}, ensure_ascii=False))
        return {"error": GENERIC_ERROR}
