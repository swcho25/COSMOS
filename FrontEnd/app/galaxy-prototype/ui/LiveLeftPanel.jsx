import { useMemo } from 'react';
import { useGalaxy } from '../store/useGalaxy';
import { REL_TYPES } from '../data/universe';
import { useLiveUniverse } from '../three/useGraph';

function dateLabel(news) {
  const value = news.publishedAtIso ?? news.publishedAt;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return news.publishedAt;
  return new Intl.DateTimeFormat('ko-KR', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(date);
}

export default function LiveLeftPanel() {
  const liveFeed = useGalaxy((state) => state.liveFeed);
  const activeNewsId = useGalaxy((state) => state.activeNewsId);
  const selectNews = useGalaxy((state) => state.selectNews);
  const showAllNews = useGalaxy((state) => state.showAllNews);
  const threshold = useGalaxy((state) => state.threshold);
  const setThreshold = useGalaxy((state) => state.setThreshold);
  const activeTypes = useGalaxy((state) => state.activeTypes);
  const toggleType = useGalaxy((state) => state.toggleType);
  const onlySerendipity = useGalaxy((state) => state.onlySerendipity);
  const toggleSerendipity = useGalaxy((state) => state.toggleSerendipity);
  const universe = useLiveUniverse();

  const news = useMemo(() => liveFeed?.items ?? [], [liveFeed]);
  const fill = `${((threshold - 0.2) / 0.6) * 100}%`;

  return (
    <div className="panel left live-left">
      <div className="sec">
        <div className="sec-head">
          <span className="eyebrow">뉴스 관계 범위</span>
          <b className="num" style={{ fontSize: 11, color: 'var(--cyan)' }}>
            {universe?.companies.length ?? 0} NODES · {universe?.edges.length ?? 0} EDGES
          </b>
        </div>
        <button className={`scope-btn ${activeNewsId === null ? 'on' : ''}`} onClick={showAllNews}>
          <i />
          <span><b>전체 최신 뉴스 관계망</b><small>수집 기사 {news.length}건 + 산업 관계 통합</small></span>
        </button>
      </div>

      <div className="sec">
        <div className="sec-head">
          <span className="eyebrow">관계 강도</span>
          <b className="num" style={{ fontSize: 13 }}>≥ {threshold.toFixed(2)}</b>
        </div>
        <input className="slider" style={{ '--fill': fill }} type="range" min={0.2} max={0.8} step={0.01} value={threshold} onChange={(event) => setThreshold(parseFloat(event.target.value))} />
        <div className="chips compact-chips">
          {Object.values(REL_TYPES).map((type) => (
            <button key={type.id} className={`chip ${activeTypes.has(type.id) ? 'on' : ''}`} onClick={() => toggleType(type.id)}>
              <i className="dot" style={{ background: type.color }} />{type.name}
            </button>
          ))}
        </div>
        <button className={`chip ${onlySerendipity ? 'on' : ''}`} style={{ marginTop: 7, width: '100%', justifyContent: 'center' }} onClick={toggleSerendipity}>
          <i className="dot" style={{ background: '#ff9de0' }} />간접 전이만 보기
        </button>
      </div>

      <div className="sec" style={{ paddingBottom: 6 }}>
        <div className="sec-head" style={{ margin: 0 }}>
          <span className="eyebrow">실시간 뉴스 · 기사별 은하</span>
          <span style={{ fontSize: 10, color: 'var(--dim)' }}>클릭 → 관계 재구성</span>
        </div>
      </div>

      <div className="list live-news-list">
        {news.map((item, index) => (
          <button key={item.id} className={`news-row ${activeNewsId === item.id ? 'active' : ''}`} onClick={() => selectNews(item.id)}>
            <span className="news-index num">{String(index + 1).padStart(2, '0')}</span>
            <span className="news-copy">
              <span><i>{item.tag}</i>{dateLabel(item)}</span>
              <b>{item.title}</b>
              <small>{item.source} · 연결 기업 {item.affected.length}개</small>
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}
