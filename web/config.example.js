// 화면 설정 예시. 배포 때 이 모양으로 web/config.js를 만듭니다.
// 로컬에서는 이 파일을 config.js로 복사해 값을 채우세요. config.js는 .gitignore에 있어 커밋되지 않습니다.
// 공개 저장소이므로 이 파일에는 실제 값을 적지 마세요.
window.APP_CONFIG = {
  // HTTP API 주소. 끝에 /ask를 붙여 부릅니다
  apiUrl: 'https://<api-id>.execute-api.ap-northeast-2.amazonaws.com',
  // Cognito 도메인 (https:// 없이)
  cognitoDomain: '<domain-prefix>.auth.ap-northeast-2.amazoncognito.com',
  // Cognito 웹 앱 클라이언트 ID (비밀값 없는 공개 클라이언트)
  clientId: '<web-app-client-id>',
  // 로그인·로그아웃 뒤 돌아올 주소. 앱 클라이언트의 콜백 URL, 로그아웃 URL에 똑같이 등록해야 합니다
  redirectUri: 'http://localhost:5500/',
  // true면 로그인 없이 가짜 응답으로 화면만 봅니다 (주소에 ?mock=1을 붙여도 같음)
  mock: false,
  // 고르기: logoutUri (기본은 redirectUri), scope (기본 'openid email profile')
};
