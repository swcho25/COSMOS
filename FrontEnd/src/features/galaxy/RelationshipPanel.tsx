import type { MetricWindow } from "@/api/types";
import { Empty, Icon, ImpactBadge, ScoreBar, Skeleton, TypeBadge } from "@/components/ui";
import { fmtDateTime, fmtRelative, fmtScore } from "@/lib/format";
import { relationshipMeta, scoreBand } from "@/lib/meta";
import { useRelationship, useRelationshipEvidence } from "@/lib/queries";
import { useGalaxy } from "@/store/galaxy";

const WINDOWS: MetricWindow[] = ["7D", "30D", "90D"];

interface Props {
  relationshipId: string;
  onClose: () => void;
  onPickCompany: (id: string) => void;
}

/** 관계선 선택 시: 양쪽 기업·유형·점수·영향 방향 + 근거 뉴스 */
export default function RelationshipPanel({ relationshipId, onClose, onPickCompany }: Props) {
  const window = useGalaxy((s) => s.window);
  const setWindow = useGalaxy((s) => s.setWindow);
  const rel = useRelationship(relationshipId, window);
  const evidence = useRelationshipEvidence(relationshipId);
  const r = rel.data;
  const meta = r ? relationshipMeta(r.relationshipType) : null;
  const items = evidence.data?.pages.flatMap((p) => p.items) ?? [];

  return (
    <aside className="card panel" aria-label="기업 관계 상세">
      <div className="panel-head">
        <span className="kick">기업 관계</span>
        <button type="button" className="icon-btn" onClick={onClose} aria-label="닫기">
          <Icon.Close />
        </button>
      </div>
      <div className="panel-body scroll">
        {!r || !meta ? (
          <>
            <Skeleton h={72} style={{ borderRadius: 12 }} />
            <Skeleton h={40} style={{ marginTop: 16 }} />
          </>
        ) : (
          <>
            <div className="row between">
              <TypeBadge type={r.relationshipType} directed={r.directionality === "DIRECTED"} />
              <ImpactBadge value={r.impactDirection} />
            </div>
            <div className="rel-pair mt-12">
              <button type="button" className="end" onClick={() => onPickCompany(r.sourceCompany.companyId)} title="이 기업 중심으로 보기">
                <span className="meta">{r.directionality === "DIRECTED" ? "source" : "기업 A"}</span>
                <b>{r.sourceCompany.name}</b>
              </button>
              <span className="arrow" aria-hidden="true">
                {r.directionality === "DIRECTED" ? "→" : "↔"}
              </span>
              <button type="button" className="end" onClick={() => onPickCompany(r.targetCompany.companyId)} title="이 기업 중심으로 보기">
                <span className="meta">{r.directionality === "DIRECTED" ? "target" : "기업 B"}</span>
                <b>{r.targetCompany.name}</b>
              </button>
            </div>
            <div className="meta mt-8" style={{ lineHeight: 1.6 }}>
              {meta.description}
            </div>

            <div className="row between mt-20">
              <span className="kick">관계 점수</span>
              <div className="seg" style={{ padding: 3 }}>
                {WINDOWS.map((w) => (
                  <button key={w} type="button" className={window === w ? "on" : ""} style={{ padding: "4px 9px", fontSize: 12 }} onClick={() => setWindow(w)}>
                    {w}
                  </button>
                ))}
              </div>
            </div>
            <div className="row mt-12" style={{ alignItems: "flex-end", gap: 14 }}>
              <div className="score-big num" style={{ color: meta.color }}>
                {fmtScore(r.score)}
              </div>
              <div className="grow">
                <div className="lab">{scoreBand(r.score)} · 100점 기준</div>
                <div className="mt-8" style={{ display: "flex" }}>
                  <ScoreBar score={r.score} color={meta.color} />
                </div>
              </div>
            </div>
            <div className="stat-grid mt-16">
              <div className="stat">
                <div className="lab">신뢰도</div>
                <div className="v num">{Math.round(r.confidence * 100)}%</div>
              </div>
              <div className="stat">
                <div className="lab">근거 문서</div>
                <div className="v num">
                  {r.evidenceCount} <span className="lab">건</span>
                </div>
              </div>
            </div>
            <div className="meta mt-12 num">
              기준 시각 {fmtDateTime(r.asOfAt)}
              {r.personalized && " · 내 가중치 적용"}
            </div>

            <div className="row between mt-20">
              <span className="kick">근거 뉴스</span>
              <span className="meta">공시는 점수에만 반영되며 공개되지 않습니다</span>
            </div>
            <div className="stack mt-12">
              {evidence.isLoading && [0, 1].map((i) => <Skeleton key={i} h={96} style={{ borderRadius: 14 }} />)}
              {!evidence.isLoading && !items.length && <Empty>공개 가능한 근거 뉴스가 없습니다.</Empty>}
              {items.map((e) => (
                <article key={e.newsId} className="news-card">
                  <div className="row between">
                    <span className="meta num">
                      {e.publisher} · {fmtRelative(e.publishedAt)}
                    </span>
                    <span className="meta num" title="관계 점수 기여도">
                      기여 {Math.round(e.contributionScore * 100)} · 신뢰 {Math.round(e.confidence * 100)}%
                    </span>
                  </div>
                  <div className="title">{e.title}</div>
                  <div className="evidence">
                    <q>{e.evidenceSentence}</q>
                  </div>
                  <div className="foot">
                    <span className="bar" style={{ flex: 1 }}>
                      <i style={{ width: `${Math.round(e.contributionScore * 100)}%`, background: meta.color }} />
                    </span>
                    <a href={e.originalUrl} target="_blank" rel="noreferrer" className="link-btn row" style={{ gap: 4 }}>
                      원문 <Icon.External />
                    </a>
                  </div>
                </article>
              ))}
            </div>
            {evidence.hasNextPage && (
              <button type="button" className="more-btn" onClick={() => evidence.fetchNextPage()} disabled={evidence.isFetchingNextPage}>
                더 보기
              </button>
            )}
          </>
        )}
      </div>
    </aside>
  );
}
