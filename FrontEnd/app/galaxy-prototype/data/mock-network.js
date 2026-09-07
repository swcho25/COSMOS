import { COMPANIES, RELATION_META } from './universe';

const SECTOR_INDUSTRY = {
  semi: '반도체·AI 인프라',
  battery: '2차전지·소재',
  auto: '완성차·자동차부품',
  platform: '클라우드·인터넷 플랫폼',
  bio: '바이오·헬스케어',
  energy: '전력·중공업',
};

const BASE_ALIASES = {
  '005380': ['현대자동차', 'Hyundai Motor'],
  '000270': ['기아자동차', 'Kia'],
  '000660': ['SK hynix', '에스케이하이닉스'],
  '005930': ['Samsung Electronics'],
  '373220': ['LG Energy Solution', 'LGES'],
  '006400': ['Samsung SDI'],
  '035420': ['네이버', 'Naver'],
};

const FOREIGN_COMPANIES = [
  ['NVDA', 'NVIDIA', 'semi', 72, ['엔비디아'], 'AI 가속기·GPU'],
  ['TSM', 'TSMC', 'semi', 68, ['대만 TSMC'], '첨단 반도체 파운드리'],
  ['ASML', 'ASML', 'semi', 58, ['에이에스엠엘'], 'EUV 노광장비'],
  ['MU', 'Micron', 'semi', 34, ['마이크론', 'Micron Technology'], '메모리 반도체'],
  ['MSFT', 'Microsoft', 'platform', 66, ['마이크로소프트'], '클라우드·AI 플랫폼'],
  ['GOOGL', 'Alphabet', 'platform', 62, ['Google', '구글', '알파벳'], '검색·클라우드·AI'],
  ['TSLA', 'Tesla', 'auto', 54, ['테슬라'], '전기차·에너지'],
  ['F', 'Ford', 'auto', 30, ['포드', 'Ford Motor'], '완성차'],
  ['GM', 'General Motors', 'auto', 33, ['GM', '제너럴모터스'], '완성차'],
  ['300750.SZ', 'CATL', 'battery', 51, ['닝더스다이', 'Contemporary Amperex'], 'EV 배터리 셀'],
  ['6752.T', 'Panasonic', 'battery', 28, ['파나소닉', 'Panasonic Energy'], 'EV 배터리 셀'],
];

export const MOCK_COMPANIES = [
  ...COMPANIES.map((company) => ({
    ...company,
    aliases: BASE_ALIASES[company.id] ?? [],
    industry: SECTOR_INDUSTRY[company.sector],
  })),
  ...FOREIGN_COMPANIES.map(([id, name, sector, cap, aliases, industry]) => ({
    id,
    name,
    ticker: id,
    sector,
    cap,
    aliases,
    industry,
  })),
];

const EXTRA_RELATIONS = [
  ['000660', 'NVDA', 'customer', 'HBM 공급망 관점에서 AI 가속기 수요와 메모리 증설 사이의 전이 경로', 0.82],
  ['005930', 'NVDA', 'customer', 'HBM·AI 메모리 공급 후보와 GPU 플랫폼 수요를 연결한 시연 관계', 0.62],
  ['MU', 'NVDA', 'customer', 'HBM 공급 경쟁과 AI 가속기 수요를 연결한 시연 관계', 0.72],
  ['TSM', 'NVDA', 'customer', 'AI GPU 설계사와 첨단 공정 파운드리 생산 관계', 0.88],
  ['ASML', 'TSM', 'supply', '첨단 공정 생산에 필요한 EUV 노광장비 공급망', 0.9],
  ['ASML', '005930', 'supply', '반도체 미세공정 투자와 EUV 장비 수요의 연결', 0.78],
  ['ASML', '000660', 'supply', 'HBM·DRAM 미세공정 투자와 EUV 장비 수요의 연결', 0.8],
  ['005930', 'TSM', 'rival', '첨단 파운드리 수주 및 공정 경쟁', 0.7],
  ['000660', 'MU', 'rival', 'HBM·DRAM 시장 점유율 경쟁과 공통 업황 노출', 0.74],
  ['005930', 'MU', 'rival', '메모리 반도체 업황과 가격 사이클의 공동 노출', 0.68],
  ['NVDA', 'MSFT', 'customer', '클라우드 AI 인프라의 GPU 조달 관계', 0.8],
  ['NVDA', 'GOOGL', 'customer', '클라우드·생성형 AI 서비스의 가속기 수요 관계', 0.66],
  ['NVDA', 'TSLA', 'customer', '자율주행·AI 연산 생태계의 기술 수요 연결', 0.5],
  ['005930', 'GOOGL', 'customer', '모바일 기기와 AI 소프트웨어 생태계 협력 관계', 0.58],
  ['005930', 'MSFT', 'customer', '디바이스·클라우드 AI 생태계 협력 관계', 0.48],
  ['035420', 'GOOGL', 'rival', '검색·광고·생성형 AI 플랫폼 경쟁', 0.62],
  ['035720', 'GOOGL', 'macro', '플랫폼 광고 경기와 AI 서비스 투자 사이클 공동 노출', 0.45],
  ['MSFT', 'GOOGL', 'rival', '글로벌 클라우드·생성형 AI 플랫폼 경쟁', 0.76],
  ['373220', 'F', 'customer', 'EV 배터리 수요와 완성차 생산 계획 사이의 계약 전이 경로', 0.7],
  ['373220', 'GM', 'customer', '북미 EV 배터리 합작·조달 생태계의 수요 전이 경로', 0.78],
  ['373220', 'TSLA', 'customer', '전기차 판매량과 배터리 셀 수요를 연결한 시연 관계', 0.56],
  ['300750.SZ', 'TSLA', 'customer', '전기차 생산량과 배터리 셀 조달의 공급망 연결', 0.82],
  ['6752.T', 'TSLA', 'customer', '전기차용 원통형 배터리 공급망 연결', 0.76],
  ['373220', '300750.SZ', 'rival', '글로벌 EV 배터리 점유율·가격 경쟁', 0.72],
  ['006400', '300750.SZ', 'rival', 'EV 배터리 기술·수주 경쟁과 원재료 가격 공동 노출', 0.66],
  ['373220', '6752.T', 'rival', '글로벌 배터리 고객사 수주 경쟁', 0.54],
  ['005380', '373220', 'customer', '완성차 전동화 판매와 배터리 셀 수요 전이', 0.66],
  ['000270', '373220', 'customer', '전기차 생산 계획과 배터리 셀 조달 관계', 0.58],
  ['TSLA', 'F', 'rival', '북미 전기차 가격·판매 경쟁', 0.58],
  ['TSLA', 'GM', 'rival', '북미 전기차 플랫폼·판매 경쟁', 0.6],
  ['F', 'GM', 'rival', '북미 완성차 수요와 비용 구조 공동 노출', 0.65],
  ['NVDA', '267260', 'macro', 'AI 데이터센터 증설과 전력기기 수요의 간접 전이', 0.58],
  ['NVDA', '034020', 'macro', 'AI 데이터센터 전력 수요와 발전 인프라 투자의 간접 연결', 0.52],
  ['MSFT', '267260', 'macro', '클라우드 데이터센터 CAPEX와 전력기기 수요의 간접 연결', 0.5],
  ['GOOGL', '034020', 'macro', '데이터센터 전력 소비와 발전 설비 투자 테마의 간접 연결', 0.44],
];

const CURATED_RELATIONS = Object.values(RELATION_META).map(({ a, b, type, reason, strength }) => ({
  a,
  b,
  type,
  reason,
  strength,
}));

export const MOCK_RELATIONS = [
  ...CURATED_RELATIONS,
  ...EXTRA_RELATIONS.map(([a, b, type, reason, strength]) => ({ a, b, type, reason, strength })),
];
