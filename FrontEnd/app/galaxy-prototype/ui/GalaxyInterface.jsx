import { useEffect, useMemo, useRef, useState } from 'react';
import PortalNav from '../../portal-nav';
import { useGalaxy } from '../store/useGalaxy';
import {
  COMPANIES,
  N_DAYS,
  SECTORS,
  SECTOR_LIST,
  changePct,
  normalizedPrice,
  priceAt,
} from '../data/universe';
import { liveTypeMeta } from '../data/live-universe';
import { tickerCode, tossSecuritiesLogoUrl } from '../data/company-logo';

const normalize = (value = '') =>
  String(value).toLocaleLowerCase('ko-KR').replace(/[^a-z0-9가-힣]/g, '');

const BRIGHTNESS_STORAGE_KEY = 'nexus-planet-brightness';

function companyMatches(left, right) {
  const a = normalize(left);
  const b = normalize(right);
  return Boolean(a && b && (a === b || (Math.min(a.length, b.length) >= 3 && (a.includes(b) || b.includes(a)))));
}

function directMentioned(news, company) {
  if (!news || !company) return false;
  const code = tickerCode(company.ticker);
  return (news.entities ?? []).some((entity) => {
    if (entity.type !== 'COMPANY') return false;
    return companyMatches(entity.name, company.name) || (code && tickerCode(entity.code ?? entity.name) === code);
  });
}

function SearchIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <circle cx="11" cy="11" r="6.5" />
      <path d="m16 16 4 4" />
    </svg>
  );
}

function CloseIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="m7 7 10 10M17 7 7 17" />
    </svg>
  );
}

function CompanyMark({ company, size = 'md' }) {
  const color = SECTORS[company?.sector]?.color ?? '#6d8cff';
  const letters = company?.name?.replace(/[^A-Za-z가-힣]/g, '').slice(0, 2) || 'NX';
  const logoUrl = tossSecuritiesLogoUrl(company);
  return (
    <span className={`company-mark ${size}`} style={{ '--company-color': color }}>
      <span>{letters}</span>
      {logoUrl && <img src={logoUrl} alt="" onError={(event) => { event.currentTarget.hidden = true; }} />}
    </span>
  );
}

function stockSnapshot(company) {
  if (!company) return null;
  const code = tickerCode(company.ticker);
  const source = COMPANIES.find((item) => item.id === code || companyMatches(item.name, company.name));
  if (source) {
    const start = N_DAYS - 52;
    return {
      values: Array.from(normalizedPrice(source.id, start, N_DAYS - 1), (value) => value * 100),
      price: priceAt(source.id, N_DAYS - 1),
      change: changePct(source.id, N_DAYS - 1, 20) * 100,
      currency: '₩',
      available: true,
    };
  }
  return { values: [], price: null, change: null, currency: '', available: false };
}

function sparkPath(values, width = 320, height = 112) {
  if (!values?.length) return '';
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  return values.map((value, index) => {
    const x = (index / Math.max(1, values.length - 1)) * width;
    const y = height - 8 - ((value - min) / span) * (height - 16);
    return `${index ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
}

function StockCard({ company, compact = false }) {
  const snapshot = useMemo(() => stockSnapshot(company), [company]);
  if (!snapshot?.available) {
    return (
      <div className={`stock-empty ${compact ? 'compact' : ''}`}>
        <span>주가 데이터 연결 준비 중</span>
        <p>종목코드 기반 시세 어댑터가 연결되면 이 영역에 실제 차트가 표시됩니다.</p>
      </div>
    );
  }
  const positive = snapshot.change >= 0;
  return (
    <section className={`stock-card ${compact ? 'compact' : ''}`}>
      <div className="stock-card-head">
        <div>
          <span>{compact ? company.name : '최근 1개월'}</span>
          <strong>{snapshot.currency}{Math.round(snapshot.price).toLocaleString('ko-KR')}</strong>
        </div>
        <b className={positive ? 'positive' : 'negative'}>{positive ? '+' : ''}{snapshot.change.toFixed(2)}%</b>
      </div>
      {!compact && (
        <svg className="stock-sparkline" viewBox="0 0 320 112" preserveAspectRatio="none" role="img" aria-label={`${company.name} 프로토타입 주가 추이`}>
          <defs>
            <linearGradient id={`stock-${company.id}`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={positive ? '#36c985' : '#f06572'} stopOpacity=".22" />
              <stop offset="100%" stopColor={positive ? '#36c985' : '#f06572'} stopOpacity="0" />
            </linearGradient>
          </defs>
          <path className="area" d={`${sparkPath(snapshot.values)} L320,112 L0,112 Z`} fill={`url(#stock-${company.id})`} />
          <path d={sparkPath(snapshot.values)} stroke={positive ? '#36c985' : '#f06572'} />
        </svg>
      )}
      <small>프로토타입 시계열 · 실제 시세 API 연동 전</small>
    </section>
  );
}

function NewsList({ items, emptyText = '최근 직접 언급된 뉴스가 없습니다.' }) {
  if (!items.length) return <div className="detail-empty"><span>{emptyText}</span><p>새 기사 수집 시 자동으로 반영됩니다.</p></div>;
  return (
    <div className="direct-news-list">
      {items.slice(0, 8).map((news) => (
        <a key={news.id} href={news.sourceUrl} target="_blank" rel="noreferrer" className="direct-news-card">
          <div><span>직접 언급</span><small>{news.source} · {news.publishedAt}</small></div>
          <strong>{news.title}</strong>
          <p>{news.summary}</p>
          <b>원문 보기 ↗</b>
        </a>
      ))}
    </div>
  );
}

function SectorCompanyList({ companies, onSelect }) {
  if (!companies.length) return <div className="detail-empty"><span>해당 산업의 연결 기업이 없습니다.</span></div>;
  return (
    <div className="sector-company-list">
      {companies.map((company) => (
        <button key={company.id} type="button" onClick={() => onSelect(company.id)}>
          <CompanyMark company={company} />
          <span><b>{company.name}</b><small>{company.ticker} · {company.industry}</small></span>
          <em>{Math.round(company.score ?? 0)}</em>
          <i>이동 →</i>
        </button>
      ))}
    </div>
  );
}

function GalaxyDetailPanel({ universe, edges, onClose }) {
  const focus = useGalaxy((state) => state.focus);
  const selectedEdgeKey = useGalaxy((state) => state.selectedEdge);
  const activeSector = useGalaxy((state) => state.activeSector);
  const closeDetail = useGalaxy((state) => state.closeDetail);
  const warpTo = useGalaxy((state) => state.warpTo);
  const [tab, setTab] = useState('news');
  const selectedEdge = edges.find((edge) => edge.key === selectedEdgeKey)
    ?? universe?.edges?.find((edge) => edge.key === selectedEdgeKey);
  const company = universe?.companyById[focus];
  const edgeA = selectedEdge ? universe?.companyById[selectedEdge.a] : null;
  const edgeB = selectedEdge ? universe?.companyById[selectedEdge.b] : null;
  const sector = activeSector ? SECTORS[activeSector] : null;
  const sectorCompanies = sector
    ? (universe?.companies ?? [])
        .filter((item) => item.sector === activeSector)
        .sort((a, b) => Number(b.direct) - Number(a.direct) || b.score - a.score)
    : [];
  const selectedKey = selectedEdge?.key ?? company?.id ?? sector?.id ?? 'none';

  useEffect(() => setTab('news'), [selectedKey]);

  const companyNews = company
    ? (universe?.newsByCompany[company.id] ?? []).filter((news) => directMentioned(news, company))
    : [];
  const liveFeedItems = useGalaxy((state) => state.liveFeed?.items);
  const feed = liveFeedItems ?? [];
  const edgeNews = selectedEdge
    ? feed.filter((news) =>
        news.id === selectedEdge.sourceNewsId ||
        (directMentioned(news, edgeA) && directMentioned(news, edgeB)))
    : [];
  const sectorNews = sector
    ? feed.filter((news) => sectorCompanies.some((item) => directMentioned(news, item)))
    : [];
  const hasSelection = Boolean(selectedEdge || company || sector);

  if (!hasSelection) return null;

  return (
    <aside className="galaxy-detail-panel selected" aria-live="polite">
      <div className="detail-panel-topline">
        <span>{selectedEdge ? '관계 인텔리전스' : company ? '기업 인텔리전스' : '산업 인텔리전스'}</span>
        <button type="button" onClick={onClose ?? closeDetail} aria-label="상세 패널 닫기"><CloseIcon /></button>
      </div>

      {selectedEdge && edgeA && edgeB ? (
        <>
          <header className="relation-detail-head">
            <div><CompanyMark company={edgeA} /><b>{edgeA.name}</b><small>{edgeA.ticker}</small></div>
            <span>→</span>
            <div><CompanyMark company={edgeB} /><b>{edgeB.name}</b><small>{edgeB.ticker}</small></div>
          </header>
          <div className="relation-summary">
            <span style={{ '--relation-color': liveTypeMeta(selectedEdge.type).color }}>{liveTypeMeta(selectedEdge.type).name}</span>
            <b>{Math.round(Math.abs(selectedEdge.corr) * 100)}%</b>
            <small>{selectedEdge.basis === 'reported' ? '뉴스 추출 관계' : selectedEdge.basis === 'disclosed' ? '공시 기반 관계' : selectedEdge.basis === 'mock' ? '프로토타입 보강 관계' : '모델 기반 간접 전이'}</small>
            <p>{selectedEdge.reason}</p>
          </div>
          <div className="detail-tabs" role="tablist">
            <button className={tab === 'news' ? 'active' : ''} onClick={() => setTab('news')}>근거 뉴스</button>
            <button className={tab === 'stock' ? 'active' : ''} onClick={() => setTab('stock')}>주가 비교</button>
          </div>
          <div className="detail-scroll">
            {tab === 'news' ? <NewsList items={edgeNews} emptyText="이 관계를 직접 뒷받침하는 뉴스가 없습니다." /> : (
              <div className="edge-stock-grid"><StockCard company={edgeA} compact /><StockCard company={edgeB} compact /></div>
            )}
          </div>
        </>
      ) : company ? (
        <>
          <header className="company-detail-head">
            <CompanyMark company={company} size="lg" />
            <div><h2>{company.name}</h2><p>{company.ticker} · {company.industry}</p></div>
            <span style={{ '--sector-color': SECTORS[company.sector]?.color }}>{SECTORS[company.sector]?.name}</span>
          </header>
          <div className="detail-tabs" role="tablist">
            <button className={tab === 'news' ? 'active' : ''} onClick={() => setTab('news')}>뉴스 <i>{companyNews.length}</i></button>
            <button className={tab === 'stock' ? 'active' : ''} onClick={() => setTab('stock')}>주가</button>
          </div>
          <div className="detail-scroll">
            {tab === 'news' ? <NewsList items={companyNews} /> : <StockCard company={company} />}
          </div>
        </>
      ) : sector ? (
        <>
          <header className="sector-detail-head" style={{ '--sector-color': sector.color }}>
            <span><i /><i /><i /></span>
            <div><h2>{sector.name}</h2><p>{sectorCompanies.length}개 기업 · 직접 언급 뉴스 {sectorNews.length}건</p></div>
            <b>SECTOR</b>
          </header>
          <div className="detail-tabs" role="tablist">
            <button className={tab === 'news' ? 'active' : ''} onClick={() => setTab('news')}>뉴스 <i>{sectorNews.length}</i></button>
            <button className={tab === 'companies' ? 'active' : ''} onClick={() => setTab('companies')}>기업 <i>{sectorCompanies.length}</i></button>
          </div>
          <div className="detail-scroll">
            {tab === 'news'
              ? <NewsList items={sectorNews} emptyText={`${sector.name} 기업이 직접 언급된 최신 뉴스가 없습니다.`} />
              : <SectorCompanyList companies={sectorCompanies} onSelect={warpTo} />}
          </div>
        </>
      ) : null}
    </aside>
  );
}

function GalaxySearch({ universe }) {
  const [query, setQuery] = useState('');
  const [open, setOpen] = useState(false);
  const [pendingQuery, setPendingQuery] = useState('');
  const inputRef = useRef(null);
  const warpTo = useGalaxy((state) => state.warpTo);
  const setActiveSector = useGalaxy((state) => state.setActiveSector);
  const setCompanyQuery = useGalaxy((state) => state.setCompanyQuery);
  const feedStatus = useGalaxy((state) => state.feedStatus);

  useEffect(() => {
    const onKey = (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        setOpen(true);
        inputRef.current?.focus();
      }
      if (event.key === 'Escape') setOpen(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const results = useMemo(() => {
    const needle = normalize(query);
    if (!needle || !universe) return [];
    return universe.companies
      .filter((company) => normalize(`${company.name}${company.ticker}${company.industry}`).includes(needle))
      .sort((a, b) => Number(b.direct) - Number(a.direct) || b.score - a.score)
      .slice(0, 8);
  }, [query, universe]);

  const choose = (company) => {
    setOpen(false);
    setQuery(company.name);
    setPendingQuery('');
    setActiveSector(null);
    warpTo(company.id);
  };

  useEffect(() => {
    if (!pendingQuery || !universe) return;
    const wanted = normalize(pendingQuery);
    const found = universe.companies.find((company) =>
      normalize(`${company.name}${company.ticker}`).includes(wanted)
    );
    if (found) choose(found);
  // choose is an event helper; the pending query and universe update are the trigger.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingQuery, universe]);

  const submit = (event) => {
    event.preventDefault();
    if (results[0]) {
      choose(results[0]);
      return;
    }
    const value = query.trim().replace(/\s+/g, ' ');
    if (value.length < 2) return;
    setPendingQuery(value);
    setCompanyQuery(value);
    setOpen(true);
  };

  return (
    <div className={`galaxy-search ${open ? 'open' : ''}`}>
      <form onSubmit={submit}>
        <SearchIcon />
        <input
          ref={inputRef}
          value={query}
          onFocus={() => setOpen(true)}
          onChange={(event) => { setQuery(event.target.value); setOpen(true); }}
          placeholder="기업명 또는 종목코드를 검색하세요"
          aria-label="기업 검색"
        />
        {query ? <button type="button" className="search-clear" onClick={() => setQuery('')} aria-label="검색어 지우기"><CloseIcon /></button> : <kbd>Ctrl K</kbd>}
      </form>
      {open && query && (
        <div className="search-results">
          <div className="search-result-caption"><span>검색 결과</span><small>{results.length}개 기업</small></div>
          {results.map((company) => (
            <button key={company.id} type="button" onClick={() => choose(company)}>
              <CompanyMark company={company} />
              <span><b>{company.name}</b><small>{company.ticker} · {company.industry}</small></span>
              <em style={{ '--sector-color': SECTORS[company.sector]?.color }}>{SECTORS[company.sector]?.name}</em>
              <i>이동 →</i>
            </button>
          ))}
          {!results.length && <p className="no-search-result">{pendingQuery && feedStatus === 'loading' ? '최신 뉴스에서 기업을 찾고 있습니다…' : 'Enter를 누르면 최신 뉴스에서 이 기업을 검색합니다.'}</p>}
        </div>
      )}
    </div>
  );
}

function SectorFilter({ universe }) {
  const activeSector = useGalaxy((state) => state.activeSector);
  const setActiveSector = useGalaxy((state) => state.setActiveSector);
  return (
    <div className="sector-filter" aria-label="산업 필터">
      <button type="button" className={!activeSector ? 'active' : ''} onClick={() => setActiveSector(null)}>전체 <span>{universe?.companies.length ?? 0}</span></button>
      {SECTOR_LIST.map((sector) => {
        const count = universe?.companies.filter((company) => company.sector === sector.id).length ?? 0;
        if (!count) return null;
        return (
          <button
            type="button"
            key={sector.id}
            className={activeSector === sector.id ? 'active' : ''}
            style={{ '--sector-color': sector.color }}
            onClick={() => setActiveSector(activeSector === sector.id ? null : sector.id)}
          >
            <i />{sector.name}<span>{count}</span>
          </button>
        );
      })}
    </div>
  );
}

function BrightnessControl() {
  const planetBrightness = useGalaxy((state) => state.planetBrightness);
  const setPlanetBrightness = useGalaxy((state) => state.setPlanetBrightness);

  useEffect(() => {
    const savedValue = window.localStorage.getItem(BRIGHTNESS_STORAGE_KEY);
    if (savedValue === null) return;
    const saved = Number(savedValue);
    if (saved >= 0 && saved <= 1.3) setPlanetBrightness(saved);
  }, [setPlanetBrightness]);

  const updateBrightness = (event) => {
    const next = Number(event.target.value);
    setPlanetBrightness(next);
    window.localStorage.setItem(BRIGHTNESS_STORAGE_KEY, String(next));
  };

  return (
    <label className="galaxy-brightness-control">
      <span aria-hidden="true">☼</span>
      <b>행성 밝기</b>
      <input
        type="range"
        min="0"
        max="1.3"
        step="0.05"
        value={planetBrightness}
        onChange={updateBrightness}
        aria-label="기업 행성 밝기"
      />
      <output>{Math.round(planetBrightness * 100)}%</output>
    </label>
  );
}

export default function GalaxyInterface({ universe, edges, authUser }) {
  const feedStatus = useGalaxy((state) => state.feedStatus);
  const refreshFeed = useGalaxy((state) => state.refreshFeed);
  const focus = useGalaxy((state) => state.focus);
  const selectedEdge = useGalaxy((state) => state.selectedEdge);
  const activeSector = useGalaxy((state) => state.activeSector);
  const [samsungNewsOpen, setSamsungNewsOpen] = useState(false);
  const focusedCompany = universe?.companyById[focus];
  const isSamsungPresentation = tickerCode(focusedCompany?.ticker) === '005930';
  const showSamsungNewsPanel = Boolean(isSamsungPresentation && samsungNewsOpen && !selectedEdge);
  const showDetailPanel = Boolean(selectedEdge || activeSector || (focus && (!isSamsungPresentation || showSamsungNewsPanel)));
  const samsungNewsCount = isSamsungPresentation
    ? (universe?.newsByCompany[focus] ?? []).filter((news) => directMentioned(news, focusedCompany)).length
    : 0;
  const observedAt = useGalaxy((state) => state.liveFeed?.collectedAt);
  const observedLabel = observedAt
    ? new Intl.DateTimeFormat('ko-KR', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(observedAt))
    : '수집 대기';

  useEffect(() => {
    setSamsungNewsOpen(false);
  }, [focus]);

  useEffect(() => {
    if (selectedEdge) setSamsungNewsOpen(false);
  }, [selectedEdge]);

  return (
    <div className={`galaxy-ui-layer ${showDetailPanel ? 'detail-open' : ''}`}>
      <PortalNav active="home" user={authUser} />

      <GalaxySearch universe={universe} />
      <SectorFilter universe={universe} />
      <BrightnessControl />
      {isSamsungPresentation && !selectedEdge && !samsungNewsOpen && (
        <button type="button" className="samsung-news-tab" onClick={() => setSamsungNewsOpen(true)} aria-label="삼성전자 실시간 뉴스 열기">
          <span>LIVE</span>
          <b>실시간 뉴스</b>
          <i>{samsungNewsCount}</i>
        </button>
      )}
      {showDetailPanel && (
        <GalaxyDetailPanel
          universe={universe}
          edges={edges}
          onClose={showSamsungNewsPanel ? () => setSamsungNewsOpen(false) : undefined}
        />
      )}

      {!isSamsungPresentation && (
        <div className="galaxy-status-card">
          <div><span className={feedStatus} /><p><b>{feedStatus === 'loading' ? '뉴스 수집 중' : feedStatus === 'live' ? '실시간 데이터 연결' : '저장 데이터 모드'}</b><small>{observedLabel} 기준</small></p></div>
          <button type="button" onClick={refreshFeed} disabled={feedStatus === 'loading'}>↻</button>
        </div>
      )}

      <div className="sector-legend">
        {SECTOR_LIST.map((sector) => <span key={sector.id}><i style={{ background: sector.color }} />{sector.name}</span>)}
      </div>

    </div>
  );
}
