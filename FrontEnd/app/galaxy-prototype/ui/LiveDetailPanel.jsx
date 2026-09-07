import { useMemo } from 'react';
import { useGalaxy } from '../store/useGalaxy';
import { useLiveUniverse } from '../three/useGraph';
import { liveTypeMeta } from '../data/live-universe';
import { SECTORS } from '../data/universe';

function strengthLabel(edge) {
  if (edge.basis === 'disclosed') return '공시 관계';
  if (edge.basis === 'reported') return '기사 추출';
  if (edge.basis === 'mock') return '목업 보강';
  return '간접 전이';
}

function LiveRelation({ edge, focus, universe }) {
  const selectedEdge = useGalaxy((state) => state.selectedEdge);
  const selectEdge = useGalaxy((state) => state.selectEdge);
  const setHovered = useGalaxy((state) => state.setHovered);
  const open = selectedEdge === edge.key;
  const a = universe.companyById[edge.a];
  const b = universe.companyById[edge.b];
  const other = focus === edge.a ? b : focus === edge.b ? a : null;
  const meta = liveTypeMeta(edge.type);
  if (!a || !b) return null;

  return (
    <div className={`rel ${open ? 'active' : ''}`} onClick={() => selectEdge(edge.key)} onMouseEnter={() => other && setHovered(other.id)} onMouseLeave={() => setHovered(null)}>
      <div className="rel-top">
        <i className="dot" style={{ width: 7, height: 7, borderRadius: '50%', background: edge.serendipity ? 'var(--pink)' : meta.color, flex: 'none' }} />
        <span className="nm">{other ? other.name : `${a.name} ↔ ${b.name}`}</span>
        {edge.basis === 'modeled' && <span className="badge seren">간접 전이</span>}
        {edge.basis === 'mock' && <span className="badge mock">산업 관계</span>}
        <span className="corr num" style={{ color: edge.basis === 'mock' ? 'var(--muted)' : '#ff8f8f' }}>
          {edge.basis === 'mock' ? `M${Math.round(edge.corr * 100)}` : `+${edge.corr.toFixed(2)}`}
        </span>
      </div>
      <div style={{ display: 'flex', gap: 6, alignItems: 'center', marginBottom: 5 }}>
        <span className="badge" style={{ color: meta.color }}>{meta.name}</span>
        <span className="badge" style={{ color: edge.basis === 'modeled' ? 'var(--pink)' : 'var(--cyan)' }}>{strengthLabel(edge)}</span>
        <span style={{ marginLeft: 'auto', color: 'var(--dim)', fontSize: 10 }}>신뢰도 {edge.confidence}%</span>
      </div>
      <div className="reason">{edge.reason}</div>
      {open && (
        <div className="evidence">
          <div className="eyebrow">{edge.basis === 'mock' ? '목업 관계 안내' : '관계 근거'}</div>
          <p>{edge.basis === 'mock' ? '관계지도 밀도와 전이 경로 시연을 위한 보강 데이터이며, 강도는 합성값입니다.' : edge.sourceTitle}</p>
          {edge.sourceUrl && <a href={edge.sourceUrl} target="_blank" rel="noreferrer">원문 기사 확인 ↗</a>}
        </div>
      )}
    </div>
  );
}

export default function LiveDetailPanel({ edges }) {
  const focus = useGalaxy((state) => state.focus);
  const activeNewsId = useGalaxy((state) => state.activeNewsId);
  const universe = useLiveUniverse();
  const company = universe?.companyById[focus];
  const activeNews = universe?.activeNews;

  const list = useMemo(
    () => focus ? edges.filter((edge) => edge.a === focus || edge.b === focus) : edges.slice(0, 32),
    [edges, focus]
  );
  if (!universe) return <div className="panel right" />;

  const direct = universe.companies.filter((item) => item.direct).length;
  const mocked = universe.companies.filter((item) => item.isMock).length;
  const indirect = universe.companies.length - direct - mocked;
  const relatedNews = focus ? universe.newsByCompany[focus] ?? [] : [];

  return (
    <div className="panel right live-detail">
      <div className="detail-head">
        <span className="eyebrow">{company ? '기업 차원' : activeNewsId ? '선택 뉴스 관계 지도' : '실시간 뉴스 은하'}</span>
        <h2 style={company ? { color: SECTORS[company.sector].color } : undefined}>
          {company?.name ?? activeNews?.title ?? '관계 랭킹'}
        </h2>
        <div className="meta">
          {company
            ? company.isMock
              ? `${company.ticker} · ${company.industry} · 산업 관계 노드`
              : `${company.ticker} · ${company.industry} · 기사 연결 ${relatedNews.length}건`
            : activeNews
              ? `${activeNews.source} · ${activeNews.publishedAt}`
              : 'NAVER API HUB · DB 저장 뉴스 통합'}
        </div>
      </div>

      <div className="kpis live-kpis">
        <div className="kpi"><span className="eyebrow">직접</span><b className="num">{direct}</b></div>
        <div className="kpi"><span className="eyebrow">전이</span><b className="num" style={{ color: 'var(--pink)' }}>{indirect}</b></div>
        <div className="kpi"><span className="eyebrow">산업</span><b className="num" style={{ color: '#b98cff' }}>{mocked}</b></div>
        <div className="kpi"><span className="eyebrow">관계</span><b className="num">{edges.length}</b></div>
      </div>

      <div className="scroll">
        {!company && activeNews && (
          <section className="live-evidence-card">
            <span>{activeNews.tag} · {activeNews.sentiment} · NLP {activeNews.confidence}</span>
            <p>{activeNews.summary}</p>
            <a href={activeNews.sourceUrl} target="_blank" rel="noreferrer">실제 뉴스 원문 ↗</a>
          </section>
        )}

        <div className="sec" style={{ paddingBottom: 7 }}>
          <div className="sec-head" style={{ margin: 0 }}>
            <span className="eyebrow">{company ? `연결 기업 ${list.length}` : '관계 강도 랭킹'}</span>
            <span style={{ fontSize: 10, color: 'var(--dim)' }}>클릭 → 근거 펼치기</span>
          </div>
        </div>
        {list.length === 0 && <div className="empty">현재 기준을 만족하는 관계가 없습니다.<br />좌측에서 관계 강도를 낮춰보세요.</div>}
        {list.map((edge) => <LiveRelation key={edge.key} edge={edge} focus={focus} universe={universe} />)}

        {company && relatedNews.length > 0 && (
          <>
            <div className="sec" style={{ borderTop: '1px solid var(--line)' }}><span className="eyebrow">이 기업이 등장한 최신 기사</span></div>
            {relatedNews.map((news) => (
              <a className="newsitem live-source-news" key={news.id} href={news.sourceUrl} target="_blank" rel="noreferrer">
                <i className="bar" style={{ background: news.sentiment === '긍정' ? 'var(--good)' : 'var(--bad)' }} />
                <span><small>{news.publishedAt} · {news.tag}</small><b>{news.title}</b></span>
              </a>
            ))}
          </>
        )}
        {company?.isMock && relatedNews.length === 0 && (
          <div className="mock-company-note">이 노드는 실시간 기사 근거가 아닌 산업 관계망 시연용 기업입니다.</div>
        )}
      </div>
    </div>
  );
}
