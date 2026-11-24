#!/usr/bin/env bash
# 07. 로그인(Cognito): MCP 서버(AgentCore Gateway)에 들어올 사람의 계정
#     - 메일 주소로 로그인합니다. 가입은 막고 관리자가 계정을 만듭니다.
#     - 앱 스택을 지웠다 다시 만들어도 계정이 남도록 SAM이 아니라 이 스크립트로 만듭니다.
#     - 로그인 방식(메일 로그인)은 사용자 풀을 만든 뒤에 바꿀 수 없습니다.
#
# 앱 클라이언트는 두 개입니다.
#   ${PREFIX}-mcp-claude : Claude 앱·Claude Code가 브라우저 로그인(OAuth 인가 코드 + PKCE)에 씀. 비밀값 없음
#   ${PREFIX}-mcp-cli    : 점검 스크립트(check_mcp.sh)가 시험 계정 토큰을 받을 때 씀.
#                          관리자 자격 증명(IAM)이 있어야만 토큰을 받는 방식이라 밖에서는 못 씀
#
# 연결 시험용 계정(mcp-test@example.com, 그룹 ana)도 만듭니다. 비밀번호는 .state/mcp-test-user.env에 적습니다.
source "$(dirname "$0")/lib.sh"

USER_POOL_NAME="${PREFIX}-users"
CLAUDE_CLIENT_NAME="${PREFIX}-mcp-claude"
CLI_CLIENT_NAME="${PREFIX}-mcp-cli"
USER_GROUPS=(ana)                       # 동아리 그룹. kb-registry의 allowed_groups와 같은 이름
CLAUDE_CODE_CALLBACK_PORT=8765          # Claude Code에 --callback-port로 같은 값을 줍니다
# 로그인을 마치고 돌아갈 주소. 정확히 같아야 해서 하나씩 등록합니다.
CALLBACK_URLS=(
  "https://claude.ai/api/mcp/auth_callback"
  "https://claude.com/api/mcp/auth_callback"
  "http://localhost:${CLAUDE_CODE_CALLBACK_PORT}/callback"
)
TEST_USER="mcp-test@example.com"        # example.com은 실제로 메일이 가지 않는 예약 도메인
TEST_USER_FILE=".state/mcp-test-user.env"

log "사용자 풀 $USER_POOL_NAME"
USER_POOL_ID="$(aws cognito-idp list-user-pools --max-results 60 \
  --query "UserPools[?Name=='${USER_POOL_NAME}'].Id | [0]" --output text)"
if [ "$USER_POOL_ID" != "None" ] && [ -n "$USER_POOL_ID" ]; then
  skip "사용자 풀 $USER_POOL_ID"
else
  USER_POOL_ID="$(aws cognito-idp create-user-pool \
    --pool-name "$USER_POOL_NAME" \
    --username-attributes email \
    --auto-verified-attributes email \
    --username-configuration CaseSensitive=false \
    --admin-create-user-config AllowAdminCreateUserOnly=true \
    --policies 'PasswordPolicy={MinimumLength=10,RequireUppercase=false,RequireLowercase=true,RequireNumbers=true,RequireSymbols=false,TemporaryPasswordValidityDays=7}' \
    --account-recovery-setting 'RecoveryMechanisms=[{Priority=1,Name=verified_email}]' \
    --mfa-configuration OFF \
    --deletion-protection ACTIVE \
    --user-pool-tags "$TAG_MAP" \
    --query UserPool.Id --output text)"
  ok "사용자 풀 생성 $USER_POOL_ID (삭제 보호 켬)"
fi
save_state USER_POOL_ID "$USER_POOL_ID"
save_state COGNITO_DISCOVERY_URL "https://cognito-idp.${AWS_REGION}.amazonaws.com/${USER_POOL_ID}/.well-known/openid-configuration"

log "그룹 ${USER_GROUPS[*]}"
for g in "${USER_GROUPS[@]}"; do
  if exists aws cognito-idp get-group --user-pool-id "$USER_POOL_ID" --group-name "$g"; then
    skip "그룹 $g"
  else
    aws cognito-idp create-group --user-pool-id "$USER_POOL_ID" --group-name "$g" \
      --description "club group: $g" >/dev/null
    ok "그룹 $g 생성"
  fi
done

log "로그인 화면 도메인"
DOMAIN_PREFIX="$(aws cognito-idp describe-user-pool --user-pool-id "$USER_POOL_ID" --query UserPool.Domain --output text)"
if [ "$DOMAIN_PREFIX" != "None" ] && [ -n "$DOMAIN_PREFIX" ]; then
  skip "도메인 $DOMAIN_PREFIX"
else
  # 도메인 앞부분은 리전 안에서 겹치면 안 돼서 임의 문자를 붙입니다 (계정 ID는 넣지 않음).
  DOMAIN_PREFIX="${PREFIX}-$(openssl rand -hex 3)"
  # 버전 1 = 기본 로그인 화면(hosted UI). 꾸미기 설정 없이 바로 씁니다.
  aws cognito-idp create-user-pool-domain --user-pool-id "$USER_POOL_ID" \
    --domain "$DOMAIN_PREFIX" --managed-login-version 1 >/dev/null
  ok "도메인 생성 $DOMAIN_PREFIX"
fi
save_state COGNITO_DOMAIN "${DOMAIN_PREFIX}.auth.${AWS_REGION}.amazoncognito.com"

# 이름으로 앱 클라이언트 ID를 찾습니다. 없으면 빈 문자열.
find_client() {
  local id
  id="$(aws cognito-idp list-user-pool-clients --user-pool-id "$USER_POOL_ID" --max-results 60 \
    --query "UserPoolClients[?ClientName=='$1'].ClientId | [0]" --output text)"
  [ "$id" = "None" ] && id=""
  printf '%s' "$id"
}

# update-user-pool-client는 빠진 설정을 기본값으로 되돌리므로, 만들 때와 고칠 때 같은 인자를 씁니다.
# (--token-validity-units 값의 쉼표는 AWS CLI 형식이라 배열 구분자가 아닙니다.)
# shellcheck disable=SC2054
COMMON_CLIENT_ARGS=(
  --prevent-user-existence-errors ENABLED
  --enable-token-revocation
  --access-token-validity 60 --id-token-validity 60 --refresh-token-validity 30
  --token-validity-units AccessToken=minutes,IdToken=minutes,RefreshToken=days
)
CLAUDE_CLIENT_ARGS=(
  --explicit-auth-flows ALLOW_REFRESH_TOKEN_AUTH
  --allowed-o-auth-flows code
  --allowed-o-auth-flows-user-pool-client
  --allowed-o-auth-scopes openid email profile
  --callback-urls "${CALLBACK_URLS[@]}"
  --supported-identity-providers COGNITO
)
CLI_CLIENT_ARGS=(
  --explicit-auth-flows ALLOW_ADMIN_USER_PASSWORD_AUTH ALLOW_REFRESH_TOKEN_AUTH
)

upsert_client() {   # 사용법: upsert_client 이름 상태키 인자...
  local name="$1" key="$2" id
  shift 2
  id="$(find_client "$name")"
  if [ -n "$id" ]; then
    aws cognito-idp update-user-pool-client --user-pool-id "$USER_POOL_ID" --client-id "$id" \
      --client-name "$name" "${COMMON_CLIENT_ARGS[@]}" "$@" >/dev/null
    skip "앱 클라이언트 $name (설정은 다시 적용)"
  else
    id="$(aws cognito-idp create-user-pool-client --user-pool-id "$USER_POOL_ID" \
      --client-name "$name" --no-generate-secret "${COMMON_CLIENT_ARGS[@]}" "$@" \
      --query UserPoolClient.ClientId --output text)"
    ok "앱 클라이언트 $name 생성"
  fi
  save_state "$key" "$id"
}

log "앱 클라이언트"
upsert_client "$CLAUDE_CLIENT_NAME" MCP_CLIENT_ID_CLAUDE "${CLAUDE_CLIENT_ARGS[@]}"
upsert_client "$CLI_CLIENT_NAME" MCP_CLIENT_ID_CLI "${CLI_CLIENT_ARGS[@]}"
info "돌아갈 주소: ${CALLBACK_URLS[*]}"

log "연결 시험용 계정 $TEST_USER (그룹 ana)"
new_password() {   # 영문 소문자·숫자만 써서 명령줄 인자로 넘기기 쉽게 합니다.
  printf '%sa1' "$(openssl rand -hex 10)"
}
if exists aws cognito-idp admin-get-user --user-pool-id "$USER_POOL_ID" --username "$TEST_USER"; then
  skip "계정 $TEST_USER"
else
  aws cognito-idp admin-create-user --user-pool-id "$USER_POOL_ID" --username "$TEST_USER" \
    --user-attributes Name=email,Value="$TEST_USER" Name=email_verified,Value=true \
    --message-action SUPPRESS >/dev/null
  rm -f "$TEST_USER_FILE"
  ok "계정 생성 (안내 메일 보내지 않음)"
fi
if [ ! -f "$TEST_USER_FILE" ]; then
  TEST_PASSWORD="$(new_password)"
  aws cognito-idp admin-set-user-password --user-pool-id "$USER_POOL_ID" --username "$TEST_USER" \
    --password "$TEST_PASSWORD" --permanent
  umask 077
  printf 'MCP_TEST_USER="%s"\nMCP_TEST_PASSWORD="%s"\n' "$TEST_USER" "$TEST_PASSWORD" > "$TEST_USER_FILE"
  ok "비밀번호를 정해서 $TEST_USER_FILE 에 적음 (커밋 안 되는 폴더)"
fi
aws cognito-idp admin-add-user-to-group --user-pool-id "$USER_POOL_ID" --username "$TEST_USER" --group-name ana
ok "그룹 ana에 넣음"

log "다음: 앱 배포(도구 Lambda) 후 bash 08_mcp_gateway.sh"
