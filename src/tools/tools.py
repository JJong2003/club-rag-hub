"""AgentCore Gateway가 부르는 MCP 도구 Lambda.

MCP 통신과 로그인 확인은 Gateway가 맡고, 도구가 불리면 Gateway가 이 함수를 부릅니다.
- event: 도구 입력값. Gateway에 등록한 도구 설명(inputSchema)의 속성이 그대로 들어옵니다.
  여기에 요청 인터셉터(src/interceptor/)가 로그인한 사람의 그룹을 caller_groups로 넣어 줍니다.
- context.client_context.custom["bedrockAgentCoreToolName"]: "<대상 이름>___<도구 이름>" 꼴 (밑줄 3개)

caller_groups는 도구 설명에 넣지 않습니다. LLM이 볼 필요가 없고, 클라이언트가 넣어 보내도
인터셉터가 토큰 값으로 덮어씁니다. 값이 없으면 그룹이 없는 사람으로 봅니다 (공개 자료만).

지금은 로그인 연결 시험용 ping 도구만 있습니다. 검색 도구는 다음 단계에서 추가합니다.
응답과 로그에 토큰, 메일 주소, 계정 ID를 넣지 않습니다.
"""

import json
import logging
import os

LOG = logging.getLogger()
LOG.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

TOOL_DELIMITER = "___"
CALLER_GROUPS = "caller_groups"  # 인터셉터(src/interceptor/interceptor.py)와 같은 이름이어야 합니다.


def tool_name(context):
    """Gateway가 붙인 대상 이름을 떼고 도구 이름만 돌려줍니다."""
    client_context = getattr(context, "client_context", None)
    custom = (getattr(client_context, "custom", None) or {}) if client_context else {}
    full = custom.get("bedrockAgentCoreToolName", "")
    return full.split(TOOL_DELIMITER, 1)[1] if TOOL_DELIMITER in full else full


def split_caller(event):
    """입력값에서 caller_groups를 떼어 냅니다. (도구 입력값, 그룹 목록)을 돌려줍니다."""
    args = dict(event or {})
    groups = args.pop(CALLER_GROUPS, None)
    if not isinstance(groups, list):
        groups = []
    return args, sorted({g for g in groups if isinstance(g, str)})


def ping(args, groups):
    """연결 시험: 입력값이 오는지, 로그인한 사람의 그룹이 Lambda까지 오는지 돌려줍니다."""
    return {
        "status": "connected",
        "service": "club-rag-hub",
        "echo": str(args.get("message", ""))[:200],
        "login": {"groups": groups},
    }


TOOLS = {"ping": ping}


def handler(event, context):
    name = tool_name(context)
    args, groups = split_caller(event)
    LOG.info(json.dumps({"tool": name, "args": sorted(args), "groups": groups}, ensure_ascii=False))
    fn = TOOLS.get(name)
    if fn is None:
        return {"error": f"알 수 없는 도구입니다: {name}"}
    return fn(args, groups)
