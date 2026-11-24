#!/usr/bin/env bash
# 앱 스택(SAM, 저장소 루트의 template.yaml)을 이 PC에서 빌드하고 배포한 뒤 /health를 부릅니다.
# 평소에는 main에 push하면 GitHub Actions(.github/workflows/deploy.yml)가 같은 인자로 배포합니다.
# 이 스크립트는 처음 배포할 때나 Actions 없이 바로 확인할 때 씁니다.
source "$(dirname "$0")/lib.sh"

command -v sam >/dev/null 2>&1 || die "sam 명령이 없습니다. AWS SAM CLI를 설치하세요."
[ -n "${ARTIFACT_BUCKET:-}" ] || die "배포 파일 버킷 정보가 없습니다. 02_storage.sh를 먼저 실행하세요."
# /ask의 로그인 확인(JWT)과 CORS에 쓰는 값. 07_cognito.sh와 09_web.sh가 .state/outputs.env에 적습니다.
for v in USER_POOL_ID WEB_CLIENT_ID MCP_CLIENT_ID_CLI WEB_URL; do
  [ -n "${!v:-}" ] || die "$v 값이 없습니다. 09_web.sh와 07_cognito.sh를 먼저 실행하세요."
done
WEB_ORIGIN="${WEB_URL%/}"   # CORS Origin은 끝에 /가 없어야 합니다.

VERSION="local-$(git describe --always --dirty --abbrev=7 2>/dev/null || echo unknown)"

# sam은 template.yaml이 있는 저장소 루트에서 돌립니다. save_state가 infra/ 기준 경로를 쓰므로 하위 셸에서 이동합니다.
(
  cd "$INFRA_DIR/.."

  log "sam build"
  sam build

  log "sam deploy → 스택 $APP_STACK (버전 $VERSION)"
  sam deploy \
    --stack-name "$APP_STACK" \
    --s3-bucket "$ARTIFACT_BUCKET" \
    --s3-prefix "$APP_STACK" \
    --capabilities CAPABILITY_IAM \
    --region "$AWS_REGION" \
    --tags "${TAG_KEY}=${TAG_VALUE}" \
    --parameter-overrides "Prefix=${PREFIX}" "AppVersion=${VERSION}" \
      "UserPoolId=${USER_POOL_ID}" "WebClientId=${WEB_CLIENT_ID}" "CliClientId=${MCP_CLIENT_ID_CLI}" \
      "WebOrigin=${WEB_ORIGIN}" \
    --no-confirm-changeset --no-fail-on-empty-changeset
)

HEALTH_URL="$(aws cloudformation describe-stacks --stack-name "$APP_STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='HealthUrl'].OutputValue" --output text)"
save_state HEALTH_URL "$HEALTH_URL"
ASK_URL="$(aws cloudformation describe-stacks --stack-name "$APP_STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='AskUrl'].OutputValue" --output text)"
save_state ASK_URL "$ASK_URL"
ok "HEALTH_URL, ASK_URL 저장 (.state/outputs.env)"

log "GET $HEALTH_URL"
curl -sS --retry 3 --retry-all-errors "$HEALTH_URL"
echo
