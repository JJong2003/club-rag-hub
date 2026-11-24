// 화면의 순수 로직 시험. 실행: node --test web/app.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import * as lib from './lib.js';
import { mockAsk } from './mock.js';

// ---------------------------------------------------------------- 질문 확인

test('질문은 앞뒤 공백을 지우고 1~1,000자만 통과', () => {
  assert.equal(lib.validateQuestion('').ok, false);
  assert.equal(lib.validateQuestion('   \n ').ok, false);
  assert.deepEqual(lib.validateQuestion('  전기난로?  '), { ok: true, value: '전기난로?', error: '' });
  assert.equal(lib.validateQuestion('가'.repeat(1000)).ok, true);
  const over = lib.validateQuestion('가'.repeat(1001));
  assert.equal(over.ok, false);
  assert.equal(over.error, '질문을 1,000자 안으로 적어 주세요.');
});

test('글자 수는 유니코드 문자 기준 (이모지 1개 = 1자)', () => {
  assert.equal(lib.countChars('😀'), 1);
  assert.equal(lib.validateQuestion('😀'.repeat(1000)).ok, true);
});

// ---------------------------------------------------------------- 행사 기획 합치기

test('행사 기획 입력칸을 한 질문 문장으로 합침', () => {
  const r = lib.buildEventQuestion({ type: ' MT ', place: '교외 펜션', date: '2026-12-05', details: '30명\n1박 2일' });
  assert.equal(r.ok, true);
  assert.ok(r.value.startsWith('[행사 기획] 행사 종류: MT / 장소: 교외 펜션 / 날짜: 2026-12-05 / 내용: 30명 1박 2일. '));
  assert.ok(!r.value.includes('\n'), '한 줄이어야 함');
  // 말풍선에는 입력한 칸만 짧게
  assert.equal(r.summary, '행사 종류: MT / 장소: 교외 펜션 / 날짜: 2026-12-05 / 내용: 30명 1박 2일');
});

test('행사 기획: 빈 칸은 빼고, 행사 종류는 꼭 필요', () => {
  const r = lib.buildEventQuestion({ type: '축제 부스', place: '', date: '  ', details: '' });
  assert.equal(r.ok, true);
  assert.ok(r.value.startsWith('[행사 기획] 행사 종류: 축제 부스. '));
  assert.ok(!r.value.includes('장소'));
  assert.equal(r.summary, '행사 종류: 축제 부스');

  const none = lib.buildEventQuestion({ place: '동아리방' });
  assert.equal(none.ok, false);
  assert.equal(none.error, '행사 종류를 적어 주세요.');
});

test('행사 기획: 합친 질문도 1,000자 제한', () => {
  const r = lib.buildEventQuestion({ type: 'MT', details: '가'.repeat(1000) });
  assert.equal(r.ok, false);
});

// ---------------------------------------------------------------- 화면 동작

test('이 PC 주소인지 (개발용 링크는 여기서만)', () => {
  for (const h of ['localhost', 'LOCALHOST', '127.0.0.1', '[::1]', 'app.localhost']) assert.equal(lib.isLocalHost(h), true, h);
  for (const h of ['d111111abcdef8.cloudfront.net', 'localhost.example.com', '127.0.0.2', '', null]) assert.equal(lib.isLocalHost(h), false, String(h));
});

test('기다리는 동안 문구: 자료 찾기 -> 1.5초 뒤 답변 쓰기, 초는 내림, 12초부터 오래 걸림 안내', () => {
  assert.deepEqual(lib.pendingStatus(0), { text: '자료를 찾고 있어요', seconds: 0, slow: false });
  assert.equal(lib.pendingStatus(1499).text, '자료를 찾고 있어요');
  assert.equal(lib.pendingStatus(1500).text, '답변을 쓰고 있어요');
  assert.equal(lib.pendingStatus(5999).seconds, 5);
  assert.equal(lib.pendingStatus(11999).slow, false);
  assert.equal(lib.pendingStatus(12000).slow, true);
  assert.equal(lib.pendingStatus(-5).seconds, 0);
});

test('답변이 오면: 다 들어가면 끝까지, 길면 답변 처음이 위에 (맨 아래로 가지 않음)', () => {
  const view = { viewHeight: 600, pad: 16 };
  // 짧은 질문 + 짧은 답변: 답변 끝이 아래에 오게 (= 맨 아래)
  assert.equal(lib.answerScrollTop({ ...view, questionTop: 1000, answerTop: 1060, answerBottom: 1400, maxScroll: 816 }), 816);
  // 짧은 질문 + 긴 답변: 질문 처음이 위에
  assert.equal(lib.answerScrollTop({ ...view, questionTop: 1000, answerTop: 1060, answerBottom: 2400, maxScroll: 1816 }), 984);
  // 긴 질문(화면 1/3보다 김) + 긴 답변: 답변 처음이 위에
  assert.equal(lib.answerScrollTop({ ...view, questionTop: 1000, answerTop: 1300, answerBottom: 2400, maxScroll: 1816 }), 1284);
  // 0보다 작거나 끝을 넘지 않음
  assert.equal(lib.answerScrollTop({ ...view, questionTop: 10, answerTop: 70, answerBottom: 900, maxScroll: 316 }), 0);
  assert.equal(lib.answerScrollTop({ ...view, questionTop: 10, answerTop: 70, answerBottom: 300, maxScroll: 0 }), 0);
});

// ---------------------------------------------------------------- [n] 나누기

test('답변의 [n]을 글과 출처 번호로 나눔', () => {
  const parts = lib.splitCitations('쓸 수 없습니다 [1]. 허가가 필요합니다 [2][3].', [1, 2, 3]);
  assert.deepEqual(parts, [
    { type: 'text', value: '쓸 수 없습니다 ' },
    { type: 'cite', n: 1 },
    { type: 'text', value: '. 허가가 필요합니다 ' },
    { type: 'cite', n: 2 },
    { type: 'cite', n: 3 },
    { type: 'text', value: '.' },
  ]);
});

test('출처 목록에 없는 번호는 글 그대로 둠', () => {
  const parts = lib.splitCitations('A [1] B [9] C', [1]);
  assert.deepEqual(parts, [
    { type: 'text', value: 'A ' },
    { type: 'cite', n: 1 },
    { type: 'text', value: ' B [9] C' },
  ]);
});

test('[1, 2] 모양도 번호 두 개로', () => {
  const parts = lib.splitCitations('끝 [1, 2]', [1, 2]);
  assert.deepEqual(parts, [{ type: 'text', value: '끝 ' }, { type: 'cite', n: 1 }, { type: 'cite', n: 2 }]);
});

test('번호가 없으면 글 하나, 빈 답변은 빈 배열', () => {
  assert.deepEqual(lib.splitCitations('출처 없음', [1]), [{ type: 'text', value: '출처 없음' }]);
  assert.deepEqual(lib.splitCitations('', []), []);
  assert.deepEqual(lib.splitCitations(null, []), []);
});

// ---------------------------------------------------------------- 링크 검사

test('링크는 http, https만 허용', () => {
  assert.equal(lib.safeUrl('https://www.law.go.kr/x?a=1'), 'https://www.law.go.kr/x?a=1');
  assert.equal(lib.safeUrl('http://example.com'), 'http://example.com/');
  for (const bad of [
    'javascript:alert(1)',
    'JAVASCRIPT:alert(1)',
    '  javascript:alert(1)',
    'java\tscript:alert(1)',
    'data:text/html,<script>alert(1)</script>',
    'vbscript:msgbox(1)',
    'file:///C:/Windows',
    '/relative/path',
    '//evil.example.com',
    'not a url',
    '',
    null,
    undefined,
    42,
  ]) {
    assert.equal(lib.safeUrl(bad), null, `막아야 함: ${String(bad)}`);
  }
});

// ---------------------------------------------------------------- 응답 정리

test('normalizeAnswer: 약속한 모양만 받고 링크를 검사함', () => {
  const a = lib.normalizeAnswer({
    answer: '답 [1]',
    sources: [
      { n: 1, kind: 'school_rule', title: 'T', detail: 'D', url: 'javascript:alert(1)', text: '근거' },
      { n: 'x', title: '번호가 숫자가 아님' },
    ],
    searched: ['school'],
    no_answer: false,
    ms: 1234,
  });
  assert.equal(a.answer, '답 [1]');
  assert.equal(a.sources.length, 1);
  assert.equal(a.sources[0].url, null);
  assert.equal(a.ms, 1234);
  assert.equal(lib.normalizeAnswer({ sources: [] }), null);
  assert.equal(lib.normalizeAnswer(null), null);
});

test('표시 이름', () => {
  assert.equal(lib.kindLabel('school_rule'), '학교 규정');
  assert.equal(lib.kindLabel('club_manual'), '동아리 자료');
  assert.equal(lib.searchedLabel(['school', 'club-ana']), '학교 규정 · 동아리 자료');
  assert.equal(lib.searchedLabel(['club-ana', 'club-b']), '동아리 자료');
});

test('오류 문구: 400은 서버 메시지, 401 다시 로그인, 429 잠시 후, 그 밖은 일반 문구', () => {
  assert.equal(lib.errorMessage(400, { error: '질문을 1,000자 안으로 적어 주세요.' }), '질문을 1,000자 안으로 적어 주세요.');
  assert.equal(lib.errorMessage(400, null), '질문을 다시 확인해 주세요.');
  assert.match(lib.errorMessage(401, { message: 'Unauthorized' }), /다시 로그인/);
  assert.match(lib.errorMessage(429, null), /잠시 후/);
  assert.equal(lib.errorMessage(500, { error: '원문 오류' }), lib.GENERIC_ERROR);
  assert.equal(lib.errorMessage(503, null), lib.GENERIC_ERROR);
  assert.match(lib.errorMessage(0, null), /연결/);
});

// ---------------------------------------------------------------- PKCE, 로그인 주소

test('PKCE: RFC 7636 부록 B의 예시 값과 같은 challenge', async () => {
  const verifier = 'dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk';
  assert.equal(await lib.pkceChallenge(verifier), 'E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM');
});

test('PKCE: verifier는 43자 base64url이고 매번 다름', () => {
  const a = lib.randomString(32);
  const b = lib.randomString(32);
  assert.equal(a.length, 43);
  assert.match(a, /^[A-Za-z0-9_-]+$/);
  assert.notEqual(a, b);
  assert.equal(lib.base64UrlEncode(new Uint8Array([251, 255])), '-_8');
});

test('authorize, token, logout 주소', () => {
  const domain = 'https://example.auth.ap-northeast-2.amazoncognito.com/';
  const url = new URL(lib.buildAuthorizeUrl({
    domain, clientId: 'cid', redirectUri: 'http://localhost:5500/', state: 'st', challenge: 'ch',
  }));
  assert.equal(url.origin + url.pathname, 'https://example.auth.ap-northeast-2.amazoncognito.com/oauth2/authorize');
  assert.equal(url.searchParams.get('response_type'), 'code');
  assert.equal(url.searchParams.get('client_id'), 'cid');
  assert.equal(url.searchParams.get('redirect_uri'), 'http://localhost:5500/');
  assert.equal(url.searchParams.get('scope'), 'openid email profile');
  assert.equal(url.searchParams.get('state'), 'st');
  assert.equal(url.searchParams.get('code_challenge'), 'ch');
  assert.equal(url.searchParams.get('code_challenge_method'), 'S256');

  const t = lib.buildTokenRequest({ domain, clientId: 'cid', code: 'c0de', redirectUri: 'http://localhost:5500/', verifier: 'v' });
  assert.equal(t.url, 'https://example.auth.ap-northeast-2.amazoncognito.com/oauth2/token');
  const body = new URLSearchParams(t.body);
  assert.equal(body.get('grant_type'), 'authorization_code');
  assert.equal(body.get('client_id'), 'cid');
  assert.equal(body.get('code'), 'c0de');
  assert.equal(body.get('redirect_uri'), 'http://localhost:5500/');
  assert.equal(body.get('code_verifier'), 'v');

  const out = new URL(lib.buildLogoutUrl({ domain, clientId: 'cid', logoutUri: 'http://localhost:5500/' }));
  assert.equal(out.pathname, '/logout');
  assert.equal(out.searchParams.get('client_id'), 'cid');
  assert.equal(out.searchParams.get('logout_uri'), 'http://localhost:5500/');
});

// ---------------------------------------------------------------- 토큰 읽기

function fakeJwt(payload) {
  const enc = (o) => Buffer.from(JSON.stringify(o)).toString('base64url');
  return `${enc({ alg: 'none' })}.${enc(payload)}.sig`;
}

test('ID 토큰에서 메일과 그룹 (한글 포함)', () => {
  const token = fakeJwt({ email: 'a@example.com', 'cognito:groups': ['ana'], name: '홍길동' });
  assert.deepEqual(lib.userFromIdToken(token), { email: 'a@example.com', groups: ['ana'] });
  assert.equal(lib.decodeJwt(token).name, '홍길동');
  assert.deepEqual(lib.userFromIdToken(fakeJwt({ email: 'b@example.com' })).groups, []);
  assert.equal(lib.decodeJwt('not-a-jwt'), null);
  assert.equal(lib.decodeJwt('a.%%%.c'), null);
});

test('토큰 만료 판단 (30초 여유)', () => {
  const now = 1_800_000_000_000;
  assert.equal(lib.isExpired({ exp: now / 1000 + 3600 }, now), false);
  assert.equal(lib.isExpired({ exp: now / 1000 + 10 }, now), true);
  assert.equal(lib.isExpired({ exp: now / 1000 - 1 }, now), true);
  assert.equal(lib.isExpired(null, now), true);
});

test('설정 확인: 자리 표시 값은 설정 안 된 것으로', () => {
  assert.equal(lib.isConfigured(undefined), false);
  assert.equal(lib.isConfigured({ apiUrl: 'https://<api-id>.x', cognitoDomain: 'd', clientId: 'c' }), false);
  assert.equal(lib.isConfigured({ apiUrl: 'https://a.x', cognitoDomain: 'd', clientId: '' }), false);
  assert.equal(lib.isConfigured({ apiUrl: 'https://a.x', cognitoDomain: 'd', clientId: 'c' }), true);
  assert.equal(lib.askUrl('https://a.x/'), 'https://a.x/ask');
});

// ---------------------------------------------------------------- 가짜 응답이 약속한 모양인지

// index.html의 질문 예시 버튼: [질문, 그룹이 있어야 보이는지]
function exampleChips() {
  const html = readFileSync(new URL('./index.html', import.meta.url), 'utf8');
  return [...html.matchAll(/<li( data-needs-group[^>]*)?><button[^>]*data-example="([^"]+)"/g)].map((m) => [m[2], !!m[1]]);
}

test('질문 예시: 학교 규정 질문은 모두에게, 동아리 질문은 그룹이 있을 때만', () => {
  const chips = exampleChips();
  assert.deepEqual(chips, [
    ['동아리방에서 전기난로를 써도 되나요?', false],
    ['휴학은 몇 학기까지 할 수 있나요?', false],
    ['총무는 어떤 일을 하나요?', true],
    ['가을 MT 장소 후보는 어디였나요?', true],
  ]);
  // 시연 모드에서도 예시마다 근거 있는 답이 나옴
  for (const [q, needsGroup] of chips) {
    const { status, body } = mockAsk(q, 'qa');
    assert.equal(status, 200, q);
    assert.equal(body.no_answer, false, q);
    const kinds = new Set(body.sources.map((s) => s.kind));
    if (needsGroup) assert.ok(kinds.has('club_manual'), `${q}: 동아리 자료 출처`);
    else assert.ok(kinds.has('school_rule'), `${q}: 학교 규정 출처`);
  }
});

test('가짜 응답은 POST /ask 약속과 같은 모양', () => {
  for (const [q, mode] of [['전기난로?', 'qa'], ['[행사 기획] 행사 종류: MT', 'event'], ['회의록?', 'qa'], ...exampleChips().map(([c]) => [c, 'qa'])]) {
    const { status, body } = mockAsk(q, mode);
    assert.equal(status, 200);
    assert.equal(typeof body.answer, 'string');
    assert.equal(body.no_answer, false);
    assert.ok(Array.isArray(body.searched));
    assert.ok(Number.isInteger(body.ms));
    const used = new Set(lib.splitCitations(body.answer, body.sources.map((s) => s.n))
      .filter((p) => p.type === 'cite').map((p) => p.n));
    for (const s of body.sources) {
      assert.deepEqual(Object.keys(s).sort(), ['detail', 'kind', 'n', 'text', 'title', 'url']);
      assert.ok(['school_rule', 'club_manual'].includes(s.kind));
      assert.ok(used.has(s.n), '답변에 쓰인 번호만 출처로');
      assert.ok(lib.safeUrl(s.url));
    }
  }
  const kinds = mockAsk('전기난로?', 'qa').body.sources.map((s) => s.kind);
  assert.ok(kinds.includes('school_rule') && kinds.includes('club_manual'), '학교 규정과 동아리 자료가 섞여야 함');
});

test('가짜 응답: 근거 없음, 오류', () => {
  const na = mockAsk('내일 날씨', 'qa');
  assert.equal(na.status, 200);
  assert.equal(na.body.no_answer, true);
  assert.equal(na.body.answer, lib.NO_ANSWER_TEXT);
  assert.deepEqual(na.body.sources, []);
  assert.equal(mockAsk('오류 시험').status, 500);
  assert.equal(mockAsk('로그인 만료').status, 401);
  assert.equal(mockAsk('요청 제한').status, 429);
  assert.equal(mockAsk('잘못된 요청').status, 400);
});

// ---------------------------------------------------------------- 보안: HTML로 넣는 코드가 없는지

test('화면 코드에 innerHTML 같은 HTML 삽입이 없음', () => {
  for (const file of ['app.js', 'lib.js', 'mock.js']) {
    const src = readFileSync(new URL(`./${file}`, import.meta.url), 'utf8');
    for (const bad of ['innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write', 'eval(', 'new Function']) {
      assert.ok(!src.includes(bad), `${file}에 ${bad}가 있음`);
    }
  }
});
