/**
 * docs/api-specification.md 의 응답 계약을 그대로 옮긴 타입.
 * 필드명은 camelCase, 식별자는 UUID 문자열, 시각은 ISO 8601 UTC.
 */
export type UUID = string;
export type ISODateTime = string;

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
  userId: UUID;
  email: string;
  nickname: string;
}
export interface Availability {
  available: boolean;
}

/* ------------------------------ users ------------------------------ */
export interface Me {
  userId: UUID;
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
  stockCode: string;
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
  nameEn: string;
  stockCode: string;
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
  nameEn: string;
  stockCode: string;
  market: Market;
  description: string;
  industries: CompanyIndustry[];
  watched: boolean;
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
  sentimentScore: number;
  relationshipCount: number;
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
  interval: "1D";
  asOfAt: ISODateTime;
  items: Candle[];
}
export interface Industry {
  industryId: UUID;
  parentIndustryId: UUID | null;
  name: string;
  description: string;
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
  summary: string;
  publisher: string;
  originalUrl: string;
  publishedAt: ISODateTime;
  sentiment: Sentiment;
  relatedCompanies: CompanyRef[];
  scrapped: boolean;
}
export interface NewsRelatedCompany extends CompanyRef {
  sentiment: Sentiment;
  relevanceScore: number;
  impactScore: number;
}
export interface NewsEvidence {
  sentence: string;
  confidence: number;
}
export interface NewsDetail {
  newsId: UUID;
  title: string;
  summary: string;
  publisher: string;
  author: string | null;
  originalUrl: string;
  publishedAt: ISODateTime;
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
  impactDirection: ImpactDirection;
}
export interface LatestGraphNode {
  companyId: UUID;
  name: string;
  stockCode: string;
  market: Market;
  industryName: string;
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
  impactDirection: ImpactDirection;
  confidence: number;
  evidenceCount: number;
  asOfAt: ISODateTime;
  personalized: boolean;
}
export interface RelationshipEvidence {
  newsId: UUID;
  title: string;
  summary: string;
  publisher: string;
  originalUrl: string;
  publishedAt: ISODateTime;
  evidenceSentence: string;
  contributionScore: number;
  confidence: number;
}

/* ---------------------------- community ---------------------------- */
export interface CommentAuthor {
  userId: UUID;
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
