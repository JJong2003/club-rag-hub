#!/usr/bin/env bash
# MCP 서버(AgentCore Gateway) 연결 점검. 리소스를 만들거나 바꾸지 않습니다.
#   1) 토큰 없이 부르면 401과 로그인 안내(WWW-Authenticate)가 오는지
#   2) 로그인 안내 문서(보호 리소스 메타데이터)와 Cognito 설정 문서에 MCP 클라이언트가 찾는 값이 있는지
#   3) 시험 계정 토큰으로 initialize → tools/list → ping 호출이 되는지
#   4) 로그인한 사람의 그룹(ana)이 인터셉터를 거쳐 도구까지 오는지, 꾸민 그룹은 덮어쓰는지
# Claude 앱이 처음 붙을 때 거치는 순서를 그대로 따라갑니다. 토큰과 비밀번호는 출력하지 않습니다.
# pass와 bad는 항상 성공(0)을 돌려주므로 "조건 && pass || bad"가 if-else처럼 동작합니다.
# shellcheck disable=SC2015
source "$(dirname "$0")/lib.sh"

[ -n "${GATEWAY_URL:-}" ] || die "MCP 서버 주소가 없습니다. 08_mcp_gateway.sh를 먼저 실행하세요."
TEST_USER_FILE=".state/mcp-test-user.env"
[ -f "$TEST_USER_FILE" ] || die "시험 계정 정보가 없습니다. 07_cognito.sh를 먼저 실행하세요."
# shellcheck source=/dev/null
source "$TEST_USER_FILE"

PY=python
fail=0
pass() { printf '  [통과] %s\n' "$*" >&2; }
bad()  { printf '  [실패] %s\n' "$*" >&2; fail=1; }

log "1) 토큰 없이 호출 → 401과 로그인 안내"
HDRS="$TMP_DIR/mcp-401.headers"
code="$(curl -sS -o "$TMP_DIR/mcp-401.body" -D "$HDRS" -w '%{http_code}' -X POST "$GATEWAY_URL" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}')"
www="$(grep -i '^www-authenticate:' "$HDRS" | tr -d '\r' || true)"
[ "$code" = "401" ] && pass "HTTP 401" || bad "HTTP $code (401이어야 함)"
info "$www"
PRM_URL="$(printf '%s' "$www" | sed -n 's/.*resource_metadata="\([^"]*\)".*/\1/p')"
[ -n "$PRM_URL" ] && pass "resource_metadata 있음" || bad "WWW-Authenticate에 resource_metadata 없음"

log "2) 로그인 안내 문서와 Cognito 설정 문서"
if [ -n "$PRM_URL" ]; then
  curl -sS "$PRM_URL" > "$TMP_DIR/mcp-prm.json"
  info "보호 리소스 메타데이터: $(cat "$TMP_DIR/mcp-prm.json")"
  AS="$($PY -c "import json,sys; print((json.load(open(sys.argv[1])).get('authorization_servers') or [''])[0])" "$TMP_DIR/mcp-prm.json")"
  [ -n "$AS" ] && pass "authorization_servers: $AS" || bad "authorization_servers 없음"
fi
curl -sS "$COGNITO_DISCOVERY_URL" > "$TMP_DIR/cognito-oidc.json"
$PY - "$TMP_DIR/cognito-oidc.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
for k in ["issuer", "authorization_endpoint", "token_endpoint", "scopes_supported",
          "code_challenge_methods_supported", "registration_endpoint"]:
    print(f"  {k}: {d.get(k, '(없음)')}", file=sys.stderr)
PY
info "registration_endpoint가 없으면 자동 등록(DCR)이 안 되므로 Claude에는 앱 클라이언트 ID를 직접 넣습니다."

log "3) 시험 계정 토큰으로 MCP 호출"
TOKEN="$(aws cognito-idp admin-initiate-auth --user-pool-id "$USER_POOL_ID" --client-id "$MCP_CLIENT_ID_CLI" \
  --auth-flow ADMIN_USER_PASSWORD_AUTH \
  --auth-parameters "USERNAME=${MCP_TEST_USER},PASSWORD=${MCP_TEST_PASSWORD}" \
  --query AuthenticationResult.AccessToken --output text)"
[ -n "$TOKEN" ] && [ "$TOKEN" != "None" ] && pass "토큰 받음 ($MCP_TEST_USER)" || die "토큰을 받지 못했습니다."

# MCP 요청 하나를 보내고 JSON 응답 본문을 출력합니다 (SSE로 오면 data: 줄만 꺼냄).
# - 본문은 표준 입력(--data-binary @-)으로 넘깁니다. Windows에서 명령줄 인자로 넘기면 한글이 깨집니다.
# - Gateway는 세션 ID(Mcp-Session-Id) 없이 요청마다 따로 처리합니다 (11/24 확인).
mcp() {
  printf '%s' "$1" | curl -sS -X POST "$GATEWAY_URL" \
    -H "Authorization: Bearer ${TOKEN}" -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' --data-binary @- \
  | $PY -c "
import sys
raw = sys.stdin.buffer.read().decode('utf-8', 'replace')
lines = [l[5:].strip() for l in raw.splitlines() if l.startswith('data:')]
sys.stdout.buffer.write(((lines[-1] if lines else raw.strip()) + '\n').encode('utf-8'))"
}

INIT="$(mcp '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"check_mcp","version":"1"}}}')"
info "initialize: $INIT"
printf '%s' "$INIT" | grep -q '"serverInfo"' && pass "initialize" || bad "initialize 응답에 serverInfo 없음"
mcp '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null

LIST="$(mcp '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}')"
TOOL_NAMES="$(printf '%s' "$LIST" | $PY -c "import json,sys; print(' '.join(t['name'] for t in json.load(sys.stdin)['result']['tools']))" 2>/dev/null || true)"
[ -n "$TOOL_NAMES" ] && pass "tools/list: $TOOL_NAMES" || bad "tools/list 실패: $LIST"

# ping 응답(도구 Lambda가 돌려준 JSON)에서 login.groups를 쉼표로 이어 출력합니다. 못 읽으면 빈 줄.
groups_of() {
  printf '%s' "$1" | $PY -c "
import json, sys
text = json.load(sys.stdin)['result']['content'][0]['text']
print(','.join(json.loads(text)['login']['groups']))" 2>/dev/null || true
}

PING_NAME="$(printf '%s' "$TOOL_NAMES" | tr ' ' '\n' | grep -E '___ping$' | head -1 || true)"
if [ -n "$PING_NAME" ]; then
  CALL="$(mcp "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"tools/call\",\"params\":{\"name\":\"${PING_NAME}\",\"arguments\":{\"message\":\"점검\"}}}")"
  info "tools/call $PING_NAME: $CALL"
  printf '%s' "$CALL" | grep -q 'connected' && pass "ping 호출" || bad "ping 응답에 connected 없음"

  log "4) 로그인 그룹 전달 (요청 인터셉터)"
  GOT_GROUPS="$(groups_of "$CALL")"
  [ "$GOT_GROUPS" = "ana" ] && pass "토큰의 그룹이 도구까지 옴: $GOT_GROUPS" \
    || bad "도구가 받은 그룹: '${GOT_GROUPS}' (ana여야 함). 인터셉터 로그를 확인하세요."
  # 클라이언트가 그룹을 꾸며 보내는 경우: 인터셉터가 토큰 값(ana)으로 덮어써야 합니다.
  SPOOF="$(mcp "{\"jsonrpc\":\"2.0\",\"id\":4,\"method\":\"tools/call\",\"params\":{\"name\":\"${PING_NAME}\",\"arguments\":{\"message\":\"점검\",\"caller_groups\":[\"admin\"]}}}")"
  info "꾸민 그룹으로 호출: $SPOOF"
  SPOOF_GROUPS="$(groups_of "$SPOOF")"
  [ "$SPOOF_GROUPS" = "ana" ] && pass "꾸민 caller_groups(admin)를 토큰 값으로 덮어씀" \
    || bad "꾸민 그룹 호출 결과: '${SPOOF_GROUPS}' (ana여야 함)"
else
  bad "ping 도구가 목록에 없음"
fi

echo >&2
[ "$fail" = 0 ] && log "모두 통과" || { log "실패한 항목이 있습니다"; exit 1; }
