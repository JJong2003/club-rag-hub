#!/usr/bin/env bash
# 00~07과 09를 순서대로 실행합니다 (09는 07보다 먼저). 중간에 멈추면 원인을 고친 뒤 이 파일을 다시 실행하면 됩니다.
# 이미 만든 리소스는 건너뛰므로 여러 번 실행해도 괜찮습니다.
set -euo pipefail
cd "$(dirname "$0")"

bash ./00_check.sh
# 01_budget.sh는 실행하지 않습니다. 이 계정은 학교 AWS Organization 소속이라
# 서비스 제어 정책(SCP)이 budgets:*를 막습니다 (2026-11-23 확인).
bash ./02_storage.sh
bash ./03_vector_bucket.sh
bash ./04_add_kb.sh school   school/kb/    NONE       '*' "충남대 학교 규정" "충남대학교 학칙과 시설·안전 규정(강의실 사용, 전열기구, 화재예방·소방안전, 감염병 관리, 정심화국제문화회관), 학생 포상·징계 규정. 조문 하나가 문서 하나"    # 모두 검색 가능
bash ./04_add_kb.sh club-ana clubs/ana/kb/ FIXED_SIZE ana "ANA 동아리 운영 자료" "충남대 ANA 동아리 노션 자료: 연간 일정, 회의록, 행사·MT·교육 기록, 부서별 역할"    # ana 그룹만
bash ./05_github_oidc.sh
bash ./06_github_actions.sh   # gh 로그인이 필요합니다
bash ./09_web.sh              # 웹 호스팅 (S3 + CloudFront). 07의 웹 앱 클라이언트가 이 주소를 써서 먼저 실행
bash ./07_cognito.sh          # 로그인 계정 (Cognito): 웹 챗봇, MCP 서버
# 08_mcp_gateway.sh는 앱(도구 Lambda)을 배포한 뒤에 따로 실행합니다 (README의 'MCP 서버' 참고).

echo
echo "모두 끝났습니다. 만든 리소스의 ID는 .state/outputs.env에 있습니다."
echo "다음은 README.md의 '스크립트 다음에 직접 할 일'을 보세요."
