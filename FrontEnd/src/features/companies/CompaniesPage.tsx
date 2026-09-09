import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import type { Market } from "@/api/types";
import { fmtDateTime } from "@/lib/format";
import { useCompanyDirectory, useIndustries, useLatestGraph, useToggleWatch, useWatchlist } from "@/lib/queries";
import { useSession } from "@/store/session";
import { useUi } from "@/store/ui";
import { CompanyAvatar, Empty, Icon, IndustryBadge, MarketTag, ScoreBar, Skeleton } from "@/components/ui";
import OwnershipView from "./OwnershipView";
import "./companies.css";

const MARKETS: { v: Market | ""; label: string }[] = [
  { v: "", label: "전체 시장" },
  { v: "KOSPI", label: "KOSPI" },
  { v: "KOSDAQ", label: "KOSDAQ" },
  { v: "NASDAQ", label: "NASDAQ" },
];

/** 기업 (화면구성도 ⑩·⑪). 목록 / 투자·지분 관계 토글. 행을 누르면 전체 은하의 기업 패널이 열린다. */
export default function CompaniesPage() {
  const navigate = useNavigate();
  const authed = useSession((s) => s.status === "authed");
  const openAuth = useUi((s) => s.openAuth);
  const [view, setView] = useState<"list" | "ownership">("list");
  const [keyword, setKeyword] = useState("");
  const [market, setMarket] = useState<Market | "">("");
  const [industryId, setIndustryId] = useState("");

  const industries = useIndustries();
  const universe = useLatestGraph(null);
  const watchlist = useWatchlist();
  const toggleWatch = useToggleWatch();
  const dir = useCompanyDirectory({ keyword: keyword.trim() || undefined, market: market || undefined, industryId: industryId || undefined, size: 40 });
  const rows = useMemo(() => dir.data?.pages.flatMap((p) => p.items) ?? [], [dir.data]);

  /** 스냅샷에서 기업별 관계 수·최고 점수 집계 */
  const stats = useMemo(() => {
    const m = new Map<string, { count: number; max: number }>();
    universe.data?.edges.forEach((e) => {
      for (const id of [e.sourceCompanyId, e.targetCompanyId]) {
        const s = m.get(id) ?? { count: 0, max: 0 };
        s.count += 1;
        s.max = Math.max(s.max, e.score);
        m.set(id, s);
      }
    });
    return m;
  }, [universe.data]);

  const open = (id: string) => navigate(`/?company=${id}`);

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <span className="pill">기업 데이터</span>
          <h1>연결 기업 데이터</h1>
          <p>최신 스냅샷의 관계 그래프에 포함된 {universe.data?.nodes.length ?? "…"}개 기업의 연결 관계와 관계 점수를 조회합니다.</p>
        </div>
        <span className="status-line">
          <i className="dot" />
          {universe.data ? `스냅샷 ${fmtDateTime(universe.data.asOfAt)} 기준` : "스냅샷 확인 중"}
        </span>
      </div>

      {/* 관심 기업 */}
      <section className="card watch-card" aria-label="내 관심 기업">
        <div className="row between wrap">
          <div>
            <div className="kick">my watchlist</div>
            <h2 className="sect" style={{ marginTop: 6, fontSize: 20 }}>
              내 관심 기업 <span className="num" style={{ color: "var(--primary-2)" }}>{watchlist.data?.items.length ?? 0}</span>
            </h2>
          </div>
          <span className="meta">{authed ? "프로필 메뉴 › 내 페이지에서 관리" : "로그인하면 관심 기업을 저장할 수 있습니다"}</span>
        </div>
        {authed ? (
          watchlist.data?.items.length ? (
            <div className="watch-grid">
              {watchlist.data.items.map((w) => {
                const s = stats.get(w.companyId);
                return (
                  <button key={w.companyId} type="button" className="watch-item" onClick={() => open(w.companyId)}>
                    <CompanyAvatar name={w.name} stockCode={w.stockCode} industry={w.primaryIndustry.name} size={40} />
                    <span className="grow">
                      <b>{w.name}</b>
                      <span className="meta num">
                        {w.primaryIndustry.name} · 관계 {s?.count ?? 0}개
                      </span>
                    </span>
                  </button>
                );
              })}
            </div>
          ) : (
            <Empty>아직 관심 기업이 없습니다. 아래 목록의 ★ 을 눌러 추가하세요.</Empty>
          )
        ) : (
          <div className="row mt-12" style={{ gap: 10 }}>
            <button type="button" className="btn btn-p btn-sm" onClick={() => openAuth("login")}>
              로그인
            </button>
            <button type="button" className="btn btn-g btn-sm" onClick={() => openAuth("signup")}>
              회원가입
            </button>
          </div>
        )}
      </section>

      {/* 디렉터리 */}
      <section className="card dir-card" aria-label="기업 목록">
        <div className="dir-head">
          <div>
            <div className="kick">company directory</div>
            <h2 className="sect" style={{ marginTop: 6 }}>
              {view === "list" ? "뉴스·관계망 연결 기업" : "투자·지분 관계"}
            </h2>
            <p className="meta mt-8" style={{ fontSize: 13 }}>
              {view === "list" ? "관계 수와 최고 관계 점수는 최신 공개 스냅샷 기준입니다." : "스냅샷의 투자·지분 유형 관계를 지배 방향(보유 → 피보유)으로 표시합니다. 숫자는 관계 점수입니다."}
            </p>
          </div>
          <div className="row" style={{ gap: 12 }}>
            <div className="seg">
              <button type="button" className={view === "list" ? "on" : ""} onClick={() => setView("list")}>
                목록
              </button>
              <button type="button" className={view === "ownership" ? "on" : ""} onClick={() => setView("ownership")}>
                투자·지분 관계
              </button>
            </div>
            <span className="pill num">{universe.data?.nodes.length ?? 0} COMPANIES</span>
          </div>
        </div>

        {view === "ownership" ? (
          <OwnershipView graph={universe.data ?? null} onPick={open} />
        ) : (
          <>
            <div className="dir-tools">
              <label className="field">
                <Icon.Search />
                <input placeholder="기업명·영문명·종목코드" value={keyword} onChange={(e) => setKeyword(e.target.value)} />
              </label>
              <select value={market} onChange={(e) => setMarket(e.target.value as Market | "")} aria-label="시장">
                {MARKETS.map((m) => (
                  <option key={m.v} value={m.v}>
                    {m.label}
                  </option>
                ))}
              </select>
              <select value={industryId} onChange={(e) => setIndustryId(e.target.value)} aria-label="산업">
                <option value="">전체 산업</option>
                {industries.data?.items.map((i) => (
                  <option key={i.industryId} value={i.industryId}>
                    {i.name} ({i.companyCount})
                  </option>
                ))}
              </select>
              <span className="meta" style={{ marginLeft: "auto" }}>
                {keyword.trim() ? "관련도순" : "기업명순"}
              </span>
            </div>
            <div style={{ overflowX: "auto" }}>
              <table className="dir-table">
                <thead>
                  <tr>
                    <th>기업</th>
                    <th>산업</th>
                    <th>시장</th>
                    <th className="right">연결 관계</th>
                    <th style={{ width: 220 }}>최고 관계 점수</th>
                    <th className="right">관심</th>
                  </tr>
                </thead>
                <tbody>
                  {dir.isLoading &&
                    [0, 1, 2, 3, 4].map((i) => (
                      <tr key={i}>
                        <td colSpan={6}>
                          <Skeleton h={40} />
                        </td>
                      </tr>
                    ))}
                  {!dir.isLoading && !rows.length && (
                    <tr>
                      <td colSpan={6}>
                        <Empty>조건에 맞는 기업이 없습니다.</Empty>
                      </td>
                    </tr>
                  )}
                  {rows.map((c) => {
                    const s = stats.get(c.companyId);
                    return (
                      <tr key={c.companyId} className="row-btn" onClick={() => open(c.companyId)} tabIndex={0} onKeyDown={(e) => e.key === "Enter" && open(c.companyId)}>
                        <td>
                          <div className="co">
                            <CompanyAvatar name={c.name} stockCode={c.stockCode} industry={c.primaryIndustry.name} size={36} />
                            <div>
                              <b>{c.name}</b>
                              <span className="meta num">
                                {c.stockCode} · {c.nameEn}
                              </span>
                            </div>
                          </div>
                        </td>
                        <td>
                          <IndustryBadge name={c.primaryIndustry.name} />
                        </td>
                        <td>
                          <MarketTag market={c.market} />
                        </td>
                        <td className="right">
                          <span className="num num-cell">{s?.count ?? 0}</span>
                          <span className="unit">개</span>
                        </td>
                        <td>
                          <div className="row" style={{ gap: 10 }}>
                            <ScoreBar score={s?.max ?? 0} />
                            <span className="num" style={{ width: 32, textAlign: "right", fontWeight: 700 }}>
                              {s ? Math.round(s.max) : "-"}
                            </span>
                          </div>
                        </td>
                        <td className="right">
                          <button
                            type="button"
                            className={`star-btn ${c.watched ? "on" : ""}`}
                            aria-label={c.watched ? "관심 해제" : "관심 등록"}
                            onClick={(e) => {
                              e.stopPropagation();
                              if (!authed) return openAuth("login");
                              toggleWatch.mutate({ companyId: c.companyId, watched: c.watched });
                            }}
                          >
                            <Icon.Star filled={c.watched} />
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <div className="dir-foot">
              <span className="meta">행을 선택하면 전체 은하에서 해당 기업의 관계망과 우측 인텔리전스 패널이 열립니다.</span>
              {dir.hasNextPage && (
                <button type="button" className="btn btn-g btn-sm" onClick={() => dir.fetchNextPage()} disabled={dir.isFetchingNextPage}>
                  {dir.isFetchingNextPage ? "불러오는 중…" : "더 보기"}
                </button>
              )}
            </div>
          </>
        )}
      </section>
    </div>
  );
}
