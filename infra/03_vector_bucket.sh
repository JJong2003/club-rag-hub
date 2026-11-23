#!/usr/bin/env bash
# 03. S3 Vectors 벡터 버킷: KB마다 이 버킷 안에 인덱스를 하나씩 만듭니다 (04에서).
source "$(dirname "$0")/lib.sh"

log "벡터 버킷 $VECTOR_BUCKET"
if exists aws s3vectors get-vector-bucket --vector-bucket-name "$VECTOR_BUCKET"; then
  skip "벡터 버킷 $VECTOR_BUCKET"
else
  aws s3vectors create-vector-bucket --vector-bucket-name "$VECTOR_BUCKET" --tags "$TAG_MAP" >/dev/null
  ok "벡터 버킷 $VECTOR_BUCKET 생성"
fi

save_state VECTOR_BUCKET "$VECTOR_BUCKET"
save_state VECTOR_BUCKET_ARN "$VECTOR_BUCKET_ARN"
