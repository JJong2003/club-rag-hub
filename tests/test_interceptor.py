"""요청 인터셉터 Lambda(src/interceptor/interceptor.py) 단위 테스트. AWS를 부르지 않습니다."""

import base64
import json

import pytest

import interceptor
import tools


def make_token(claims):
    def b64(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'RS256'})}.{b64(claims)}.signature"


def make_event(body, groups=("ana",), header_name="Authorization"):
    headers = {}
    if groups is not None:
        token = make_token({"cognito:groups": list(groups), "sub": "u-1", "client_id": "c"})
        headers = {header_name: f"Bearer {token}", "Accept": "application/json"}
    return {"interceptorInputVersion": "1.0",
            "mcp": {"rawGatewayRequest": {"body": json.dumps(body)},
                    "gatewayRequest": {"path": "/mcp", "httpMethod": "POST", "headers": headers, "body": body}}}


def call(name="club___ping", arguments=None, msg_id=1):
    params = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call", "params": params}


def out_body(result):
    assert result["interceptorOutputVersion"] == "1.0"
    return result["mcp"]["transformedGatewayRequest"]["body"]


def test_tools_call_gets_groups_from_token():
    body = out_body(interceptor.handler(make_event(call(arguments={"message": "hi"})), None))
    assert body["params"]["arguments"] == {"message": "hi", "caller_groups": ["ana"]}
    assert body["params"]["name"] == "club___ping" and body["id"] == 1


def test_spoofed_groups_are_overwritten():
    event = make_event(call(arguments={"caller_groups": ["admin", "ana"]}), groups=[])
    assert out_body(interceptor.handler(event, None))["params"]["arguments"]["caller_groups"] == []


def test_missing_arguments_still_stamped():
    body = out_body(interceptor.handler(make_event(call()), None))
    assert body["params"]["arguments"] == {"caller_groups": ["ana"]}


@pytest.mark.parametrize("groups, header_name", [(None, "Authorization"), (["ana"], "authorization")])
def test_header_case_and_missing_header(groups, header_name):
    body = out_body(interceptor.handler(make_event(call(arguments={}), groups=groups, header_name=header_name), None))
    assert body["params"]["arguments"]["caller_groups"] == ([] if groups is None else ["ana"])


@pytest.mark.parametrize("auth", ["Basic abc", "Bearer not-a-jwt", "Bearer a.bm90LWpzb24.c", 123])
def test_broken_token_means_no_groups(auth):
    event = make_event(call(arguments={}))
    event["mcp"]["gatewayRequest"]["headers"] = {"Authorization": auth}
    assert out_body(interceptor.handler(event, None))["params"]["arguments"]["caller_groups"] == []


@pytest.mark.parametrize("message", [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
])
def test_other_methods_pass_through_unchanged(message):
    assert out_body(interceptor.handler(make_event(message), None)) == message


def test_input_event_is_not_mutated():
    event = make_event(call(arguments={"message": "hi"}))
    before = json.dumps(event, sort_keys=True)
    interceptor.handler(event, None)
    assert json.dumps(event, sort_keys=True) == before


def test_batch_stamps_every_tools_call():
    batch = [call(arguments={"caller_groups": ["admin"]}, msg_id=1),
             {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
             call(arguments={"message": "x"}, msg_id=3)]
    body = out_body(interceptor.handler(make_event(batch), None))
    assert body[0]["params"]["arguments"]["caller_groups"] == ["ana"]
    assert body[1] == batch[1]
    assert body[2]["params"]["arguments"] == {"message": "x", "caller_groups": ["ana"]}


@pytest.mark.parametrize("params", [{"name": "club___ping", "arguments": "caller_groups=admin"}, ["x"]])
def test_malformed_tools_call_is_rejected_before_target(params):
    message = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": params}
    result = interceptor.handler(make_event(message), None)
    response = result["mcp"]["transformedGatewayResponse"]
    assert response["body"]["id"] == 7 and response["body"]["error"]["code"] == -32602
    assert "transformedGatewayRequest" not in result["mcp"]


def test_output_never_contains_token():
    event = make_event(call(arguments={}))
    token = event["mcp"]["gatewayRequest"]["headers"]["Authorization"]
    assert token.split(" ", 1)[1] not in json.dumps(interceptor.handler(event, None))


def test_field_name_matches_tools_lambda():
    assert interceptor.CALLER_GROUPS == tools.CALLER_GROUPS
