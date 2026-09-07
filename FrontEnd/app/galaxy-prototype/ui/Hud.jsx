import { useEffect, useState } from 'react';
import { useGalaxy } from '../store/useGalaxy';
import { COMPANY_BY_ID, SECTORS, WINDOW, dateLabel } from '../data/universe';
import { useLiveUniverse } from '../three/useGraph';

export default function Hud({ edgeCount }) {
  const phase = useGalaxy((s) => s.phase);
  const focus = useGalaxy((s) => s.focus);
  const day = useGalaxy((s) => s.day);
  const showLabels = useGalaxy((s) => s.showLabels);
  const toggleLabels = useGalaxy((s) => s.toggleLabels);
  const backToGalaxy = useGalaxy((s) => s.backToGalaxy);
  const liveFeed = useGalaxy((s) => s.liveFeed);
  const feedStatus = useGalaxy((s) => s.feedStatus);
  const companyQuery = useGalaxy((s) => s.companyQuery);
  const setCompanyQuery = useGalaxy((s) => s.setCompanyQuery);
  const refreshFeed = useGalaxy((s) => s.refreshFeed);
  const liveUniverse = useLiveUniverse();
  const [draft, setDraft] = useState(companyQuery);

  useEffect(() => setDraft(companyQuery), [companyQuery]);

  const company = focus ? (liveUniverse?.companyById[focus] ?? COMPANY_BY_ID[focus]) : null;
  const observedAt = liveFeed?.collectedAt
    ? new Intl.DateTimeFormat('ko-KR', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(liveFeed.collectedAt))
    : dateLabel(day);
  const sectorColor = company ? SECTORS[company.sector]?.color : undefined;

  return (
    <div className="panel hud">
      <a className="brand" href="/" aria-label="코스모스 홈으로 이동">
        <h1>COSMOS GALAXY</h1>
        <small>실시간 뉴스 × 리스크 전이 탐사</small>
      </a>

      <div className="hud-sep" />

      <div className="hud-stat hud-coordinate">
        <span className="eyebrow">현재 좌표</span>
        <b style={company ? { color: sectorColor } : undefined}>
          {company ? company.name : '은하 전체'}
        </b>
      </div>

      <div className="hud-stat hud-observed">
        <span className="eyebrow">관측 시점</span>
        <b className="num">{observedAt}</b>
      </div>

      <div className="hud-stat hud-links">
        <span className="eyebrow">연결 수</span>
        <b className="num">{edgeCount}</b>
      </div>

      <div className="hud-stat hud-data" style={{ minWidth: 108 }}>
        <span className="eyebrow">데이터 상태</span>
        <b className="num" style={{ color: feedStatus === 'live' ? 'var(--cyan)' : 'var(--amber)' }}>
          {liveFeed ? 'LIVE · DB MERGE' : `${WINDOW}일 롤링`}
        </b>
      </div>

      <div className="hud-spacer" />

      <div className="hud-actions">
        <form
          className="hud-search"
          onSubmit={(event) => {
            event.preventDefault();
            const value = draft.trim().replace(/\s+/g, ' ');
            if (value.length === 0 || value.length >= 2) {
              window.history.replaceState(null, '', value ? `/network/?company=${encodeURIComponent(value)}` : '/network/');
              setCompanyQuery(value);
            }
          }}
        >
          <input value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="기업 뉴스 검색" aria-label="기업 뉴스 검색" />
          <button type="submit">검색</button>
        </form>
        <button className="btn ghost" onClick={refreshFeed}>{feedStatus === 'loading' ? '수집 중' : '새로고침'}</button>
        <button className={`btn ghost ${showLabels ? 'on' : ''}`} onClick={toggleLabels}>
          라벨
        </button>
        {focus && phase !== 'warp' && (
          <button className="btn" onClick={backToGalaxy}>
            ← 은하로 복귀
          </button>
        )}
        <nav className="hud-nav" aria-label="서비스 메뉴">
          <a href="/news/">뉴스</a>
          <a className="on" href="/network/">은하</a>
          <a href="/companies/">기업</a>
          <a href="/data/">데이터</a>
        </nav>
      </div>
    </div>
  );
}
