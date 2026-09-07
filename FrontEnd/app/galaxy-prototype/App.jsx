'use client';

import { useEffect } from 'react';
import Scene from './three/Scene';
import { useGraph, useLiveUniverse } from './three/useGraph';
import GalaxyInterface from './ui/GalaxyInterface';
import { useGalaxy } from './store/useGalaxy';
import { COMPANY_BY_ID } from './data/universe';
import { mergeNewsSignalsNewestFirst } from '../../lib/news-data';
import { applyCompanyRolesToNews, inferTargetPriceRoles } from '../../lib/news-role-analysis';

function decodeNewsText(value = '') {
  const textarea = document.createElement('textarea');
  textarea.innerHTML = value;
  return textarea.value.replace(/\s+/g, ' ').trim();
}

function useLiveNewsFeed() {
  const companyQuery = useGalaxy((s) => s.companyQuery);
  const refreshNonce = useGalaxy((s) => s.refreshNonce);
  const setLiveFeed = useGalaxy((s) => s.setLiveFeed);
  const setFeedStatus = useGalaxy((s) => s.setFeedStatus);
  const feedStatus = useGalaxy((s) => s.feedStatus);
  const liveFeed = useGalaxy((s) => s.liveFeed);
  const refreshFeed = useGalaxy((s) => s.refreshFeed);

  useEffect(() => {
    const requested = new URLSearchParams(window.location.search).get('company')?.trim() ?? '';
    if (requested.length >= 2 && !useGalaxy.getState().companyQuery) {
      useGalaxy.getState().setCompanyQuery(requested);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const params = new URLSearchParams({
      scenario: 'all',
      refresh: `${Date.now()}-${refreshNonce}`,
    });
    if (companyQuery.trim().length >= 2) params.set('company', companyQuery.trim());

    const accept = (feed) => {
      if (!feed?.items?.length) throw new Error('empty_feed');
      const refined = feed.items.map((rawNews) => {
        const news = {
          ...rawNews,
          title: decodeNewsText(rawNews.title),
          summary: decodeNewsText(rawNews.summary),
          paragraphs: rawNews.paragraphs?.map(decodeNewsText) ?? [],
        };
        return applyCompanyRolesToNews(news, inferTargetPriceRoles(news));
      });
      const previous = useGalaxy.getState().liveFeed;
      const sameQuery = (previous?.companyQuery ?? '') === (feed.companyQuery ?? '');
      setLiveFeed({
        ...feed,
        items: sameQuery
          ? mergeNewsSignalsNewestFirst(refined, previous?.items ?? [], 60)
          : refined,
      });
    };

    const request = async (history = false) => {
      const next = new URLSearchParams(params);
      if (history) next.set('history', '1');
      const response = await fetch(`/api/news?${next}`, { cache: 'no-store', signal: controller.signal });
      if (!response.ok) throw new Error(`news_${response.status}`);
      accept(await response.json());
    };

    setFeedStatus('loading');
    void (async () => {
      try {
        await request(true);
      } catch (error) {
        if (error?.name === 'AbortError') return;
      }
      try {
        await request(false);
      } catch (error) {
        if (error?.name === 'AbortError') return;
        if (!useGalaxy.getState().liveFeed) setFeedStatus('fallback');
        else setFeedStatus('live');
      }
    })();

    return () => controller.abort();
  }, [companyQuery, refreshNonce, setFeedStatus, setLiveFeed]);

  useEffect(() => {
    if (feedStatus !== 'live' || !liveFeed) return undefined;
    const seconds = Math.max(60, Math.min(liveFeed.refreshAfterSeconds ?? 300, 900));
    const timer = window.setTimeout(refreshFeed, seconds * 1000);
    return () => window.clearTimeout(timer);
  }, [feedStatus, liveFeed, refreshFeed]);
}

export default function App({ authUser = null }) {
  useLiveNewsFeed();
  const phase = useGalaxy((s) => s.phase);
  const focus = useGalaxy((s) => s.focus);
  const warpTarget = useGalaxy((s) => s.warpTarget);
  const edges = useGraph();
  const liveUniverse = useLiveUniverse();

  // 키보드 단축키
  useEffect(() => {
    const onKey = (e) => {
      const s = useGalaxy.getState();
      if (s.phase === 'warp') return;
      if (e.key === 'Escape' && s.focus) s.backToGalaxy();
      else if (e.key === ' ') {
        e.preventDefault();
        s.togglePlay();
      } else if (e.key === 'ArrowRight') s.stepDay(e.shiftKey ? 20 : 5);
      else if (e.key === 'ArrowLeft') s.stepDay(e.shiftKey ? -20 : -5);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const warping = phase === 'warp';
  const dest = warpTarget ? (liveUniverse?.companyById[warpTarget]?.name ?? COMPANY_BY_ID[warpTarget]?.name) : '은하 전체';

  return (
      <main className="galaxy-experience">
      <div className="stage galaxy-viewport" aria-label="기업 관계 전체 은하">
        <Scene />
      </div>
      <GalaxyInterface universe={liveUniverse} edges={edges} authUser={authUser} />

      {warping && (
        <div className="warp-caption galaxy-warp-caption">
          <div><div className="t1">기업으로 이동 중</div><div className="t2">{dest}</div></div>
        </div>
      )}
      {!warping && focus === null && phase === 'galaxy' && (
        <div className="galaxy-help">드래그로 회전 · 휠로 확대 · 기업 또는 관계를 선택해 보세요</div>
      )}
      </main>
  );
}
