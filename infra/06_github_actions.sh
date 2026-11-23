#!/usr/bin/env bash
# 06. GitHub Actions 설정: 배포 역할 ARN을 저장소 Secrets(AWS_ROLE_ARN)에 등록합니다.
#     값은 05가 .state/outputs.env에 적어 둔 DEPLOY_ROLE_ARN을 그대로 씁니다. 직접 입력할 값은 없습니다.
#     공개 저장소라 Actions 로그에 계정 ID가 찍히지 않게 Variables가 아니라 Secrets에 둡니다.
#     다시 실행하면 같은 값으로 덮어씁니다.
source "$(dirname "$0")/lib.sh"

REPO="${GITHUB_OWNER}/${GITHUB_REPO}"

[ -n "${DEPLOY_ROLE_ARN:-}" ] || die "DEPLOY_ROLE_ARN이 없습니다. 05_github_oidc.sh를 먼저 실행하세요."
command -v gh >/dev/null 2>&1 || die "GitHub CLI(gh)가 없습니다."
gh auth status >/dev/null 2>&1 || die "gh 로그인이 필요합니다: gh auth login"

log "Actions 시크릿 AWS_ROLE_ARN → $REPO"
# 표준 입력으로 넘겨서 값이 명령줄에 남지 않게 합니다.
printf '%s' "$DEPLOY_ROLE_ARN" | gh secret set AWS_ROLE_ARN --repo "$REPO"
ok "AWS_ROLE_ARN 등록 (값: .state/outputs.env의 DEPLOY_ROLE_ARN)"
info "등록된 시크릿:"
gh secret list --repo "$REPO" >&2
