# infra: AWS 리소스 생성 스크립트

모든 리소스에 태그 `Project=cloudarchitect-termproj`를 답니다. 예산 알림은 예외예요.
리전은 서울(ap-northeast-2)입니다.

## 무엇을 어디서 만드는지

| 구분 | 만드는 곳 | 리소스 |
|---|---|---|
| 데이터 | 이 폴더의 스크립트 | S3 버킷, DynamoDB 테이블, S3 Vectors, Bedrock KB, KB용 IAM 역할, GitHub OIDC, Actions 시크릿 |
| 로그인, MCP 서버 | 이 폴더의 스크립트 | Cognito 사용자 풀(계정), AgentCore Gateway(MCP 서버), Gateway 실행 역할 |
| 앱 | 저장소 루트의 `template.yaml` (SAM) | Lambda(`/health`, MCP 도구, Gateway 인터셉터), HTTP API, Lambda 실행 역할, 로그 그룹 |

데이터 쪽을 스크립트로 뺀 이유는 두 가지예요. 동아리 KB를 하나 더 붙일 때 앱을 다시 배포하지 않아도 되고, 앱 스택을 지웠다 다시 만들어도 문서와 벡터가 남습니다.
Cognito와 Gateway도 같은 이유로 스크립트로 만듭니다. 앱 스택을 지워도 계정이 남고, GitHub 배포 역할의 권한을 늘리지 않아도 됩니다.

## KB 여러 개 연결

```
                      ┌ kb-registry (DynamoDB): 연결된 KB와 검색할 수 있는 그룹
질문 → MCP 도구 Lambda ┤
                      ├ KB school   ← s3://<문서 버킷>/school/kb/     검색: 모두
                      └ KB club-ana ← s3://<문서 버킷>/clubs/ana/kb/  검색: ana 그룹
```

- MCP 도구 Lambda는 사용자가 볼 수 있는 KB를 레지스트리에서 고릅니다. 고른 KB마다 Retrieve를 호출하고, 결과를 KB별로 묶어서 돌려줍니다. 답은 사용자의 LLM(Claude 등)이 씁니다. (검색 도구는 만드는 중)
- KB끼리 점수로 순위를 매기지 않습니다. 11/23 점검에서 동아리 자료 점수가 학교 조문보다 높게 나와서, 점수순으로 자르면 학교 규정이 빠졌어요.
- 모든 KB는 같은 임베딩(Titan Text Embeddings V2, 1024차원, cosine)을 씁니다.
- KB마다 IAM 역할을 따로 줘서, 각 KB는 자기 S3 폴더와 자기 벡터 인덱스만 읽고 씁니다.
- 동아리 추가: `bash 04_add_kb.sh club-xxx clubs/xxx/kb/ FIXED_SIZE xxx`
- 연합 행사: 레지스트리에서 상대 동아리 KB 항목의 `allowed_groups`에 우리 그룹을 추가합니다.

## 실행 전 준비 (직접)

1. Git Bash와 최신 AWS CLI v2를 설치합니다. 예전 CLI에는 `s3vectors` 명령이 없어요.
2. 작업용 IAM 사용자의 액세스 키로 `aws configure --profile club-rag-hub`를 실행합니다. 리전은 `ap-northeast-2`, 출력은 `json`으로 둡니다.
3. `config.env` 값을 확인합니다. 그다음 같은 폴더에 `config.local.env`를 만들고 아래 한 줄을 적습니다. 이 파일은 git에 올라가지 않아요.
   ```
   ALERT_EMAIL="받을메일@example.com"
   ```

## 실행

Git Bash에서:

```
cd infra
bash run_all.sh
```

하나씩 돌릴 때는 순서를 지켜 주세요: `00_check` → `02_storage` → `03_vector_bucket` → `04_add_kb`(KB마다) → `05_github_oidc` → `06_github_actions` → `07_cognito`. `08_mcp_gateway`는 앱을 배포한 뒤에 돌립니다.
`01_budget`은 `run_all.sh`에서 뺐습니다. 이 계정은 학교 AWS Organization 소속이라 서비스 제어 정책(SCP)이 예산(Budgets)을 막습니다.
이미 있는 리소스는 건너뛰기 때문에, 중간에 멈추면 원인만 고치고 다시 돌리면 됩니다.
만든 리소스의 ID는 `.state/outputs.env`에 적힙니다. 태그로 확인하려면 `bash list_tagged.sh`를 실행하세요.

## 스크립트 다음에 직접 할 일

1. **Anthropic 모델 사용 양식**: Bedrock 콘솔(서울) → 모델 카탈로그 → Claude 모델 선택 → 사용 사례 양식 제출 → 플레이그라운드에서 질문 한 번.
2. **GitHub 저장소**: 저장소 루트에서 `git init` → 첫 커밋 → `gh repo create JJong2003/club-rag-hub --public --source . --push`.
3. **Actions 시크릿**: `bash 06_github_actions.sh`를 실행합니다(`gh` 로그인 필요). `05`가 `.state/outputs.env`에 적어 둔 `DEPLOY_ROLE_ARN`을 저장소 Secrets의 `AWS_ROLE_ARN`으로 등록하므로 직접 입력할 값은 없어요. 공개 저장소라 계정 ID가 Actions 로그에 찍히지 않도록 Variables가 아니라 Secrets에 둡니다. main 브랜치 보호는 하지 않습니다(혼자 하는 프로젝트, 11/23 결정).
4. **문서 올리기**: 로컬 `data/` 아래 경로가 그대로 S3 키입니다(`data/school/kb/x.md` → `school/kb/x.md`). 저장소 루트에서 `python scripts/sync_s3.py <data/ 아래 경로>`로 올립니다.
   - `school`: 학교 규정 원본(`raw/`)과 조문(`kb/`). `prepare_school_kb.py`가 끝나면 자동으로 올립니다.
   - `clubs/ana`: ANA 자료. 개인정보 가림 결과를 확인한 뒤 직접 올립니다.
   - `_sources`: 수집 원본 보관. `crawl_cnu_plus.py`가 끝나면 자동으로 올립니다.
   - KB가 읽는 폴더(kb-registry의 `s3_prefix`)는 로컬과 똑같이 맞추고(로컬에 없는 파일은 지움), 바뀐 게 있으면 KB 동기화(ingestion)까지 기다립니다. 그 밖의 폴더는 쌓기만 합니다.

## 앱 배포 (SAM)

앱은 저장소 루트의 `template.yaml`로 `club-rag-hub-app` 스택에 배포합니다. Lambda, HTTP API, Lambda 실행 역할, 로그 그룹(14일 보관)이 들어 있어요.

- **평소**: `template.yaml`, `src/`, `tests/`를 고쳐서 main에 push하면 `.github/workflows/deploy.yml`이 테스트 → `sam deploy` → `/health` 확인까지 합니다. Actions 화면에서 수동으로 돌릴 수도 있어요(workflow_dispatch).
- **이 PC에서 바로**: `bash deploy_app.sh`. 같은 인자로 배포하고 `/health`를 부른 뒤 주소를 `.state/outputs.env`의 `HEALTH_URL`에 적습니다.
- `GET /health`는 S3 문서 버킷 조회, kb-registry 조회, Titan 임베딩(1024차원) 호출을 한 번에 확인합니다. 모두 성공하면 200, 하나라도 실패하면 503이에요. 응답에는 계정 ID가 들어가지 않습니다.
- 배포 역할은 스택 이름(`club-rag-hub-app-*`)으로 시작하는 리소스만 다룹니다. 그래서 Lambda 이름은 `${AWS::StackName}-<이름>`으로 짓고, 실행 역할 이름은 비워 둡니다(CloudFormation이 스택 이름으로 시작하게 지어 줌). 관리형 정책은 `AWSLambdaBasicExecutionRole`만 붙일 수 있어요.

## MCP 서버 (AgentCore Gateway + Cognito)

Claude 앱이나 Claude Code가 접속하는 MCP 서버입니다. Gateway가 MCP 통신과 로그인 확인(Cognito 토큰)을 맡고, 도구가 불리면 앱 스택의 도구 Lambda(`club-rag-hub-app-tools`)를 부릅니다.

```
Claude 앱 / Claude Code ──HTTPS + 로그인──▶ AgentCore Gateway ① 토큰 확인 (Cognito)
                                             ② 인터셉터 Lambda ⇄ 토큰의 그룹을 입력값에 넣은 요청
                                             ③ 도구 Lambda 호출
```

순서:

1. `bash 07_cognito.sh`: 사용자 풀(메일 로그인, 가입 막음), 그룹 `ana`, 로그인 화면 도메인, 앱 클라이언트 2개, 연결 시험용 계정(`mcp-test@example.com`). 시험 계정 비밀번호는 `.state/mcp-test-user.env`에 적힙니다.
2. 앱 배포(`bash deploy_app.sh` 또는 main push): 도구 Lambda와 인터셉터 Lambda가 만들어집니다.
3. `bash 08_mcp_gateway.sh`: Gateway 실행 역할, Gateway(요청 인터셉터 연결), 대상(도구 Lambda)을 만들고 주소를 `.state/outputs.env`의 `GATEWAY_URL`에 적습니다.
4. `bash check_mcp.sh`: 토큰 없이 401과 로그인 안내가 오는지, 시험 계정으로 도구 목록과 ping 호출이 되는지, 로그인 그룹(`ana`)이 도구까지 오고 꾸민 그룹은 덮어쓰는지 확인합니다.

- 도구 설명(이름, 설명, 입력 형식)은 `src/tools/tool_schema.json`에 있습니다. 고치면 `08_mcp_gateway.sh`를 다시 실행해야 Gateway에 반영돼요. 도구 코드는 앱 배포로 반영됩니다.
- 앱 클라이언트는 두 개입니다. `club-rag-hub-mcp-claude`는 Claude가 브라우저 로그인에 쓰고(비밀값 없음, PKCE), `club-rag-hub-mcp-cli`는 점검 스크립트가 관리자 자격 증명으로 시험 계정 토큰을 받을 때 씁니다.
- Cognito는 자동 등록(DCR)을 지원하지 않아서, Claude에 커넥터를 추가할 때 `MCP_CLIENT_ID_CLAUDE` 값을 직접 넣습니다.
- 로그인 후 돌아갈 주소: `https://claude.ai/api/mcp/auth_callback`, `https://claude.com/api/mcp/auth_callback`, `http://localhost:8765/callback`(Claude Code, `--callback-port 8765`).
- **인터셉터가 필요한 이유**: Gateway는 로그인 토큰(`Authorization` 헤더)을 도구 Lambda로 넘기지 않고, 넘기는 설정도 막습니다. 그래서 Gateway가 도구 Lambda를 부르기 직전에 요청 인터셉터 Lambda(`club-rag-hub-app-interceptor`)를 부르고, 인터셉터가 토큰의 그룹(`cognito:groups`)을 도구 입력값 `caller_groups`에 넣습니다.
  - 도구 호출(tools/call)만 바꾸고 다른 요청은 그대로 통과시킵니다.
  - 클라이언트가 `caller_groups`를 꾸며 보내도 토큰 값으로 덮어씁니다. `caller_groups`는 도구 설명에 넣지 않아서 LLM에게 보이지 않습니다.
  - 도구 Lambda는 `caller_groups`가 없으면 그룹이 없는 사람으로 봅니다(공개 자료만).

## 주의

- 리소스를 만든 뒤에는 `config.env`의 `PREFIX`를 바꾸지 마세요. 이름이 달라져서 새로 만들어집니다.
- `.state/`와 `config.local.env`는 커밋하지 않습니다 (`.gitignore`에 있음).
- 배포 역할은 앱 스택 이름(`club-rag-hub-app-*`)으로 시작하는 리소스만 다루게 좁혔습니다. 다만 그 이름의 Lambda 역할에는 인라인 정책을 붙일 수 있어요. 이 부분은 나중에 권한 경계(permissions boundary)로 막을 수 있습니다.
