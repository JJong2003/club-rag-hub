"""AgentCore Gateway 요청 인터셉터 Lambda.

Gateway는 로그인 토큰(Authorization 헤더)을 도구 Lambda에 넘기지 않습니다 (넘기는 설정도 막혀 있음, 11/24 확인).
대신 요청 인터셉터에게는 헤더를 넘길 수 있습니다 (passRequestHeaders). 그래서 이 함수가 중간에서
토큰의 그룹(cognito:groups)을 꺼내 도구 입력값의 caller_groups에 적어 넣습니다.

  Claude ──토큰──▶ Gateway ① 토큰 검사 ② 이 함수 호출 ⇄ 바뀐 요청 ③ 도구 Lambda 호출

- tools/call만 바꿉니다. initialize, tools/list 같은 다른 요청은 그대로 돌려줍니다.
- 클라이언트가 caller_groups를 직접 넣어 보내도 토큰 값으로 덮어씁니다.
- 토큰 서명은 Gateway가 이미 확인했으므로 여기서는 내용만 읽습니다.
  이 함수는 Gateway 역할만 부를 수 있어서, 꾸민 헤더가 들어올 길이 없습니다.
- 토큰, 메일 주소, 사용자 ID는 로그에 남기지 않습니다. 그룹 이름만 남깁니다.

입출력 형식: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-interceptors-types.html
"""

import base64
import copy
import json
import logging
import os

LOG = logging.getLogger()
LOG.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

CALLER_GROUPS = "caller_groups"  # 도구 Lambda(src/tools/tools.py)와 같은 이름이어야 합니다.
OUTPUT_VERSION = "1.0"


def token_claims(headers):
    """Authorization: Bearer <JWT>의 내용(클레임)을 읽습니다. 없거나 깨졌으면 빈 dict."""
    auth = next((v for k, v in (headers or {}).items() if k.lower() == "authorization"), "")
    if not isinstance(auth, str) or not auth.lower().startswith("bearer "):
        return {}
    try:
        payload = auth.split(" ", 1)[1].strip().split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError):
        return {}
    return claims if isinstance(claims, dict) else {}


def groups_from(claims):
    groups = claims.get("cognito:groups") or []
    return sorted({g for g in groups if isinstance(g, str)}) if isinstance(groups, list) else []


def invalid_params(message_id, text):
    """도구 Lambda까지 보내지 않고 Gateway가 바로 돌려줄 JSON-RPC 오류."""
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": -32602, "message": text}}


def stamp(message, groups):
    """JSON-RPC 메시지 하나를 처리합니다. (바뀐 메시지, 오류 응답) 중 하나를 돌려줍니다."""
    if not isinstance(message, dict) or message.get("method") != "tools/call":
        return message, None
    message = copy.deepcopy(message)
    params = message.get("params")
    if params is None:
        params = message["params"] = {}
    if not isinstance(params, dict):
        return None, invalid_params(message.get("id"), "params는 객체여야 합니다.")
    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return None, invalid_params(message.get("id"), "arguments는 객체여야 합니다.")
    params["arguments"] = {**arguments, CALLER_GROUPS: groups}
    return message, None


def handler(event, context):
    request = ((event or {}).get("mcp") or {}).get("gatewayRequest") or {}
    body = request.get("body")
    headers = request.get("headers") or {}
    groups = groups_from(token_claims(headers))

    # MCP 최신 규격은 요청 묶음(배열)을 쓰지 않지만, 들어오면 하나도 빠짐없이 처리합니다.
    # 하나라도 건너뛰면 그 요청에 클라이언트가 꾸민 caller_groups가 그대로 남기 때문입니다.
    messages = body if isinstance(body, list) else [body]
    stamped, errors = [], []
    for message in messages:
        new, error = stamp(message, groups)
        stamped.append(new)
        if error:
            errors.append(error)

    tool_calls = [m for m in messages if isinstance(m, dict) and m.get("method") == "tools/call"]
    if tool_calls:
        if not headers:
            LOG.warning("헤더가 오지 않았습니다. Gateway 인터셉터 설정의 passRequestHeaders를 확인하세요.")
        LOG.info(json.dumps({"tools": [(m.get("params") or {}).get("name") for m in tool_calls
                                       if isinstance(m.get("params"), dict)],
                             "groups": groups}, ensure_ascii=False))

    if errors:
        return {"interceptorOutputVersion": OUTPUT_VERSION, "mcp": {"transformedGatewayResponse": {
            "statusCode": 200, "body": errors[0] if len(errors) == 1 else errors}}}
    new_body = stamped if isinstance(body, list) else stamped[0]
    return {"interceptorOutputVersion": OUTPUT_VERSION,
            "mcp": {"transformedGatewayRequest": {"body": new_body}}}
