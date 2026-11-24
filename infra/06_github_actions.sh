#!/usr/bin/env bash
# 06. GitHub Actions 설정: 배포에 쓰는 값을 저장소 Secrets와 Variables에 등록합니다.
#     값은 05, 07, 09가 .state/outputs.env에 적어 둔 것을 그대로 씁니다. 직접 입력할 값은 없습니다.
#     공개 저장소라 계정 ID가 들어간 값(역할 ARN, 웹 버킷 이름)은 Actions 로그에 찍히지 않게 Secrets에 둡니다.
#     나머지(사용자 풀 ID, 클라이언트 ID, 배포 ID, 주소)는 웹 화면이나 토큰에도 보이는 값이라 Variables에 둡니다.
#     다시 실행하면 같은 이름을 덮어씁니다.
source "$(dirname "$0")/lib.sh"

REPO="${GITHUB_OWNER}/${GITHUB_REPO}"

command -v gh >/dev/null 2>&1 || die "GitHub CLI(gh)가 없습니다."
gh auth status >/dev/null 2>&1 || die "gh 로그인이 필요합니다: gh auth login"

# 저장소에 등록할 이름=outputs.env의 이름. CLI 클라이언트는 07이 MCP_CLIENT_ID_CLI로 적습니다.
SECRETS="AWS_ROLE_ARN=DEPLOY_ROLE_ARN WEB_BUCKET=WEB_BUCKET"
VARIABLES="USER_POOL_ID=USER_POOL_ID WEB_CLIENT_ID=WEB_CLIENT_ID CLI_CLIENT_ID=MCP_CLIENT_ID_CLI
  WEB_DISTRIBUTION_ID=WEB_DISTRIBUTION_ID WEB_URL=WEB_URL COGNITO_DOMAIN=COGNITO_DOMAIN"

# 하나라도 없으면 아무것도 등록하지 않고 멈춥니다.
for pair in $SECRETS $VARIABLES; do
  src="${pair#*=}"
  [ -n "${!src:-}" ] || die "$src 값이 없습니다. 05_github_oidc.sh, 09_web.sh, 07_cognito.sh를 먼저 실행하세요."
done

log "Actions 시크릿 → $REPO"
for pair in $SECRETS; do
  name="${pair%%=*}" src="${pair#*=}"
  # 표준 입력으로 넘겨서 값이 명령줄에 남지 않게 합니다.
  printf '%s' "${!src}" | gh secret set "$name" --repo "$REPO"
  ok "$name (값: .state/outputs.env의 $src)"
done

log "Actions 변수 → $REPO"
for pair in $VARIABLES; do
  name="${pair%%=*}" src="${pair#*=}"
  val="${!src}"
  [ "$name" = WEB_URL ] && val="${val%/}"   # 끝의 /는 빼고 등록합니다 (워크플로가 붙임)
  printf '%s' "$val" | gh variable set "$name" --repo "$REPO"
  ok "$name (값: .state/outputs.env의 $src)"
done

# 목록에는 이름과 바꾼 시각만 보여 줍니다 (변수 값은 화면에 찍지 않음).
info "등록된 시크릿:"
gh secret list --repo "$REPO" >&2
info "등록된 변수:"
gh variable list --repo "$REPO" --json name,updatedAt --jq '.[] | "\(.name)\t\(.updatedAt)"' >&2
