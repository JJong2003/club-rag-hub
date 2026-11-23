#!/usr/bin/env bash
# 02. 저장소: S3 버킷 2개(문서, 배포 파일)와 DynamoDB 테이블 2개(규정 반영 상태, KB 목록)
source "$(dirname "$0")/lib.sh"

# 퍼블릭 차단, 기본 암호화, HTTPS 강제, 태그를 건 비공개 버킷을 만듭니다.
# 사용법: make_private_bucket 버킷이름 on|off(버전 관리)
make_private_bucket() {
  local b="$1" versioning="$2"
  if exists aws s3api head-bucket --bucket "$b"; then
    skip "S3 버킷 $b"
  else
    aws s3api create-bucket --bucket "$b" \
      --create-bucket-configuration "LocationConstraint=${AWS_REGION}" >/dev/null
    aws s3api wait bucket-exists --bucket "$b"
    ok "S3 버킷 $b 생성"
  fi

  aws s3api put-public-access-block --bucket "$b" \
    --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  aws s3api put-bucket-encryption --bucket "$b" \
    --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
  if [ "$versioning" = "on" ]; then
    aws s3api put-bucket-versioning --bucket "$b" --versioning-configuration Status=Enabled
  fi

  cat > "$TMP_DIR/tls-${b}.json" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DenyInsecureTransport",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "s3:*",
      "Resource": [ "arn:aws:s3:::${b}", "arn:aws:s3:::${b}/*" ],
      "Condition": { "Bool": { "aws:SecureTransport": "false" } }
    }
  ]
}
EOF
  aws s3api put-bucket-policy --bucket "$b" --policy "file://${TMP_DIR}/tls-${b}.json"
  aws s3api put-bucket-tagging --bucket "$b" --tagging "TagSet=[{Key=${TAG_KEY},Value=${TAG_VALUE}}]"
  ok "  퍼블릭 차단, 암호화, HTTPS 강제, 태그 적용 (버전 관리: $versioning)"
}

# 온디맨드 DynamoDB 테이블을 만듭니다. 사용법: make_table 테이블이름 파티션키
make_table() {
  local t="$1" pk="$2"
  if exists aws dynamodb describe-table --table-name "$t"; then
    skip "DynamoDB 테이블 $t"
    return 0
  fi
  aws dynamodb create-table --table-name "$t" \
    --attribute-definitions "AttributeName=${pk},AttributeType=S" \
    --key-schema "AttributeName=${pk},KeyType=HASH" \
    --billing-mode PAY_PER_REQUEST \
    --tags "$TAG_LIST" >/dev/null
  aws dynamodb wait table-exists --table-name "$t"
  ok "DynamoDB 테이블 $t 생성 (파티션 키 $pk)"
}

log "문서 버킷 (학교 원본, KB별 문서)"
make_private_bucket "$DOCS_BUCKET" on
# 학교 원본 HWP/PDF를 둘 폴더. KB가 읽는 폴더(school/kb/ 등)는 파일을 올리면 생깁니다.
aws s3api put-object --bucket "$DOCS_BUCKET" --key "school/raw/" >/dev/null
ok "  school/raw/ 폴더"

log "배포 파일 버킷 (sam deploy가 코드 묶음을 올리는 곳)"
make_private_bucket "$ARTIFACT_BUCKET" off

log "DynamoDB 테이블"
make_table "$RULE_TABLE" rule_id        # 규정마다 마지막으로 반영한 개정 정보
make_table "$REGISTRY_TABLE" kb_key     # 연결된 KB 목록과 누가 검색할 수 있는지

save_state DOCS_BUCKET "$DOCS_BUCKET"
save_state ARTIFACT_BUCKET "$ARTIFACT_BUCKET"
save_state RULE_TABLE "$RULE_TABLE"
save_state REGISTRY_TABLE "$REGISTRY_TABLE"
