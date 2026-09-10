/**
 * docs/api-specification.md 의 응답 계약을 그대로 옮긴 타입.
 * 필드명은 camelCase, 식별자는 UUID 문자열, 시각은 ISO 8601 UTC.
 */
export type UUID = string;
export type ISODateTime = string;
/** 사용자 식별자만 64비트 정수(users.user_id BIGSERIAL). 그 밖의 식별자는 UUID 문자열이다. */
export type UserId = number;

export type Market = "KOSPI" | "KOSDAQ" | "NASDAQ" | (string & {});
export type MetricWindow = "7D" | "30D" | "90D";
export type PricePeriod = "1M" | "3M" | "6M" | "1Y";
export type Sentiment = "POSITIVE" | "NEGATIVE" | "NEUTRAL";
export type ImpactDirection = "POSITIVE" | "NEGATIVE" | "NEUTRAL";
export type Directionality = "DIRECTED" | "UNDIRECTED";
/** 관계 유형 코드. DB 설계의 공급·투자·협력·경쟁을 기준으로 하되 미정 코드는 문자열로 수용한다. */
export type RelationshipType = "SUPPLY" | "INVEST" | "PARTNER" | "COMPETE" | (string & {});

export interface Page<T> {
  items: T[];
  nextCursor: string | null;
  hasNext: boolean;
}

export interface FieldError {
  field: string;
  message: string;
}
export interface ApiErrorBody {
  code: string;
  message: string;
  fieldErrors: FieldError[];
}

/* ------------------------------ auth ------------------------------ */
export interface LoginRequest {
  email: string;
  password: string;
}
export interface TokenResponse {
  accessToken: string;
}
export interface SignupRequest {
  email: string;
  password: string;
  nickname: string;
}
export interface SignupResponse {
  userId: UserId;
  email: string;
  nickname: string;
}
export interface Availability {
  available: boolean;
}

/* ------------------------------ users ------------------------------ */
export interface Me {
  userId: UserId;
  email: string;
  nickname: string;
}
export interface ScrapItem {
  newsId: UUID;
  title: string;
  summary: string;
  publisher: string;
  originalUrl: string;
  publishedAt: ISODateTime;
  scrappedAt: ISODateTime;
}
export interface IndustryRef {
  industryId: UUID;
  name: string;
}
export interface WatchCompanyItem {
  companyId: UUID;
  name: string;
  stockCode: string | null;
  market: Market;
  primaryIndustry: IndustryRef;
  watchedAt: ISODateTime;
}
export interface WeightSetting {
  newsWeight: number;
  disclosureWeight: number;
  updatedAt: ISODateTime | null;
}

/* ---------------------------- companies ---------------------------- */
export interface CompanySummary {
  companyId: UUID;
  name: string;
  nameEn: string | null;
  stockCode: string | null;
  market: Market;
  primaryIndustry: IndustryRef;
  watched: boolean;
}
export interface CompanyIndustry extends IndustryRef {
  primary: boolean;
}
export interface CompanyDetail {
  companyId: UUID;
  name: string;
  nameEn: string | null;
  stockCode: string | null;
  market: Market;
  description: string | null;
  industries: CompanyIndustry[];
  watched: boolean;
  /**
   * 시가총액·상장주식수는 V1 DDL 의 company 테이블에도 명세 응답에도 없다.
   * 백엔드가 열어 줄 때 화면이 그대로 받을 수 있도록 선택 필드로만 자리를 둔다(목업은 채운다).
   */
  marketCap?: number | null;
  listedShares?: number | null;
}
export interface CompanyMetrics {
  companyId: UUID;
  window: MetricWindow;
  newsMentionCount: number | null;
  positiveCount: number | null;
  negativeCount: number | null;
  sentimentScore: number | null;
  relationshipCount: number | null;
  measuredAt: ISODateTime | null;
}
export interface MetricPoint {
  measuredAt: ISODateTime;
  newsMentionCount: number;
  positiveCount: number;
  negativeCount: number;
  sentimentScore: number | null;
  relationshipCount: number;
}
export interface MetricsHistoryParams {
  window?: MetricWindow;
  /** YYYY-MM-DD */
  from?: string;
  to?: string;
}
export interface MetricsHistory {
  companyId: UUID;
  window: MetricWindow;
  items: MetricPoint[];
}
export interface Candle {
  tradingAt: ISODateTime;
  openPrice: number;
  highPrice: number;
  lowPrice: number;
  closePrice: number;
  tradingVolume: number;
}
export interface StockPrices {
  companyId: UUID;
  period: PricePeriod;
  /** MVP 는 1D 만 지원하지만 stock_price_history.interval_type 은 자유값이라 확장을 열어 둔다. */
  interval: "1D" | (string & {});
  asOfAt: ISODateTime;
  items: Candle[];
}
export interface Industry {
  industryId: UUID;
  parentIndustryId: UUID | null;
  name: string;
  description: string | null;
  companyCount: number;
}
export interface CompanyListParams {
  keyword?: string;
  market?: Market;
  industryId?: UUID;
  cursor?: string;
  size?: number;
}

/* ------------------------------ news ------------------------------ */
export interface CompanyRef {
  companyId: UUID;
  name: string;
}
export interface NewsItem {
  newsId: UUID;
  title: string;
  summary: string | null;
  publisher: string | null;
  originalUrl: string;
  publishedAt: ISODateTime | null;
  sentiment: Sentiment;
  relatedCompanies: CompanyRef[];
  scrapped: boolean;
}
export interface NewsRelatedCompany extends CompanyRef {
  sentiment: Sentiment;
  relevanceScore: number | null;
  impactScore: number | null;
}
export interface NewsEvidence {
  sentence: string;
  confidence: number | null;
}
export interface NewsDetail {
  newsId: UUID;
  title: string;
  summary: string | null;
  publisher: string | null;
  author: string | null;
  originalUrl: string;
  publishedAt: ISODateTime | null;
  relatedCompanies: NewsRelatedCompany[];
  evidence: NewsEvidence[];
  scrapped: boolean;
}
export interface NewsListParams {
  keyword?: string;
  companyId?: UUID;
  industryId?: UUID;
  sentiment?: Sentiment;
  from?: string;
  to?: string;
  cursor?: string;
  size?: number;
}

/* ------------------------------ graph ------------------------------ */
export interface GraphEdge {
  relationshipId: UUID;
  sourceCompanyId: UUID;
  targetCompanyId: UUID;
  relationshipType: RelationshipType;
  /** 0 ~ 100 */
  score: number;
  impactDirection: ImpactDirection | null;
}
/** 은하 뷰가 행성 높이로 쓰는 등락률 기간 */
export type AltitudeWindow = "1D" | "1M" | "3M";
export interface LatestGraphNode {
  companyId: UUID;
  name: string;
  stockCode: string | null;
  market: Market;
  industryName: string;
  /**
   * 기간별 주가 등락률(퍼센트, 예 -3.2). 은하 뷰의 행성 높이가 이 값을 쓴다.
   * 서버가 아직 내려주지 않을 수 있어 선택 필드이고, 시계열이 없는 기업은 null 이다 (README '백엔드에 확인이 필요한 항목').
   */
  priceChange?: { "1D"?: number | null; "1M"?: number | null; "3M"?: number | null };
}
export interface LatestGraph {
  snapshotId: UUID;
  asOfAt: ISODateTime;
  nextRefreshAt: ISODateTime;
  personalized: boolean;
  nodes: LatestGraphNode[];
  edges: GraphEdge[];
}
export interface LatestGraphParams {
  universe?: string;
  industryId?: UUID;
  edgesPerCompany?: number;
}
export interface CompanyGraphNode {
  companyId: UUID;
  name: string;
  /** 0 = 중심 기업, 1 = 직접 관계, 2·3 = 간접 관계 */
  depth: number;
}
export interface CompanyGraph {
  snapshotId: UUID;
  asOfAt: ISODateTime;
  centerCompanyId: UUID;
  personalized: boolean;
  nodes: CompanyGraphNode[];
  edges: GraphEdge[];
}
export interface CompanyGraphParams {
  maxDepth?: number;
  neighborsPerNode?: number;
  maxNodes?: number;
}
export interface RelationshipDetail {
  relationshipId: UUID;
  sourceCompany: CompanyRef;
  targetCompany: CompanyRef;
  relationshipType: RelationshipType;
  directionality: Directionality;
  window: MetricWindow;
  score: number;
  impactDirection: ImpactDirection | null;
  confidence: number | null;
  evidenceCount: number;
  asOfAt: ISODateTime;
  personalized: boolean;
}
export interface RelationshipEvidence {
  newsId: UUID;
  title: string;
  summary: string | null;
  publisher: string | null;
  originalUrl: string;
  publishedAt: ISODateTime | null;
  evidenceSentence: string;
  contributionScore: number | null;
  confidence: number | null;
}

/* ---------------------------- community ---------------------------- */
export interface CommentAuthor {
  userId: UserId;
  nickname: string;
}
export interface CompanyComment {
  commentId: UUID;
  content: string;
  author: CommentAuthor;
  companyId: UUID;
  createdAt: ISODateTime;
  updatedAt: ISODateTime;
  edited: boolean;
}
export interface GlobalComment extends Omit<CompanyComment, "companyId"> {
  company: CompanyRef;
}
export interface CommentWriteResponse {
  commentId: UUID;
  companyId: UUID;
  content: string;
  createdAt: ISODateTime;
  updatedAt: ISODateTime;
}
export interface CommentEditResponse {
  commentId: UUID;
  content: string;
  createdAt: ISODateTime;
  updatedAt: ISODateTime;
  edited: boolean;
}
