import { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import type { GraphEdge, LatestGraph, NewsDetail, Sentiment } from "@/api/types";
import MiniScene from "@/three/MiniScene";
import { layoutNewsSubgraph } from "@/lib/graph";
import { fmtDateTime, fmtRelative } from "@/lib/format";
import { SENTIMENTS, SENTIMENT_META, industryColor, marketLabel, relationshipMeta } from "@/lib/meta";
import { useCompany, useIndustries, useLatestGraph, useNewsDetail, useNewsFeed, useToggleScrap, useToggleWatch } from "@/lib/queries";
import { useSession } from "@/store/session";
import { useUi } from "@/store/ui";
import { CompanyAvatar, Empty, Icon, Kpi, ScoreBar, SentimentBadge, Skeleton, TypeBadge, TypeLegend } from "@/components/ui";
import "./news.css";

/**
 * 뉴스 (화면구성도 ⑦~⑨, 시안 A1). 좌측 컨텍스트 레일 + 우측 관계도.
 * 관계도의 기업 노드를 누르면 좌측 목록이 그 기업 뉴스로 전환되고 브레드크럼이 복귀 경로를 보여준다.
 */
export default function NewsPage() {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const toast = useUi((s) => s.toast);
  const authed = useSession((s) => s.status === "authed");
  const openAuth = useUi((s) => s.openAuth);

  const companyId = params.get("company");
  const [industryId, setIndustryId] = useState<string | null>(null);
  const [sentiment, setSentiment] = useState<Sentiment | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [expand, setExpand] = useState(true);

  const industries = useIndustries();
  const universe = useLatestGraph(null);
  const company = useCompany(companyId);
  const feed = useNewsFeed({ companyId: companyId ?? undefined, industryId: companyId ? undefined : industryId ?? undefined, sentiment: sentiment ?? undefined, size: 20 });
  const items = useMemo(() => feed.data?.pages.flatMap((p) => p.items) ?? [], [feed.data]);

  // 목록이 바뀌면 첫 기사를 선택한다
  useEffect(() => {
    if (!items.length) return;
    if (!selectedId || !items.some((n) => n.newsId === selectedId)) setSelectedId(items[0].newsId);
  }, [items, selectedId]);

  const detail = useNewsDetail(selectedId);
  const toggleScrap = useToggleScrap();
  const toggleWatch = useToggleWatch();

  const setCompany = (id: string | null) => {
    const next = new URLSearchParams(params);
    if (id) next.set("company", id);
    else next.delete("company");
    setParams(next);
    setSentiment(null);
  };

  /* 관계 지도 모델: 선택 뉴스의 관련 기업 (+1홉) */
  const seedIds = useMemo(() => {
    const d = detail.data;
    if (!d) return [] as string[];
    const sorted = d.relatedCompanies.slice().sort((a, b) => b.relevanceScore * b.impactScore - a.relevanceScore * a.impactScore);
    const ids = sorted.map((r) => r.companyId);
    if (companyId && ids.includes(companyId)) return [companyId, ...ids.filter((x) => x !== companyId)];
    return ids;
  }, [detail.data, companyId]);
  const model = useMemo(() => (universe.data && seedIds.length ? layoutNewsSubgraph(universe.data, seedIds, expand) : null), [universe.data, seedIds, expand]);

  const analysis = useMemo(() => (detail.data && universe.data ? analyze(detail.data, universe.data) : null), [detail.data, universe.data]);

  const kpi = detail.data
    ? {
        companies: detail.data.relatedCompanies.length,
        pos: detail.data.relatedCompanies.filter((r) => r.sentiment === "POSITIVE").length,
        neg: detail.data.relatedCompanies.filter((r) => r.sentiment === "NEGATIVE").length,
        evidence: detail.data.evidence.length,
        confidence: detail.data.evidence.length ? Math.round((detail.data.evidence.reduce((s, e) => s + e.confidence, 0) / detail.data.evidence.length) * 100) : null,
      }
    : null;

  return (
    <div className="page">
      <div className="row between wrap" style={{ marginBottom: 14 }}>
        <span className="status-line">
          <i className="dot" />
          {universe.data ? `스냅샷 ${fmtDateTime(universe.data.asOfAt)} 기준 · 1시간 단위 갱신` : "스냅샷 확인 중"}
        </span>
        <button type="button" className="btn btn-g btn-sm" onClick={() => qc.invalidateQueries({ queryKey: ["news"] })}>
          <Icon.Refresh /> 새로고침
        </button>
      </div>

      <div className="news-page">
        {/* ---------------- 좌측 레일 ---------------- */}
        <section className="card news-rail" aria-label="뉴스 목록">
          {companyId ? (
            <>
              <div className="crumbs">
                <button type="button" onClick={() => setCompany(null)}>
                  전체 뉴스
                </button>
                <span aria-hidden="true">›</span>
                <b>{company.data?.name ?? "…"}</b>
                <button type="button" className="reset" onClick={() => setCompany(null)}>
                  전체로 <Icon.Close />
                </button>
              </div>
              <div className="company-ctx">
                {company.data && <CompanyAvatar name={company.data.name} stockCode={company.data.stockCode} industry={company.data.industries[0]?.name} size={40} />}
                <div className="grow">
                  <div className="nm">{company.data?.name ?? <Skeleton h={18} w={120} />} 뉴스</div>
                  <div className="meta num">
                    {company.data?.stockCode} · {company.data?.industries.find((i) => i.primary)?.name} · {items.length}건{feed.hasNextPage ? "+" : ""}
                  </div>
                </div>
              </div>
              <div className="row wrap mt-12" style={{ gap: 7 }}>
                <button type="button" className={`chip chip-sm ${sentiment === null ? "on" : ""}`} onClick={() => setSentiment(null)}>
                  전체
                </button>
                {SENTIMENTS.map((s) => (
                  <button key={s} type="button" className={`chip chip-sm ${sentiment === s ? "on" : ""}`} onClick={() => setSentiment(s)}>
                    <i style={{ background: SENTIMENT_META[s].color }} />
                    {SENTIMENT_META[s].label}
                  </button>
                ))}
              </div>
            </>
          ) : (
            <>
              <div className="row between">
                <div>
                  <div className="kick">latest feed</div>
                  <h2 className="sect" style={{ marginTop: 6 }}>
                    최신 금융 뉴스
                  </h2>
                </div>
              </div>
              <div className="row wrap mt-12" style={{ gap: 7 }}>
                <button type="button" className={`chip chip-sm ${industryId === null ? "on" : ""}`} onClick={() => setIndustryId(null)}>
                  전체
                </button>
                {industries.data?.items
                  .filter((i) => i.companyCount > 0)
                  .map((i) => (
                    <button key={i.industryId} type="button" className={`chip chip-sm ${industryId === i.industryId ? "on" : ""}`} onClick={() => setIndustryId(industryId === i.industryId ? null : i.industryId)}>
                      <i style={{ background: industryColor(i.name) }} />
                      {i.name}
                    </button>
                  ))}
              </div>
            </>
          )}

          <div className="list scroll stack">
            {feed.isLoading && [0, 1, 2, 3].map((i) => <Skeleton key={i} h={110} style={{ borderRadius: 14 }} />)}
            {!feed.isLoading && !items.length && <Empty>조건에 맞는 뉴스가 없습니다.</Empty>}
            {items.map((n) => (
              <article key={n.newsId} className={`news-card ${n.newsId === selectedId ? "on" : ""}`} onClick={() => setSelectedId(n.newsId)} role="button" tabIndex={0} onKeyDown={(e) => e.key === "Enter" && setSelectedId(n.newsId)}>
                <div className="row between">
                  <div className="row" style={{ gap: 8 }}>
                    <SentimentBadge value={n.sentiment} />
                    <span className="lab">
                      연결 <b className="num">{n.relatedCompanies.length}</b>
                    </span>
                  </div>
                  <span className="meta num">
                    {n.publisher} · {fmtRelative(n.publishedAt)}
                  </span>
                </div>
                <div className="title clamp-2">{n.title}</div>
                <div className="summary clamp-2">{n.summary}</div>
              </article>
            ))}
            {feed.hasNextPage && (
              <button type="button" className="more-btn" onClick={() => feed.fetchNextPage()} disabled={feed.isFetchingNextPage}>
                {feed.isFetchingNextPage ? "불러오는 중…" : "더 보기"}
              </button>
            )}
          </div>
        </section>

        {/* ---------------- 우측 ---------------- */}
        <section aria-label="관계 분석">
          <div className="kpi-row">
            <Kpi label="선택 뉴스 연결 기업" value={kpi?.companies ?? "-"} unit="개사" />
            <Kpi label="긍정 / 부정 기업" value={kpi ? `${kpi.pos} / ${kpi.neg}` : "-"} tone={kpi && kpi.neg > kpi.pos ? "var(--neg-fg)" : undefined} />
            <Kpi label="대표 근거 문장" value={kpi?.evidence ?? "-"} unit="개" />
            <Kpi label="분석 신뢰도" value={kpi?.confidence ?? "-"} unit="/100" />
          </div>

          <div className="card map-card">
            <div className="map-head">
              {companyId && company.data ? (
                <div className="row" style={{ gap: 12, alignItems: "flex-start" }}>
                  <CompanyAvatar name={company.data.name} stockCode={company.data.stockCode} industry={company.data.industries[0]?.name} size={44} />
                  <div>
                    <div className="kick" style={{ color: "var(--primary-2)" }}>
                      focused company
                    </div>
                    <div className="ttl">{company.data.name}</div>
                    <div className="meta">선택 뉴스 「{detail.data?.title.slice(0, 26) ?? "…"}…」 기준 관계망</div>
                  </div>
                </div>
              ) : (
                <div className="grow">
                  <div className="kick" style={{ color: "var(--primary-2)" }}>
                    selected news
                  </div>
                  <div className="ttl">{detail.data?.title ?? <Skeleton h={22} w={360} />}</div>
                  <div className="meta num mt-8">{detail.data ? `${detail.data.publisher} · ${fmtDateTime(detail.data.publishedAt)}${detail.data.author ? ` · ${detail.data.author}` : ""}` : ""}</div>
                </div>
              )}
              <div className="row" style={{ gap: 8, flex: "none" }}>
                {companyId && company.data ? (
                  <>
                    <button
                      type="button"
                      className={`btn btn-g btn-sm ${company.data.watched ? "on" : ""}`}
                      onClick={() => (authed ? toggleWatch.mutate({ companyId: company.data!.companyId, watched: company.data!.watched }) : openAuth("login"))}
                      style={{ color: company.data.watched ? "var(--primary-2)" : undefined }}
                    >
                      <Icon.Star filled={company.data.watched} /> {company.data.watched ? "관심 기업" : "관심 기업 +"}
                    </button>
                    <button type="button" className="btn btn-p btn-sm" onClick={() => navigate(`/?company=${companyId}`)}>
                      기업 상세 ↗
                    </button>
                  </>
                ) : (
                  detail.data && (
                    <>
                      <button
                        type="button"
                        className="btn btn-g btn-sm"
                        onClick={() => (authed ? toggleScrap.mutate({ newsId: detail.data!.newsId, scrapped: detail.data!.scrapped }, { onSuccess: (now) => toast(now ? "스크랩했습니다." : "스크랩을 해제했습니다.", "success") }) : openAuth("login"))}
                        style={{ color: detail.data.scrapped ? "var(--primary-2)" : undefined }}
                      >
                        <Icon.Bookmark filled={detail.data.scrapped} /> {detail.data.scrapped ? "스크랩됨" : "스크랩"}
                      </button>
                      <a className="btn btn-g btn-sm" href={detail.data.originalUrl} target="_blank" rel="noreferrer">
                        원문 보기 <Icon.External />
                      </a>
                    </>
                  )
                )}
              </div>
            </div>
            <div className="map-tools">
              <div className="row" style={{ gap: 8 }}>
                <button type="button" className={`chip chip-sm ${!expand ? "on" : ""}`} onClick={() => setExpand(false)}>
                  선택 뉴스 기업만
                </button>
                <button type="button" className={`chip chip-sm ${expand ? "on" : ""}`} onClick={() => setExpand(true)}>
                  1홉 관계까지 확장
                </button>
              </div>
              <TypeLegend />
            </div>
            <div className="map-stage">
              {model ? (
                <MiniScene model={model} highlightId={companyId} onPickCompany={(id) => setCompany(id)} onPickEdge={() => navigate(`/?company=${seedIds[0]}`)} />
              ) : (
                <div className="stage-loading">
                  <span className="kick">relation map</span>
                  <i />
                  <span>{detail.isLoading || universe.isLoading ? "관계 지도를 만드는 중" : "이 뉴스와 연결된 기업이 그래프에 없습니다"}</span>
                </div>
              )}
              <div className="map-overlay" style={{ left: 16, top: 14 }}>
                <span className="pill">
                  3D RELATION MAP · {model?.nodes.length ?? 0} NODES · {model?.edges.length ?? 0} EDGES
                </span>
              </div>
              <div className="map-overlay" style={{ right: 16, bottom: 14 }}>
                <span className="meta">노드 클릭 → 좌측 목록이 그 기업 뉴스로 전환 · 관계선 클릭 → 전체 은하</span>
              </div>
            </div>
            <div className="map-foot">
              <span className="kick">
                {model?.nodes.length ?? 0} nodes · {model?.edges.length ?? 0} edges · {companyId ? "company context" : "1 selected news"}
              </span>
              <span className="meta">굵고 밝은 선일수록 관계 점수가 높습니다. 흐르는 방향이 공급·투자 방향입니다.</span>
            </div>
          </div>

          {/* ---------------- 하단 분석 ---------------- */}
          <div className="analysis">
            <div className="card full">
              <div className="kick">영향 분석</div>
              <div className="row between mt-8">
                <h2 className="sect">영향도가 큰 기업</h2>
                <span className="pill">점수순 · 100 기준</span>
              </div>
              <div className="mt-12">
                {!analysis && [0, 1, 2].map((i) => <Skeleton key={i} h={52} style={{ marginTop: 10 }} />)}
                {analysis?.ranking.map((r, i) => (
                  <button key={r.id} type="button" className="rank" onClick={() => setCompany(r.id)} title="이 기업 뉴스 보기">
                    <span className="kick num idx">{String(i + 1).padStart(2, "0")}</span>
                    <CompanyAvatar name={r.name} stockCode={r.stockCode} industry={r.industry} size={32} radius={16} />
                    <span className="nm">
                      <b>{r.name}</b>
                      <span className="meta num">
                        {marketLabel(r.market)} · {r.stockCode}
                      </span>
                    </span>
                    <ScoreBar score={r.score} color={r.hop === 0 ? "#829CFF" : "#F5A623"} />
                    <span className="sc">
                      <b className="num">{r.score}</b>
                      <span className="meta num">{r.hop === 0 ? "직접 언급" : `${r.hop} HOP`}</span>
                    </span>
                  </button>
                ))}
              </div>
              <div className="row mt-12" style={{ gap: 16 }}>
                <span className="meta row" style={{ gap: 6 }}>
                  <i className="dot" style={{ background: "#829CFF" }} /> 직접 언급 (기사 관련도 × 영향도)
                </span>
                <span className="meta row" style={{ gap: 6 }}>
                  <i className="dot" style={{ background: "#F5A623" }} /> 간접 전이 (관계 점수로 감쇠)
                </span>
              </div>
            </div>

            <div className="card">
              <div className="kick">주요 경로</div>
              <div className="row between mt-8">
                <h2 className="sect" style={{ fontSize: 20 }}>
                  가장 강한 전이 경로
                </h2>
                {analysis?.path && <span className="badge" style={{ background: "rgba(245,166,35,.13)", color: "var(--warn-fg)" }}>{analysis.path.strength}% 전이강도</span>}
              </div>
              {analysis?.path ? (
                <div className="path">
                  <div className="node">
                    <span className="lab">이벤트</span>
                    <b className="clamp-1" title={detail.data?.title}>
                      {detail.data?.title.slice(0, 18)}…
                    </b>
                  </div>
                  {analysis.path.steps.map((s, i) => (
                    <div key={s.id} style={{ display: "contents" }}>
                      <div className="hop">
                        <span>→</span>
                        {s.via && <TypeBadge type={s.via.relationshipType} />}
                        {s.via && <span className="num">{Math.round(s.via.score)}</span>}
                      </div>
                      <button type="button" className="node" onClick={() => setCompany(s.id)} style={{ textAlign: "left" }}>
                        <span className="lab">{i === 0 ? "직접 언급" : `${i} HOP`}</span>
                        <b>{s.name}</b>
                      </button>
                    </div>
                  ))}
                </div>
              ) : (
                <Empty>선택한 뉴스에서 이어지는 관계 경로가 없습니다.</Empty>
              )}
              <div className="meta mt-12" style={{ lineHeight: 1.7 }}>
                기사 영향도에 관계 점수(0~1)를 곱해 가장 큰 곱을 갖는 경로를 표시합니다. 프로토타입 계산식이며 투자 판단 자료가 아닙니다.
              </div>
            </div>

            <div className="card">
              <div className="kick">상세 정보</div>
              <div className="row between mt-8">
                <h2 className="sect" style={{ fontSize: 20 }}>
                  선택한 뉴스
                </h2>
                {detail.data && <SentimentBadge value={detail.data.relatedCompanies[0]?.sentiment ?? "NEUTRAL"} />}
              </div>
              {detail.data ? (
                <>
                  <div className="mt-12" style={{ fontSize: 15, fontWeight: 600, lineHeight: 1.45 }}>
                    {detail.data.title}
                  </div>
                  <div className="mt-8" style={{ fontSize: 14, lineHeight: 1.65, color: "var(--text-3)" }}>
                    {detail.data.summary}
                  </div>
                  <div className="kick mt-16">근거 문장</div>
                  <div className="ev-list">
                    {detail.data.evidence.map((e, i) => (
                      <div key={i} className="evidence">
                        <q>{e.sentence}</q>
                        <div className="meta num mt-8">신뢰도 {Math.round(e.confidence * 100)}%</div>
                      </div>
                    ))}
                  </div>
                  <div className="kick mt-16">관련 기업</div>
                  <div className="row wrap mt-8" style={{ gap: 8 }}>
                    {detail.data.relatedCompanies.map((r) => (
                      <button key={r.companyId} type="button" className={`chip chip-sm ${r.companyId === companyId ? "on" : ""}`} onClick={() => setCompany(r.companyId)}>
                        <i style={{ background: SENTIMENT_META[r.sentiment].color }} />
                        {r.name}
                        <b className="num" style={{ opacity: 0.7 }}>
                          {Math.round(r.relevanceScore * 100)}
                        </b>
                      </button>
                    ))}
                  </div>
                  <div className="meta mt-8">칩의 숫자는 관련도, 점 색은 해당 기업 기준 감성입니다. 누르면 그 기업 뉴스로 전환됩니다.</div>
                </>
              ) : (
                <Skeleton h={120} style={{ marginTop: 12 }} />
              )}
            </div>
          </div>
        </section>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* 분석: 영향도 랭킹 + 최강 전이 경로 (클라이언트 계산)                        */
/* ------------------------------------------------------------------ */
interface RankRow {
  id: string;
  name: string;
  stockCode: string;
  market: string;
  industry: string;
  score: number;
  hop: number;
}
interface PathStep {
  id: string;
  name: string;
  via: GraphEdge | null;
}
const DECAY = 0.82;
interface Best {
  score: number;
  hop: number;
  from: string | null;
  via: GraphEdge | null;
}

function analyze(detail: NewsDetail, graph: LatestGraph) {
  const info = new Map(graph.nodes.map((n) => [n.companyId, n]));
  const adj = new Map<string, GraphEdge[]>();
  graph.edges.forEach((e) => {
    for (const id of [e.sourceCompanyId, e.targetCompanyId]) {
      if (!adj.has(id)) adj.set(id, []);
      adj.get(id)!.push(e);
    }
  });
  const best = new Map<string, Best>();
  detail.relatedCompanies.forEach((r) => {
    const s = Math.round(r.relevanceScore * r.impactScore * 100);
    const cur = best.get(r.companyId);
    if (!cur || cur.score < s) best.set(r.companyId, { score: s, hop: 0, from: null, via: null });
  });
  // 1~2홉 전이
  const frontier = [...best.keys()];
  for (let hop = 1; hop <= 2; hop += 1) {
    const next: string[] = [];
    for (const id of frontier) {
      const b = best.get(id)!;
      if (b.hop !== hop - 1) continue;
      for (const e of adj.get(id) ?? []) {
        const o = e.sourceCompanyId === id ? e.targetCompanyId : e.sourceCompanyId;
        const s = Math.round(b.score * (e.score / 100) * DECAY);
        if (s < 3) continue;
        const cur = best.get(o);
        if (!cur || cur.score < s) {
          best.set(o, { score: s, hop, from: id, via: e });
          next.push(o);
        }
      }
    }
    frontier.push(...next);
  }
  const ranking: RankRow[] = [...best.entries()]
    .map(([id, b]) => {
      const n = info.get(id);
      const rel = detail.relatedCompanies.find((r) => r.companyId === id);
      return { id, name: n?.name ?? rel?.name ?? id, stockCode: n?.stockCode ?? "", market: n?.market ?? "", industry: n?.industryName ?? "", score: b.score, hop: b.hop };
    })
    .sort((a, b) => b.score - a.score)
    .slice(0, 6);

  // 최강 경로: hop>=1 중 최고 점수 노드에서 역추적
  let path: { strength: number; steps: PathStep[] } | null = null;
  const end = [...best.entries()].filter(([, b]) => b.hop >= 1).sort((a, b) => b[1].score - a[1].score)[0];
  if (end) {
    const steps: PathStep[] = [];
    let cur: string | null = end[0];
    while (cur) {
      const b: Best = best.get(cur)!;
      steps.unshift({ id: cur, name: info.get(cur)?.name ?? detail.relatedCompanies.find((r) => r.companyId === cur)?.name ?? cur, via: b.via });
      cur = b.from;
    }
    const start = best.get(steps[0].id)!.score;
    path = { strength: start ? Math.round((end[1].score / start) * 100) : 0, steps };
  }
  return { ranking, path };
}

export { relationshipMeta };
