// 동아리 운영 도우미 화면.
// 보안: 서버에서 받은 글(답변, 출처)은 textContent와 DOM 생성으로만 넣습니다. HTML 문자열로 넣지 않습니다.

import * as lib from './lib.js';
import { mockAsk } from './mock.js';

// ---------------------------------------------------------------- 설정

const cfg = window.APP_CONFIG || {};
const params = new URLSearchParams(location.search);
const MOCK = cfg.mock === true || params.get('mock') === '1';
const DEMO = MOCK && params.get('demo') === '1';
const REDIRECT_URI = cfg.redirectUri || new URL('./', location.href).href;
const LOGOUT_URI = cfg.logoutUri || REDIRECT_URI;
const SCOPE = cfg.scope || lib.DEFAULT_SCOPE;
const REQUEST_TIMEOUT_MS = 40000; // HTTP API는 30초에 끊으므로 그보다 조금 길게
// 터치 화면에서는 Enter가 줄바꿈 (보내기는 버튼)
const TOUCH = window.matchMedia('(pointer: coarse)').matches;
const ENTER_SENDS = !TOUCH;
const REDUCE_MOTION = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
// 이 PC에서 띄운 화면 (개발용 링크와 자세한 설정 안내는 여기서만)
const LOCAL = lib.isLocalHost(location.hostname);

const KEY = {
  id: 'crh.idToken',
  access: 'crh.accessToken',
  verifier: 'crh.pkce.verifier',
  state: 'crh.pkce.state',
};

// sessionStorage를 못 쓰는 브라우저(일부 비공개 창)에서도 멈추지 않게
const store = {
  get(k) { try { return sessionStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { sessionStorage.setItem(k, v); return true; } catch { return false; } },
  del(k) { try { sessionStorage.removeItem(k); } catch { /* 무시 */ } },
};

// ---------------------------------------------------------------- 화면 요소

const $ = (id) => document.getElementById(id);
const els = {
  userBox: $('user-box'),
  userEmail: $('user-email'),
  userGroups: $('user-groups'),
  mockBadge: $('mock-badge'),
  logoutButton: $('logout-button'),
  loginView: $('login-view'),
  loginMessage: $('login-message'),
  loginButton: $('login-button'),
  loginHelp: $('login-help'),
  mockLinkBox: $('mock-link-box'),
  chatView: $('chat-view'),
  messages: $('messages'),
  emptyState: $('empty-state'),
  emptyLead: $('empty-lead'),
  groupExamples: [...document.querySelectorAll('[data-needs-group]')],
  mockHint: $('mock-hint'),
  composer: $('composer'),
  modeButtons: [...document.querySelectorAll('.mode-btn')],
  qaFields: $('qa-fields'),
  eventFields: $('event-fields'),
  question: $('question'),
  evType: $('ev-type'),
  evPlace: $('ev-place'),
  evDate: $('ev-date'),
  evDetails: $('ev-details'),
  send: $('send-button'),
  hint: $('composer-hint'),
  count: $('question-count'),
  composerError: $('composer-error'),
  announcer: $('announcer'),
};

let mode = 'qa';
let busy = false;
let msgSeq = 0;
let mockLoggedIn = true;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function srOnly(text) {
  return el('span', 'sr-only', text);
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// ---------------------------------------------------------------- 로그인 상태

function clearTokens() {
  store.del(KEY.access);
  store.del(KEY.id);
}

// null: 로그인 안 함, { expired: true }: 토큰 만료, 그 밖: { user, accessToken }
function currentSession() {
  if (MOCK) {
    return mockLoggedIn ? { user: { email: '시연 계정', groups: ['ana'] }, accessToken: 'mock' } : null;
  }
  const access = store.get(KEY.access);
  const id = store.get(KEY.id);
  if (!access || !id) return null;
  if (lib.isExpired(lib.decodeJwt(access))) return { expired: true };
  return { user: lib.userFromIdToken(id) || { email: '', groups: [] }, accessToken: access };
}

async function startLogin() {
  if (MOCK) {
    mockLoggedIn = true;
    showChat(currentSession());
    return;
  }
  if (!lib.isConfigured(cfg)) {
    showSetupNeeded();
    return;
  }
  if (!window.isSecureContext || !window.crypto || !crypto.subtle) {
    showLogin('보안 연결(HTTPS 또는 localhost)에서만 로그인할 수 있습니다.');
    return;
  }
  const verifier = lib.randomString(32);
  const state = lib.randomString(16);
  if (!store.set(KEY.verifier, verifier) || !store.set(KEY.state, state)) {
    showLogin('브라우저 저장소를 쓸 수 없어 로그인할 수 없습니다. 비공개 창 설정을 확인해 주세요.');
    return;
  }
  const challenge = await lib.pkceChallenge(verifier);
  location.assign(lib.buildAuthorizeUrl({
    domain: cfg.cognitoDomain,
    clientId: cfg.clientId,
    redirectUri: REDIRECT_URI,
    state,
    challenge,
    scope: SCOPE,
  }));
}

// 로그인 화면에서 돌아왔을 때 코드를 토큰으로 바꿈.
// 돌아온 게 아니면 null, 성공하면 '', 실패하면 안내 문구
async function handleCallback() {
  const code = params.get('code');
  const state = params.get('state');
  const error = params.get('error');
  if (!code && !error) return null;

  // 주소창과 방문 기록에서 코드를 바로 지움
  history.replaceState(null, '', location.pathname);
  const savedState = store.get(KEY.state);
  const verifier = store.get(KEY.verifier);
  store.del(KEY.state);
  store.del(KEY.verifier);

  if (error) {
    const desc = (params.get('error_description') || error).slice(0, 200);
    return `로그인하지 못했습니다. (${desc})`;
  }
  if (!state || !savedState || state !== savedState || !verifier) {
    return '로그인 확인 값이 맞지 않습니다. 다시 로그인해 주세요.';
  }

  showLogin('로그인하는 중입니다…', { busy: true });
  try {
    const req = lib.buildTokenRequest({
      domain: cfg.cognitoDomain,
      clientId: cfg.clientId,
      code,
      redirectUri: REDIRECT_URI,
      verifier,
    });
    const res = await fetch(req.url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: req.body,
    });
    const data = await res.json().catch(() => null);
    if (!res.ok || !data || !data.access_token || !data.id_token) {
      return '로그인을 마치지 못했습니다. 다시 로그인해 주세요.';
    }
    // refresh token은 두지 않음. 만료되면 다시 로그인
    store.set(KEY.access, data.access_token);
    store.set(KEY.id, data.id_token);
    return '';
  } catch {
    return '로그인 서버에 연결하지 못했습니다. 다시 시도해 주세요.';
  }
}

function logout() {
  clearTokens();
  if (MOCK) {
    mockLoggedIn = false;
    resetChat();
    showLogin('로그아웃했습니다.');
    return;
  }
  if (lib.isConfigured(cfg)) {
    location.assign(lib.buildLogoutUrl({ domain: cfg.cognitoDomain, clientId: cfg.clientId, logoutUri: LOGOUT_URI }));
  } else {
    showLogin('로그아웃했습니다.');
  }
}

// ---------------------------------------------------------------- 화면 전환

function showLogin(message = '', { busy: loading = false, setup = false } = {}) {
  els.chatView.hidden = true;
  els.userBox.hidden = true;
  els.loginView.hidden = false;
  els.loginMessage.textContent = message;
  els.loginMessage.hidden = !message;
  els.loginButton.textContent = MOCK ? '로그인 (시연)' : '로그인';
  els.loginButton.hidden = loading || setup;
  els.loginHelp.hidden = loading || setup || MOCK;
  els.mockLinkBox.hidden = MOCK || loading || !LOCAL;
}

function showSetupNeeded() {
  showLogin(
    LOCAL
      ? '설정 파일(config.js)이 없어서 로그인할 수 없습니다. config.example.js를 config.js로 복사해 값을 채워 주세요.'
      : '지금은 로그인할 수 없습니다. 화면 설정이 끝나지 않았습니다. 임원진에게 알려 주세요.',
    { setup: true },
  );
}

function showChat(session) {
  els.loginView.hidden = true;
  els.chatView.hidden = false;
  els.userBox.hidden = false;
  els.mockBadge.hidden = !MOCK;
  els.mockHint.hidden = !MOCK;
  els.userEmail.textContent = session.user.email || '로그인됨';
  els.userEmail.title = session.user.email || '';
  const groups = session.user.groups.length
    ? session.user.groups.map((g) => el('span', 'group', g))
    : [el('span', 'group none', '그룹 없음')];
  els.userGroups.replaceChildren(...groups);
  // 그룹은 보여 주기만 함. 어떤 자료를 찾을지는 서버가 토큰으로 정함
  // 그룹이 없으면 동아리 자료를 찾지 못하므로 동아리 질문 예시와 안내를 뺌
  const hasGroup = session.user.groups.length > 0;
  for (const li of els.groupExamples) li.hidden = !hasGroup;
  els.emptyLead.textContent = hasGroup
    ? '학교 규정과 동아리 자료에서 근거를 찾아 답하고, 문장 끝에 출처 번호를 붙입니다.'
    : '학교 규정에서 근거를 찾아 답하고, 문장 끝에 출처 번호를 붙입니다.';
}

function resetChat() {
  els.messages.replaceChildren(els.emptyState);
  els.emptyState.hidden = false;
  msgSeq = 0;
}

// ---------------------------------------------------------------- 입력칸

function setMode(next, { focus = true } = {}) {
  mode = next;
  for (const b of els.modeButtons) b.setAttribute('aria-pressed', String(b.dataset.mode === next));
  els.qaFields.hidden = next !== 'qa';
  els.eventFields.hidden = next !== 'event';
  els.composer.classList.toggle('is-event', next === 'event');
  hideComposerError();
  updateCount();
  if (focus) (next === 'qa' ? els.question : els.evType).focus();
}

function eventValues() {
  return {
    type: els.evType.value,
    place: els.evPlace.value,
    date: els.evDate.value,
    details: els.evDetails.value,
  };
}

function readComposer() {
  return mode === 'qa' ? lib.validateQuestion(els.question.value) : lib.buildEventQuestion(eventValues());
}

function updateCount() {
  let n = 0;
  if (mode === 'qa') {
    n = lib.countChars(els.question.value.trim());
  } else {
    const r = lib.buildEventQuestion(eventValues());
    n = r.value ? lib.countChars(r.value) : 0;
  }
  els.count.textContent = `${n.toLocaleString('ko-KR')} / ${lib.MAX_QUESTION.toLocaleString('ko-KR')}자`;
  els.count.classList.toggle('over', n > lib.MAX_QUESTION);
}

// 글이 늘면 칸도 늘림 (최대 높이는 style.css의 max-height)
function autoGrow(textarea) {
  textarea.style.height = 'auto';
  const max = parseFloat(getComputedStyle(textarea).maxHeight) || 200;
  const border = textarea.offsetHeight - textarea.clientHeight;
  textarea.style.height = `${Math.min(textarea.scrollHeight + border, max)}px`;
}

function clearComposer() {
  if (mode === 'qa') {
    els.question.value = '';
    autoGrow(els.question);
  } else {
    for (const f of [els.evType, els.evPlace, els.evDate, els.evDetails]) f.value = '';
    autoGrow(els.evDetails);
  }
  updateCount();
}

function showComposerError(message) {
  els.composerError.textContent = message;
  els.composerError.hidden = false;
}

function hideComposerError() {
  els.composerError.hidden = true;
  els.composerError.textContent = '';
}

// 기다리는 동안 보내기만 막음 (버튼 글자는 그대로 두어 입력칸 폭이 흔들리지 않게)
function setBusy(on) {
  busy = on;
  els.send.disabled = on;
  els.composer.setAttribute('aria-busy', String(on));
}

// Enter 보내기, Shift+Enter 줄바꿈. 한글 조합 중 Enter는 무시 (마지막 글자가 두 번 들어가는 문제)
function onFieldKeydown(e) {
  if (e.key !== 'Enter' || e.shiftKey || e.isComposing || e.keyCode === 229) return;
  const isTextarea = e.target.tagName === 'TEXTAREA';
  if (isTextarea && !ENTER_SENDS) return;
  e.preventDefault();
  submitQuestion();
}

// ---------------------------------------------------------------- 질문 보내기

async function submitQuestion() {
  if (busy) return;
  const r = readComposer();
  if (!r.ok) {
    showComposerError(r.error);
    (mode === 'qa' ? els.question : els.evType).focus();
    return;
  }
  hideComposerError();

  const session = currentSession();
  if (!session || session.expired) {
    if (session && session.expired) clearTokens();
    appendLoginNotice(session && session.expired
      ? '로그인 시간이 끝났습니다. 다시 로그인한 뒤 질문해 주세요.'
      : '로그인이 필요합니다. 로그인한 뒤 질문해 주세요.');
    return;
  }

  // 말풍선에는 입력한 내용만 (행사 기획은 합친 긴 질문 대신 칸 내용)
  const req = { question: r.value, mode, shown: mode === 'event' ? r.summary : r.value };
  clearComposer();
  // 행사 기획을 보낸 뒤에는 질문 탭으로 돌아가 입력칸이 답변을 가리지 않게
  if (mode === 'event') setMode('qa', { focus: false });
  afterSend();
  await ask(req);
}

// 보낸 뒤: 컴퓨터는 다음 질문을 바로 쓰게 질문 칸에 포커스,
// 휴대폰은 키보드가 답변을 가리지 않게 닫음
function afterSend() {
  if (TOUCH) {
    if (document.activeElement && document.activeElement !== document.body) document.activeElement.blur();
  } else {
    els.question.focus({ preventScroll: true });
  }
}

// 질문 말풍선 -> 기다림 표시 -> 답변 또는 오류
// req: { question: 보낼 질문, mode: 'qa' | 'event', shown: 말풍선 글 }
async function ask(req, { delay } = {}) {
  setBusy(true);
  els.emptyState.hidden = true;
  const userNode = appendUser(req.shown || req.question, req.mode);
  const pending = appendPending();
  announce('자료를 찾고 있어요.');

  let result;
  try {
    result = await callAsk(req.question, req.mode, delay);
  } finally {
    pending.stop();
  }

  const answerNode = renderResult(result, req);
  pending.node.replaceWith(answerNode);
  setBusy(false);
  revealAnswer(userNode, answerNode);
}

// 답변 말풍선의 처음이 보이게 (긴 답변이어도 맨 아래로 내려가지 않게)
function revealAnswer(userNode, answerNode) {
  const box = els.messages;
  const boxTop = box.getBoundingClientRect().top - box.scrollTop;
  const q = userNode.getBoundingClientRect();
  const a = answerNode.getBoundingClientRect();
  const top = lib.answerScrollTop({
    questionTop: q.top - boxTop,
    answerTop: a.top - boxTop,
    answerBottom: a.bottom - boxTop,
    viewHeight: box.clientHeight,
    maxScroll: box.scrollHeight - box.clientHeight,
  });
  box.scrollTo({ top, behavior: REDUCE_MOTION || DEMO ? 'auto' : 'smooth' });
}

// 실제 /ask 또는 가짜 응답. 둘 다 { status, body }
async function callAsk(question, usedMode, delay) {
  if (MOCK) {
    await sleep(delay ?? 1000 + Math.random() * 1000);
    return mockAsk(question, usedMode);
  }
  const session = currentSession();
  if (!session || session.expired) return { status: 401, body: null };

  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), REQUEST_TIMEOUT_MS);
  try {
    const res = await fetch(lib.askUrl(cfg.apiUrl), {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${session.accessToken}`,
      },
      body: JSON.stringify({ question, mode: usedMode }),
      signal: ctrl.signal,
    });
    const body = await res.json().catch(() => null);
    return { status: res.status, body };
  } catch (e) {
    return { status: e && e.name === 'AbortError' ? -1 : 0, body: null };
  } finally {
    clearTimeout(timer);
  }
}

// ---------------------------------------------------------------- 대화 그리기

function scrollToBottom() {
  els.messages.scrollTo({ top: els.messages.scrollHeight, behavior: REDUCE_MOTION || DEMO ? 'auto' : 'smooth' });
}

// 같은 문구도 다시 읽히게 비웠다가 넣음
function announce(text) {
  els.announcer.textContent = '';
  setTimeout(() => { els.announcer.textContent = text; }, 60);
}

function appendUser(question, usedMode) {
  const art = el('article', 'msg user');
  art.setAttribute('aria-label', '내 질문');
  const bubble = el('div', 'bubble');
  if (usedMode === 'event') bubble.append(el('span', 'tag', '행사 기획'));
  bubble.append(el('p', 'user-text', question));
  art.append(bubble);
  els.messages.append(art);
  return art;
}

// 기다리는 동안: 단계 문구(자료 찾기 -> 답변 쓰기)와 지난 시간
function appendPending() {
  const art = el('article', 'msg bot pending');
  art.setAttribute('aria-label', '답변 기다리는 중');
  const bubble = el('div', 'bubble');
  const line = el('div', 'pending-line');
  const dots = el('span', 'dots');
  dots.setAttribute('aria-hidden', 'true');
  dots.append(el('i'), el('i'), el('i'));
  const text = el('span', 'pending-text');
  const elapsed = el('span', 'elapsed');
  elapsed.setAttribute('aria-hidden', 'true');
  line.append(dots, text, elapsed);
  const slow = el('p', 'slow-hint', '평소보다 오래 걸리고 있어요. 조금만 더 기다려 주세요.');
  slow.hidden = true;
  bubble.append(line, slow);
  art.append(bubble);
  els.messages.append(art);
  scrollToBottom();

  const started = Date.now();
  const update = () => {
    const st = lib.pendingStatus(Date.now() - started);
    if (text.textContent !== st.text) text.textContent = st.text;
    elapsed.textContent = `${st.seconds}초`;
    slow.hidden = !st.slow;
  };
  update();
  const timer = setInterval(update, 250);
  return { node: art, stop: () => clearInterval(timer) };
}

function renderResult({ status, body }, req) {
  if (status === 200) {
    const a = lib.normalizeAnswer(body);
    if (a) return renderAnswer(a);
    return renderError(lib.GENERIC_ERROR, 500, req); // 약속과 다른 모양
  }
  return renderError(lib.errorMessage(status, body), status, req);
}

function metaLine(a) {
  const parts = [];
  const searched = lib.searchedLabel(a.searched);
  if (searched) parts.push(`찾아본 자료: ${searched}`);
  if (a.ms !== null) parts.push(`${(a.ms / 1000).toFixed(1)}초`);
  return parts.length ? el('p', 'meta', parts.join(' · ')) : null;
}

function renderAnswer(a) {
  const id = ++msgSeq;
  const art = el('article', 'msg bot');
  art.setAttribute('aria-label', a.noAnswer ? '답변 (근거 없음)' : '답변');
  const bubble = el('div', a.noAnswer ? 'bubble no-answer' : 'bubble');

  if (a.noAnswer) {
    const row = el('div', 'notice-row');
    const icon = el('span', 'notice-icon', '!');
    icon.setAttribute('aria-hidden', 'true');
    row.append(icon, el('p', 'answer', a.answer));
    bubble.append(row);
  } else {
    const answer = el('div', 'answer');
    for (const part of lib.splitCitations(a.answer, a.sources.map((s) => s.n))) {
      answer.append(part.type === 'text' ? document.createTextNode(part.value) : citeLink(id, part.n));
    }
    bubble.append(answer);
  }

  const meta = metaLine(a);
  if (meta) bubble.append(meta);

  if (!a.noAnswer && a.sources.length) {
    const section = el('section', 'sources');
    const headingId = `sources-${id}`;
    section.setAttribute('aria-labelledby', headingId);
    const h = el('h2', 'sources-title', `출처 ${a.sources.length}개`);
    h.id = headingId;
    const list = el('ol', 'source-list');
    for (const s of a.sources) list.append(sourceCard(id, s));
    section.append(h, list);
    bubble.append(section);
  }

  art.append(bubble);
  announce(a.noAnswer
    ? `답변: ${a.answer}`
    : `답변이 도착했습니다. ${a.answer} 출처 ${a.sources.length}개.`);
  return art;
}

function citeLink(msgId, n) {
  const a = el('a', 'cite', `[${n}]`);
  a.href = `#src-${msgId}-${n}`;
  a.setAttribute('aria-label', `출처 ${n}번 보기`);
  a.addEventListener('click', (e) => {
    e.preventDefault();
    focusSource(msgId, n);
  });
  return a;
}

function focusSource(msgId, n) {
  const card = document.getElementById(`src-${msgId}-${n}`);
  if (!card) return;
  card.scrollIntoView({ block: 'nearest', behavior: REDUCE_MOTION ? 'auto' : 'smooth' });
  card.focus({ preventScroll: true });
  card.classList.remove('flash');
  void card.offsetWidth; // 애니메이션을 처음부터 다시
  card.classList.add('flash');
}

function sourceCard(msgId, s) {
  const kindClass = s.kind === 'school_rule' ? ' is-school' : s.kind === 'club_manual' ? ' is-club' : '';
  const li = el('li', `source${kindClass}`);
  li.id = `src-${msgId}-${s.n}`;
  li.tabIndex = -1;

  const head = el('div', 'source-head');
  const num = el('span', 'source-n', String(s.n));
  num.setAttribute('aria-label', `출처 ${s.n}번`);
  head.append(num, el('span', 'badge', lib.kindLabel(s.kind)));
  if (s.url) { // normalizeAnswer에서 http, https만 남김
    const link = el('a', 'source-link', s.kind === 'club_manual' ? '노션에서 보기' : '원문 보기');
    link.href = s.url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    const arrow = el('span', null, ' ↗');
    arrow.setAttribute('aria-hidden', 'true');
    link.append(arrow, srOnly(' (새 창)'));
    head.append(link);
  }
  li.append(head, el('p', 'source-title', s.title || '(제목 없음)'));
  if (s.detail) li.append(el('p', 'source-detail', s.detail));

  if (s.text) {
    const details = el('details', 'source-body');
    details.append(el('summary', null, '근거 원문'), el('blockquote', 'source-text', s.text));
    li.append(details);
  }
  return li;
}

function renderError(message, status, req) {
  const art = el('article', 'msg bot');
  art.setAttribute('aria-label', '오류');
  const bubble = el('div', 'bubble error');
  const row = el('div', 'notice-row');
  const icon = el('span', 'notice-icon', '!');
  icon.setAttribute('aria-hidden', 'true');
  row.append(icon, el('p', 'answer', message));
  bubble.append(row);

  const actions = el('div', 'actions');
  if (status === 401) {
    if (!MOCK) clearTokens();
    actions.append(button('다시 로그인', 'btn primary small', startLogin));
  } else if (status !== 400 && status !== 403) {
    actions.append(button('다시 보내기', 'btn ghost small', () => {
      if (!busy) ask(req);
    }));
  }
  if (actions.childElementCount) bubble.append(actions);
  art.append(bubble);
  announce(message);
  return art;
}

function appendLoginNotice(message) {
  const art = el('article', 'msg bot');
  art.setAttribute('aria-label', '안내');
  const bubble = el('div', 'bubble error');
  const row = el('div', 'notice-row');
  const icon = el('span', 'notice-icon', '!');
  icon.setAttribute('aria-hidden', 'true');
  row.append(icon, el('p', 'answer', message));
  const actions = el('div', 'actions');
  actions.append(button('로그인', 'btn primary small', startLogin));
  bubble.append(row, actions);
  art.append(bubble);
  els.emptyState.hidden = true;
  els.messages.append(art);
  scrollToBottom();
  announce(message);
}

function button(text, className, onClick) {
  const b = el('button', className, text);
  b.type = 'button';
  b.addEventListener('click', onClick);
  return b;
}

// ---------------------------------------------------------------- 시연 (?mock=1&demo=1)

// 가짜 대화를 자동으로 채움. 화면 캡처와 발표용
async function runDemo() {
  const event = lib.buildEventQuestion({
    type: 'MT',
    place: '교외 펜션 (충남 보령)',
    date: '2026-12-05 ~ 12-06',
    details: '30명 참석, 1박 2일',
  });
  const steps = [
    ['qa', '동아리방에서 전기난로를 써도 되나요?'],
    ['event', event.value, event.summary],
    ['qa', '내일 대전 날씨 알려 줘'],
    ['qa', '오류가 나면 어떻게 보이나요?'],
  ];
  for (const [m, q, shown] of steps) await ask({ question: q, mode: m, shown: shown || q }, { delay: 0 });
  const first = els.messages.querySelector('details');
  if (first) first.open = true;
  els.messages.scrollTop = params.get('scroll') === 'bottom' ? els.messages.scrollHeight : 0;
}

// ---------------------------------------------------------------- 시작

function bindEvents() {
  els.loginButton.addEventListener('click', startLogin);
  els.logoutButton.addEventListener('click', logout);
  for (const b of els.modeButtons) b.addEventListener('click', () => setMode(b.dataset.mode));
  els.composer.addEventListener('submit', (e) => {
    e.preventDefault();
    submitQuestion();
  });
  for (const f of [els.question, els.evType, els.evPlace, els.evDate, els.evDetails]) {
    f.addEventListener('keydown', onFieldKeydown);
    f.addEventListener('input', () => {
      if (f.tagName === 'TEXTAREA') autoGrow(f);
      hideComposerError();
      updateCount();
    });
  }
  for (const chip of document.querySelectorAll('[data-example]')) {
    chip.addEventListener('click', () => {
      if (busy) return;
      if (mode !== 'qa') setMode('qa', { focus: false });
      els.question.value = chip.dataset.example;
      submitQuestion();
    });
  }
  if (!ENTER_SENDS) els.hint.hidden = true;
}

async function init() {
  bindEvents();
  setMode('qa', { focus: false });

  if (MOCK) {
    showChat(currentSession());
    if (DEMO) await runDemo();
    return;
  }
  if (!lib.isConfigured(cfg)) {
    showSetupNeeded();
    return;
  }
  const callbackError = await handleCallback();
  if (callbackError) {
    clearTokens();
    showLogin(callbackError);
    return;
  }
  const session = currentSession();
  if (!session) {
    showLogin();
  } else if (session.expired) {
    clearTokens();
    showLogin('로그인 시간이 끝났습니다. 다시 로그인해 주세요.');
  } else {
    showChat(session);
  }
}

init();
