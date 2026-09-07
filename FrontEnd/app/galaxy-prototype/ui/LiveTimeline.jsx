import { useEffect, useMemo } from 'react';
import { useGalaxy } from '../store/useGalaxy';

function formatDate(news) {
  const date = new Date(news.publishedAtIso ?? news.publishedAt);
  if (Number.isNaN(date.getTime())) return news.publishedAt;
  return new Intl.DateTimeFormat('ko-KR', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(date);
}

export default function LiveTimeline() {
  const liveFeed = useGalaxy((state) => state.liveFeed);
  const activeNewsId = useGalaxy((state) => state.activeNewsId);
  const selectNews = useGalaxy((state) => state.selectNews);
  const playing = useGalaxy((state) => state.playing);
  const togglePlay = useGalaxy((state) => state.togglePlay);
  const news = useMemo(() => [...(liveFeed?.items ?? [])].reverse(), [liveFeed]);
  const activeIndex = Math.max(0, news.findIndex((item) => item.id === activeNewsId));
  const current = news[activeIndex] ?? news.at(-1);

  useEffect(() => {
    if (!playing || news.length < 2) return undefined;
    const timer = window.setInterval(() => {
      const state = useGalaxy.getState();
      const index = news.findIndex((item) => item.id === state.activeNewsId);
      const next = index < 0 || index >= news.length - 1 ? 0 : index + 1;
      state.selectNews(news[next].id);
    }, 1800);
    return () => window.clearInterval(timer);
  }, [news, playing]);

  if (!news.length) return null;
  const move = (delta) => {
    const next = (activeIndex + delta + news.length) % news.length;
    selectNews(news[next].id);
  };

  return (
    <div className="panel timeline live-timeline">
      <div className="tl-head">
        <button className="btn" onClick={togglePlay} style={{ width: 78 }}>{playing ? '❚❚ 정지' : '▶ 재생'}</button>
        <button className="btn ghost" onClick={() => move(-1)}>이전 기사</button>
        <button className="btn ghost" onClick={() => move(1)}>다음 기사</button>
        <span className="eyebrow" style={{ marginLeft: 4 }}>뉴스 시점</span>
        <span className="tl-date num">{current ? formatDate(current) : '-'}</span>
        <div className="macro-now"><b>[{current?.tag}]</b><span>{current?.title}</span></div>
      </div>
      <div className="tl-track">
        <div className="tl-marks">
          {news.map((item, index) => (
            <button
              key={item.id}
              className={`tl-live-mark ${item.id === activeNewsId ? 'on' : ''}`}
              style={{ left: `${(index / Math.max(1, news.length - 1)) * 100}%` }}
              title={`${formatDate(item)} ${item.title}`}
              onClick={() => selectNews(item.id)}
            />
          ))}
        </div>
        <input className="slider" style={{ '--fill': `${(activeIndex / Math.max(1, news.length - 1)) * 100}%` }} type="range" min={0} max={Math.max(0, news.length - 1)} value={activeIndex} onChange={(event) => selectNews(news[Number(event.target.value)].id)} />
      </div>
      <div className="tl-legend">
        <i><b style={{ color: 'var(--cyan)' }}>▌</b> 실제 수집 뉴스</i>
        <i><b style={{ color: 'var(--pink)' }}>▌</b> 모델 간접 전이</i>
        <i style={{ marginLeft: 'auto' }}>{liveFeed?.provider} · DB 이전 기사 + 신규 기사 병합</i>
      </div>
    </div>
  );
}
