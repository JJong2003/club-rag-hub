// 화면과 상관없는 순수 함수 모음. 브라우저(app.js)와 테스트(app.test.mjs)가 같이 씁니다.

export const MAX_QUESTION = 1000;
export const NO_ANSWER_TEXT = '관련 규정을 찾지 못했습니다. 학생과나 총동아리연합회에 확인해 주세요.';
export const DEFAULT_SCOPE = 'openid email profile';

// ---------------------------------------------------------------- 질문

// 글자 수는 유니코드 문자 기준 (서버의 Python len과 같게)
export function countChars(text) {
  return [...String(text ?? '')].length;
}

// 앞뒤 공백을 지우고 1~1,000자인지 확인
export function validateQuestion(text) {
  const value = String(text ?? '').trim();
  if (!value) return { ok: false, value, error: '질문을 적어 주세요.' };
  if (countChars(value) > MAX_QUESTION) {
    return { ok: false, value, error: '질문을 1,000자 안으로 적어 주세요.' };
  }
  return { ok: true, value, error: '' };
}

// 줄바꿈과 겹친 공백을 한 칸으로
function oneLine(text) {
  return String(text ?? '').replace(/\s+/g, ' ').trim();
}

// '행사 기획' 입력칸을 한 질문 문장으로 합침. 행사 종류만 꼭 필요함
export function buildEventQuestion({ type, place, date, details } = {}) {
  const fields = [
    ['행사 종류', oneLine(type)],
    ['장소', oneLine(place)],
    ['날짜', oneLine(date)],
    ['내용', oneLine(details)],
  ];
  if (!fields[0][1]) return { ok: false, value: '', error: '행사 종류를 적어 주세요.' };
  const parts = fields.filter(([, v]) => v).map(([k, v]) => `${k}: ${v}`);
  const value = `[행사 기획] ${parts.join(' / ')}. `
    + '이 행사를 준비할 때 거쳐야 할 절차, 지켜야 할 학교 규정, 동아리에서 해 온 방식을 알려 주세요.';
  return validateQuestion(value);
}

// ---------------------------------------------------------------- 답변의 [n]

// 답변을 글과 출처 번호로 나눔. known에 있는 번호만 링크가 되고 나머지는 글로 둠
// 예: "쓸 수 없습니다 [1][2]." -> [{text}, {cite 1}, {cite 2}, {text}]
export function splitCitations(answer, known = []) {
  const text = String(answer ?? '');
  const ok = new Set(known.map(Number));
  const parts = [];
  const pattern = /\[(\d{1,3}(?:\s*,\s*\d{1,3})*)\]/g;
  let last = 0;
  const pushText = (s) => {
    if (!s) return;
    const prev = parts[parts.length - 1];
    if (prev && prev.type === 'text') prev.value += s;
    else parts.push({ type: 'text', value: s });
  };
  for (const m of text.matchAll(pattern)) {
    const nums = m[1].split(',').map((s) => Number(s.trim()));
    if (!nums.every((n) => ok.has(n))) continue; // 모르는 번호는 글 그대로
    pushText(text.slice(last, m.index));
    for (const n of nums) parts.push({ type: 'cite', n });
    last = m.index + m[0].length;
  }
  pushText(text.slice(last));
  return parts;
}

// ---------------------------------------------------------------- 링크 검사

// http, https 주소만 돌려주고 나머지(javascript:, data:, 상대 주소 등)는 null
export function safeUrl(url) {
  if (typeof url !== 'string' || !url.trim()) return null;
  try {
    const u = new URL(url.trim());
    return u.protocol === 'http:' || u.protocol === 'https:' ? u.href : null;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------- 출처 표시

export function kindLabel(kind) {
  if (kind === 'school_rule') return '학교 규정';
  if (kind === 'club_manual') return '동아리 자료';
  return '자료';
}

export function kbLabel(key) {
  if (key === 'school') return '학교 규정';
  if (String(key).startsWith('club')) return '동아리 자료';
  return String(key);
}

// 응답의 searched 배열을 "학교 규정 · 동아리 자료"처럼 (같은 이름은 한 번만)
export function searchedLabel(keys) {
  if (!Array.isArray(keys)) return '';
  return [...new Set(keys.map(kbLabel))].join(' · ');
}

// ---------------------------------------------------------------- 오류 문구

export const GENERIC_ERROR = '답변을 만들지 못했습니다. 잠시 후 다시 시도해 주세요.';

// status 0은 연결 실패, -1은 기다리다 끊음
export function errorMessage(status, body) {
  if (status === 400) {
    const msg = body && typeof body.error === 'string' ? body.error.trim().slice(0, 300) : '';
    return msg || '질문을 다시 확인해 주세요.';
  }
  if (status === 401) return '로그인 시간이 끝났습니다. 다시 로그인해 주세요.';
  if (status === 403) return '질문할 권한이 없습니다. 다시 로그인해도 같으면 임원진에게 알려 주세요.';
  if (status === 429) return '질문이 몰리고 있습니다. 잠시 후 다시 시도해 주세요.';
  if (status === 0) return '서버에 연결하지 못했습니다. 인터넷 연결을 확인하고 다시 시도해 주세요.';
  if (status === -1) return '답변이 너무 오래 걸려 멈췄습니다. 잠시 후 다시 시도해 주세요.';
  return GENERIC_ERROR;
}

// 200 응답이 약속한 모양인지 확인하고, 화면에 쓸 값만 골라냄
export function normalizeAnswer(body) {
  if (!body || typeof body.answer !== 'string') return null;
  const sources = Array.isArray(body.sources) ? body.sources : [];
  return {
    answer: body.answer,
    noAnswer: body.no_answer === true,
    searched: Array.isArray(body.searched) ? body.searched.map(String) : [],
    ms: Number.isFinite(body.ms) ? body.ms : null,
    sources: sources
      .filter((s) => s && Number.isInteger(s.n))
      .map((s) => ({
        n: s.n,
        kind: String(s.kind ?? ''),
        title: String(s.title ?? ''),
        detail: String(s.detail ?? ''),
        url: safeUrl(s.url),
        text: String(s.text ?? ''),
      })),
  };
}

// ---------------------------------------------------------------- PKCE, 로그인 주소

export function base64UrlEncode(bytes) {
  let bin = '';
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

// 무작위 문자열 (32바이트 -> 43자). code_verifier와 state에 씀
export function randomString(byteLength = 32) {
  const bytes = new Uint8Array(byteLength);
  crypto.getRandomValues(bytes);
  return base64UrlEncode(bytes);
}

// code_challenge = base64url(SHA-256(code_verifier))
export async function pkceChallenge(verifier) {
  const data = new TextEncoder().encode(verifier);
  const hash = await crypto.subtle.digest('SHA-256', data);
  return base64UrlEncode(new Uint8Array(hash));
}

// "https://x.auth...com/" 처럼 적어도 "x.auth...com"만 남김
export function normalizeDomain(domain) {
  return String(domain ?? '').trim().replace(/^https?:\/\//i, '').replace(/\/+$/, '');
}

export function buildAuthorizeUrl({ domain, clientId, redirectUri, state, challenge, scope = DEFAULT_SCOPE }) {
  const params = new URLSearchParams({
    response_type: 'code',
    client_id: clientId,
    redirect_uri: redirectUri,
    scope,
    state,
    code_challenge: challenge,
    code_challenge_method: 'S256',
  });
  return `https://${normalizeDomain(domain)}/oauth2/authorize?${params}`;
}

export function buildTokenRequest({ domain, clientId, code, redirectUri, verifier }) {
  return {
    url: `https://${normalizeDomain(domain)}/oauth2/token`,
    body: new URLSearchParams({
      grant_type: 'authorization_code',
      client_id: clientId,
      code,
      redirect_uri: redirectUri,
      code_verifier: verifier,
    }).toString(),
  };
}

export function buildLogoutUrl({ domain, clientId, logoutUri }) {
  const params = new URLSearchParams({ client_id: clientId, logout_uri: logoutUri });
  return `https://${normalizeDomain(domain)}/logout?${params}`;
}

// ---------------------------------------------------------------- 토큰 읽기 (표시용, 서명 확인은 서버가 함)

export function decodeJwt(token) {
  try {
    const part = String(token).split('.')[1];
    if (!part) return null;
    const b64 = part.replace(/-/g, '+').replace(/_/g, '/');
    const bin = atob(b64 + '='.repeat((4 - (b64.length % 4)) % 4));
    const bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0));
    const payload = JSON.parse(new TextDecoder().decode(bytes));
    return payload && typeof payload === 'object' ? payload : null;
  } catch {
    return null;
  }
}

// 만료 30초 전부터 끝난 것으로 봄
export function isExpired(payload, nowMs = Date.now(), skewSec = 30) {
  if (!payload || !Number.isFinite(payload.exp)) return true;
  return payload.exp - skewSec <= nowMs / 1000;
}

// ID 토큰에서 머리에 보일 메일과 그룹
export function userFromIdToken(idToken) {
  const p = decodeJwt(idToken);
  if (!p) return null;
  const groups = Array.isArray(p['cognito:groups']) ? p['cognito:groups'].map(String) : [];
  return { email: String(p.email || p['cognito:username'] || ''), groups };
}

// 설정값이 비었거나 config.example.js의 자리 표시 값(<...>)이면 false
export function isConfigured(cfg) {
  if (!cfg) return false;
  return ['apiUrl', 'cognitoDomain', 'clientId'].every((k) => {
    const v = cfg[k];
    return typeof v === 'string' && v.trim() !== '' && !/[<>]/.test(v);
  });
}

export function askUrl(apiUrl) {
  return `${String(apiUrl).trim().replace(/\/+$/, '')}/ask`;
}
