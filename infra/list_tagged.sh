#!/usr/bin/env bash
# 태그로 이 프로젝트 리소스를 찾아봅니다 (만들거나 지우지 않습니다).
source "$(dirname "$0")/lib.sh"

log "태그 ${TAG_KEY}=${TAG_VALUE} 가 붙은 리소스 ($AWS_REGION)"
aws resourcegroupstaggingapi get-resources \
  --tag-filters "Key=${TAG_KEY},Values=${TAG_VALUE}" \
  --query 'ResourceTagMappingList[].ResourceARN' --output text | tr '\t' '\n'

log "IAM 역할 (전역 서비스라 이름으로 따로 조회)"
aws iam list-roles --query "Roles[?starts_with(RoleName, '${PREFIX}-')].RoleName" --output text | tr '\t' '\n'

log "만든 리소스 ID (.state/outputs.env)"
cat "$STATE_FILE"

info ""
info "서비스 종류에 따라 태그 조회 결과에 안 나오는 리소스가 있을 수 있습니다."
info "예산 알림(${PREFIX}-monthly)은 태그 없이 만들었습니다."
