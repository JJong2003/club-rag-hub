#!/usr/bin/env bash
# 08. MCP 서버(AgentCore Gateway): Claude 앱·Claude Code가 접속하는 주소
#     Gateway가 MCP 통신과 로그인 확인(Cognito 토큰)을 맡고, 도구가 불리면 도구 Lambda를 부릅니다.
#     도구 Lambda를 부르기 직전에 요청 인터셉터 Lambda가 토큰의 그룹을 입력값(caller_groups)에 넣습니다.
#     먼저 07_cognito.sh와 앱 배포(도구·인터셉터 Lambda, deploy_app.sh 또는 Actions)가 끝나 있어야 합니다.
#
# 도구 설명(이름, 설명, 입력 형식)은 ../src/tools/tool_schema.json에 있습니다.
# 도구 설명을 고치면 이 스크립트를 다시 실행해야 Gateway에 반영됩니다 (Lambda 코드는 앱 배포로 반영).
source "$(dirname "$0")/lib.sh"

GATEWAY_NAME="${PREFIX}-mcp"
GATEWAY_ROLE="${PREFIX}-mcp-gateway"
TARGET_NAME="club"                       # Claude에 보이는 도구 이름 앞에 붙습니다 (예: club___ping)
TOOLS_FUNCTION="${APP_STACK}-tools"
INTERCEPTOR_FUNCTION="${APP_STACK}-interceptor"
TOOL_SCHEMA_FILE="../src/tools/tool_schema.json"
# MCP 클라이언트(Claude)가 서버에 처음 붙을 때 읽는 안내문입니다.
# JSON 안에 그대로 들어가므로 큰따옴표와 역슬래시를 쓰지 마세요.
INSTRUCTIONS="충남대학교 동아리(ANA) 운영을 돕는 자료 검색 서버입니다. 학교 규정과 동아리 운영 자료(노션)에서 근거를 찾습니다. list_sources로 볼 수 있는 자료를 확인하고, search로 관련 문단을 찾고, 동아리 자료 전체가 필요하면 read_document로 읽으세요. 답변은 찾은 근거로만 쓰고 출처(규정명·조문, 노션 경로·연도)를 붙이세요."

if [ -z "${USER_POOL_ID:-}" ] || [ -z "${MCP_CLIENT_ID_CLAUDE:-}" ] || [ -z "${MCP_CLIENT_ID_CLI:-}" ]; then
  die "로그인(Cognito) 정보가 없습니다. 07_cognito.sh를 먼저 실행하세요."
fi
TOOLS_ARN="$(aws lambda get-function --function-name "$TOOLS_FUNCTION" \
  --query Configuration.FunctionArn --output text 2>/dev/null)" \
  || die "도구 Lambda($TOOLS_FUNCTION)가 없습니다. 앱을 먼저 배포하세요 (bash deploy_app.sh)."
INTERCEPTOR_ARN="$(aws lambda get-function --function-name "$INTERCEPTOR_FUNCTION"   --query Configuration.FunctionArn --output text 2>/dev/null)"   || die "인터셉터 Lambda($INTERCEPTOR_FUNCTION)가 없습니다. 앱을 먼저 배포하세요 (bash deploy_app.sh)."
[ -f "$TOOL_SCHEMA_FILE" ] || die "도구 설명 파일이 없습니다: $TOOL_SCHEMA_FILE"

log "Gateway 실행 역할 $GATEWAY_ROLE"
# AgentCore 서비스가 이 계정의 Gateway를 위해서만 역할을 빌릴 수 있게 조건을 겁니다 (confused deputy 방지).
cat > "$TMP_DIR/gateway-trust.json" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "bedrock-agentcore.amazonaws.com" },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": { "aws:SourceAccount": "${ACCOUNT_ID}" },
        "ArnLike": { "aws:SourceArn": "arn:aws:bedrock-agentcore:${AWS_REGION}:${ACCOUNT_ID}:gateway/*" }
      }
    }
  ]
}
EOF
# Gateway가 할 수 있는 일은 인터셉터 Lambda와 도구 Lambda를 부르는 것뿐입니다.
cat > "$TMP_DIR/gateway-policy.json" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "InvokeInterceptorAndToolsLambdaOnly",
      "Effect": "Allow",
      "Action": "lambda:InvokeFunction",
      "Resource": [ "${INTERCEPTOR_ARN}", "${INTERCEPTOR_ARN}:*", "${TOOLS_ARN}", "${TOOLS_ARN}:*" ]
    }
  ]
}
EOF
if exists aws iam get-role --role-name "$GATEWAY_ROLE"; then
  skip "역할 $GATEWAY_ROLE (정책은 다시 적용합니다)"
  aws iam update-assume-role-policy --role-name "$GATEWAY_ROLE" \
    --policy-document "file://${TMP_DIR}/gateway-trust.json"
else
  aws iam create-role --role-name "$GATEWAY_ROLE" \
    --assume-role-policy-document "file://${TMP_DIR}/gateway-trust.json" \
    --description "AgentCore Gateway ${GATEWAY_NAME}: invoke MCP tools Lambda" \
    --tags "$TAG_LIST" >/dev/null
  ok "역할 생성"
fi
aws iam put-role-policy --role-name "$GATEWAY_ROLE" --policy-name invoke-tools \
  --policy-document "file://${TMP_DIR}/gateway-policy.json"
GATEWAY_ROLE_ARN="$(aws iam get-role --role-name "$GATEWAY_ROLE" --query Role.Arn --output text)"

# 만들거나 고친 뒤 READY가 될 때까지 기다립니다. 사용법: wait_ready 설명 명령...
wait_ready() {
  local what="$1" status
  shift
  for _ in $(seq 1 40); do
    status="$("$@" --query status --output text)"
    case "$status" in
      READY) ok "$what READY"; return 0 ;;
      FAILED|*_UNSUCCESSFUL)
        "$@" --query statusReasons --output text >&2
        die "$what $status" ;;
    esac
    sleep 5
  done
  die "$what 이(가) 3분 넘게 $status 상태입니다."
}

log "Gateway $GATEWAY_NAME (로그인: Cognito 토큰, 허용 앱 클라이언트 2개, 요청 인터셉터)"
# allowedClients: 토큰의 client_id가 이 목록에 있어야 통과합니다. 서명·만료·발급자도 Gateway가 확인합니다.
# interceptorConfigurations: 요청(REQUEST)마다 인터셉터를 부르고, 헤더(토큰 포함)를 넘깁니다(passRequestHeaders).
#   Gateway는 토큰을 도구 Lambda에 넘기지 않으므로, 인터셉터가 토큰의 그룹을 입력값에 적어 넣습니다.
cat > "$TMP_DIR/gateway.json" <<EOF
{
  "name": "${GATEWAY_NAME}",
  "description": "club-rag-hub MCP server",
  "roleArn": "${GATEWAY_ROLE_ARN}",
  "protocolType": "MCP",
  "protocolConfiguration": { "mcp": { "instructions": "${INSTRUCTIONS}" } },
  "authorizerType": "CUSTOM_JWT",
  "authorizerConfiguration": {
    "customJWTAuthorizer": {
      "discoveryUrl": "${COGNITO_DISCOVERY_URL}",
      "allowedClients": [ "${MCP_CLIENT_ID_CLAUDE}", "${MCP_CLIENT_ID_CLI}" ]
    }
  },
  "interceptorConfigurations": [
    {
      "interceptor": { "lambda": { "arn": "${INTERCEPTOR_ARN}" } },
      "interceptionPoints": [ "REQUEST" ],
      "inputConfiguration": { "passRequestHeaders": true }
    }
  ]
}
EOF
GATEWAY_ID="$(aws bedrock-agentcore-control list-gateways \
  --query "items[?name=='${GATEWAY_NAME}'].gatewayId | [0]" --output text)"
if [ "$GATEWAY_ID" != "None" ] && [ -n "$GATEWAY_ID" ]; then
  # 설정을 파일 내용으로 다시 맞춥니다 (로그인 설정이나 안내문을 고쳤을 때 반영).
  python - "$TMP_DIR/gateway.json" "$GATEWAY_ID" <<'PY'
import json, sys
path, gid = sys.argv[1], sys.argv[2]
cfg = json.load(open(path, encoding="utf-8"))
cfg["gatewayIdentifier"] = gid
json.dump(cfg, open(path, "w", encoding="utf-8"), ensure_ascii=False)
PY
  aws bedrock-agentcore-control update-gateway --cli-input-json "file://${TMP_DIR}/gateway.json" >/dev/null
  skip "Gateway $GATEWAY_ID (설정은 다시 적용)"
else
  # 새로 만든 역할은 몇 초 뒤에야 서비스가 빌릴 수 있어서 실패하면 다시 시도합니다.
  retry 6 aws bedrock-agentcore-control create-gateway \
    --cli-input-json "file://${TMP_DIR}/gateway.json" \
    --tags "${TAG_KEY}=${TAG_VALUE}" >/dev/null
  GATEWAY_ID="$(aws bedrock-agentcore-control list-gateways \
    --query "items[?name=='${GATEWAY_NAME}'].gatewayId | [0]" --output text)"
  ok "Gateway 생성 $GATEWAY_ID"
fi
wait_ready "Gateway" aws bedrock-agentcore-control get-gateway --gateway-identifier "$GATEWAY_ID"
GATEWAY_URL="$(aws bedrock-agentcore-control get-gateway --gateway-identifier "$GATEWAY_ID" --query gatewayUrl --output text)"
save_state GATEWAY_ID "$GATEWAY_ID"
save_state GATEWAY_URL "$GATEWAY_URL"

log "Gateway 대상 $TARGET_NAME → 도구 Lambda (도구: $(python -c "import json,sys; print(', '.join(t['name'] for t in json.load(open(sys.argv[1], encoding='utf-8'))))" "$TOOL_SCHEMA_FILE"))"
# 대상에 Authorization 헤더를 넘기는 설정(metadataConfiguration)은 Gateway가
# "Header 'Authorization' is restricted"로 거부합니다 (11/24 확인). 그래서 그룹은 위의 인터셉터가 넘깁니다.
python - "$TMP_DIR/gateway-target.json" "$GATEWAY_ID" "$TARGET_NAME" "$TOOLS_ARN" "$TOOL_SCHEMA_FILE" <<'PY'
import json, sys
out, gid, name, arn, schema = sys.argv[1:]
cfg = {
    "gatewayIdentifier": gid,
    "name": name,
    "description": "club-rag-hub tools Lambda",
    "targetConfiguration": {"mcp": {"lambda": {
        "lambdaArn": arn,
        "toolSchema": {"inlinePayload": json.load(open(schema, encoding="utf-8"))},
    }}},
    "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
}
json.dump(cfg, open(out, "w", encoding="utf-8"), ensure_ascii=False)
PY
TARGET_ID="$(aws bedrock-agentcore-control list-gateway-targets --gateway-identifier "$GATEWAY_ID" \
  --query "items[?name=='${TARGET_NAME}'].targetId | [0]" --output text)"
if [ "$TARGET_ID" != "None" ] && [ -n "$TARGET_ID" ]; then
  aws bedrock-agentcore-control update-gateway-target --target-id "$TARGET_ID" \
    --cli-input-json "file://${TMP_DIR}/gateway-target.json" >/dev/null
  skip "대상 $TARGET_ID (도구 설명은 다시 적용)"
else
  TARGET_ID="$(aws bedrock-agentcore-control create-gateway-target \
    --cli-input-json "file://${TMP_DIR}/gateway-target.json" --query targetId --output text)"
  ok "대상 생성 $TARGET_ID"
fi
wait_ready "대상" aws bedrock-agentcore-control get-gateway-target --gateway-identifier "$GATEWAY_ID" --target-id "$TARGET_ID"

log "MCP 서버 주소: $GATEWAY_URL"
info "점검: bash check_mcp.sh"
