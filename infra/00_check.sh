#!/usr/bin/env bash
# 00. 실행 전 확인: AWS CLI 버전, 로그인한 계정, 리전, 임베딩 모델, 만들 리소스 이름
source "$(dirname "$0")/lib.sh"

log "AWS CLI 확인"
ver="$(aws --version 2>&1)"
info "$ver"
case "$ver" in
  aws-cli/2.*) ok "AWS CLI v2" ;;
  *) die "AWS CLI v2가 필요합니다." ;;
esac

log "로그인한 계정 확인 (프로필 $AWS_PROFILE, 리전 $AWS_REGION)"
read -r real_account arn < <(aws sts get-caller-identity --query '[Account,Arn]' --output text)
info "$arn"
case "$arn" in
  *:root) die "루트 계정으로 실행 중입니다. 작업용 IAM 사용자 프로필로 바꾸세요." ;;
esac
[ "$real_account" = "$ACCOUNT_ID" ] \
  || die ".state에 적힌 계정($ACCOUNT_ID)과 지금 계정($real_account)이 다릅니다. 계정을 바꿨다면 .state 폴더를 지우고 다시 실행하세요."
ok "계정 ID $ACCOUNT_ID"

log "S3 Vectors 명령 확인"
exists aws s3vectors list-vector-buckets --max-results 1 \
  || die "s3vectors 명령이 실패했습니다. AWS CLI를 최신 버전으로 업데이트하세요."
ok "s3vectors 사용 가능"

log "임베딩 모델 확인 ($EMBED_MODEL_ID)"
exists aws bedrock get-foundation-model --model-identifier "$EMBED_MODEL_ID" \
  || die "$AWS_REGION 에서 $EMBED_MODEL_ID 를 찾지 못했습니다."
ok "Titan Text Embeddings V2 사용 가능"

log "설정 확인"
if [ -z "${ALERT_EMAIL:-}" ]; then
  info "ALERT_EMAIL이 비어 있습니다. 01_budget.sh 전에 config.local.env에 적으세요."
fi
info "GitHub 저장소: ${GITHUB_OWNER}/${GITHUB_REPO}"
info "태그: ${TAG_KEY}=${TAG_VALUE}"

log "만들 리소스 이름"
info "문서 버킷          $DOCS_BUCKET"
info "배포 파일 버킷     $ARTIFACT_BUCKET"
info "벡터 버킷          $VECTOR_BUCKET"
info "DynamoDB 테이블    $RULE_TABLE, $REGISTRY_TABLE"
info "앱 스택 (SAM)      $APP_STACK"
info "GitHub 배포 역할   $DEPLOY_ROLE"
ok "확인 끝"
