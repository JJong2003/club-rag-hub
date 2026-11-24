// 가짜 응답. POST /ask가 생기기 전에 화면을 만들고 시험하려고 씁니다.
// 질문에 들어간 말로 시나리오를 고릅니다.
//   '오류'        -> 500
//   '로그인 만료' -> 401
//   '요청 제한'   -> 429
//   '잘못된 요청' -> 400 (서버 메시지를 그대로 보여 줌)
//   '날씨', '모르는' -> 근거 없음(no_answer)
//   '보안 시험'   -> HTML 글자와 javascript: 링크가 섞인 응답 (글자로만 보이고 링크가 안 생겨야 함)
//   mode가 event, 'MT', '행사' -> 행사 기획 답변
//   '회의'        -> 동아리 자료만 쓴 답변
//   그 밖        -> 학교 규정 + 동아리 자료가 섞인 답변

import { NO_ANSWER_TEXT, GENERIC_ERROR } from './lib.js';

const LAW_URL = 'https://www.law.go.kr/';
const NOTION_URL = 'https://www.notion.so/';

const HEATER = {
  answer:
    '동아리방에서는 전기난로 같은 전열기구를 개인적으로 쓸 수 없습니다 [1]. '
    + '꼭 필요하면 건물 관리 부서의 허가를 받은 기구만 정해진 곳에서 쓸 수 있습니다 [2].\n'
    + '동아리에서는 겨울에 전기난로 대신 담요를 두고, 마지막에 나가는 사람이 멀티탭 전원을 끄기로 했습니다 [3].',
  sources: [
    {
      n: 1,
      kind: 'school_rule',
      title: '「충남대학교 전열기구 사용관리 규정」 제4조(사용 금지)',
      detail: '제2111호, 2023-09-12 개정',
      url: LAW_URL,
      text: '제4조(사용 금지) 교내 건물에서는 허가받지 않은 전열기구를 사용할 수 없다. 다만, 업무상 꼭 필요한 경우에는 제5조에 따라 허가를 받아 사용할 수 있다.',
    },
    {
      n: 2,
      kind: 'school_rule',
      title: '「충남대학교 전열기구 사용관리 규정」 제5조(사용 허가)',
      detail: '제2111호, 2023-09-12 개정',
      url: LAW_URL,
      text: '제5조(사용 허가) ① 전열기구를 사용하려는 사람은 건물 관리 부서의 장에게 신청하여 허가를 받아야 한다.\n② 허가받은 기구는 정해진 장소에서만 사용한다.',
    },
    {
      n: 3,
      kind: 'club_manual',
      title: '동아리방 관리 > 동아리방 사용 수칙',
      detail: '2025',
      url: NOTION_URL,
      text: '겨울철 난방: 전기난로는 쓰지 않는다. 담요 3장을 사물함에 둔다. 마지막으로 나가는 사람이 멀티탭 전원을 끄고 단톡방에 알린다.',
    },
  ],
  searched: ['school', 'club-ana'],
  no_answer: false,
  ms: 8421,
};

const EVENT = {
  answer:
    '교외에서 MT를 하려면 출발 1주 전까지 학생과에 교외활동 계획서와 참가자 명단을 내야 합니다 [1]. '
    + '참가자 모두 여행자 보험에 들어야 하고, 행사 중 음주를 강요하면 안 됩니다 [2].\n'
    + '동아리에서는 여름 2차 회의에서 회비 정산 담당과 차량 담당을 미리 정하기로 했습니다 [3]. '
    + '지난 MT 때는 숙소 계약금을 2주 전에 보냈습니다 [4].',
  sources: [
    {
      n: 1,
      kind: 'school_rule',
      title: '「충남대학교 학생활동 지원 규정」 제8조(교외활동 신고)',
      detail: '제1950호, 2022-03-01 개정',
      url: LAW_URL,
      text: '제8조(교외활동 신고) 학생 단체가 교외에서 활동하려면 활동 7일 전까지 계획서와 참가자 명단을 학생과에 제출하여야 한다.',
    },
    {
      n: 2,
      kind: 'school_rule',
      title: '「충남대학교 학생활동 지원 규정」 제9조(안전 관리)',
      detail: '제1950호, 2022-03-01 개정',
      url: LAW_URL,
      text: '제9조(안전 관리) ① 교외활동 참가자는 보험에 가입하여야 한다.\n② 활동 중 음주를 강요하거나 위험한 행동을 하게 하여서는 아니 된다.',
    },
    {
      n: 3,
      kind: 'club_manual',
      title: '전체 일정 > 일정 및 작업들 > 여름 2차 회의',
      detail: '2026',
      url: NOTION_URL,
      text: '안건 2. 가을 MT\n- 회비 정산 담당 1명, 차량 담당 1명을 MT 3주 전에 정한다.\n- 장소 후보는 다음 회의까지 각자 1곳씩 조사한다.',
    },
    {
      n: 4,
      kind: 'club_manual',
      title: '행사 > MT > 2025 가을 MT 준비',
      detail: '2025',
      url: NOTION_URL,
      text: '숙소: 계약금 30%를 2주 전에 송금. 잔금은 당일 현장 결제. 취소는 1주 전까지 전액 환불.',
    },
  ],
  searched: ['school', 'club-ana'],
  no_answer: false,
  ms: 12840,
};

const CLUB = {
  answer:
    '회의록은 노션의 \'전체 일정 > 일정 및 작업들\' 아래에 회의마다 한 페이지씩 만듭니다 [1]. '
    + '페이지 첫 줄에 참석자와 안건을 적고, 정한 일은 담당자와 마감일을 함께 적습니다 [1].',
  sources: [
    {
      n: 1,
      kind: 'club_manual',
      title: '전체 일정 > 일정 및 작업들 > 여름 2차 회의',
      detail: '2026',
      url: NOTION_URL,
      text: '참석: 임원진 5명\n안건 1. 회의록 정리 방식\n- 회의마다 이 데이터베이스에 페이지를 하나 만든다.\n- 정한 일은 담당자와 마감일을 같이 적는다.',
    },
  ],
  searched: ['school', 'club-ana'],
  no_answer: false,
  ms: 6120,
};

const NO_ANSWER = {
  answer: NO_ANSWER_TEXT,
  sources: [],
  searched: ['school', 'club-ana'],
  no_answer: true,
  ms: 2310,
};

const SECURITY = {
  answer:
    '<b>굵게 보이면 안 됩니다</b> <img src=x onerror=alert(1)> [1] '
    + '없는 번호는 글자로 남아야 합니다 [9]. 링크가 막힌 출처도 있습니다 [2].',
  sources: [
    {
      n: 1,
      kind: 'school_rule',
      title: '<script>alert("title")</script>',
      detail: '<i>기울이면 안 됨</i>',
      url: 'javascript:alert(1)',
      text: '<img src=x onerror=alert("text")> 근거 원문도 글자로만 보여야 합니다.',
    },
    {
      n: 2,
      kind: 'club_manual',
      title: '링크 검사 > data 주소',
      detail: '2026',
      url: 'data:text/html,<script>alert(1)</script>',
      text: '이 출처에는 원문 링크가 보이면 안 됩니다.',
    },
  ],
  searched: ['school', 'club-ana'],
  no_answer: false,
  ms: 1200,
};

const copy = (o) => JSON.parse(JSON.stringify(o));

// 실제 /ask와 같은 { status, body }를 돌려줌
export function mockAsk(question, mode = 'qa') {
  const q = String(question ?? '');
  if (q.includes('오류')) return { status: 500, body: { error: GENERIC_ERROR } };
  if (q.includes('로그인 만료')) return { status: 401, body: { message: 'Unauthorized' } };
  if (q.includes('요청 제한')) return { status: 429, body: { message: 'Too Many Requests' } };
  if (q.includes('잘못된 요청')) return { status: 400, body: { error: '질문을 1,000자 안으로 적어 주세요.' } };
  if (q.includes('보안 시험')) return { status: 200, body: copy(SECURITY) };
  if (q.includes('날씨') || q.includes('모르는')) return { status: 200, body: copy(NO_ANSWER) };
  if (mode === 'event' || q.includes('MT') || q.includes('행사')) return { status: 200, body: copy(EVENT) };
  if (q.includes('회의')) return { status: 200, body: copy(CLUB) };
  return { status: 200, body: copy(HEATER) };
}
