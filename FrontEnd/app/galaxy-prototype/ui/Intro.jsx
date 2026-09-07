import { useGalaxy } from '../store/useGalaxy';
import { COMPANIES, N_DAYS, dateLabel } from '../data/universe';

export default function Intro() {
  const begin = useGalaxy((s) => s.begin);
  const liveFeed = useGalaxy((s) => s.liveFeed);
  const feedStatus = useGalaxy((s) => s.feedStatus);
  const companyCount = new Set(liveFeed?.items?.flatMap((news) => news.affected.map((company) => company.nodeId)) ?? []).size;
  return (
    <div className="intro">
      <div className="intro-box">
        <div className="kicker">Live News × Risk Propagation Explorer</div>
        <h1>GALAXY</h1>
        <h2>실제 뉴스 속 기업 관계를 차원 이동하듯 탐색한다</h2>
        <p>
          네이버 API와 저장 DB의 최신 기사에서 <b>직접 언급·추출 관계·간접 전이</b>를 분리해
          기업 사이의 중력으로 그립니다. 항성을 선택하면 해당 기업 차원으로 워프해 어떤 기사와
          관계가 충격을 만들었는지 확인할 수 있습니다.
        </p>
        <button className="intro-cta" onClick={begin}>
          탐사 시작
        </button>
        <div className="intro-note">
          {liveFeed
            ? `${liveFeed.provider} · 뉴스 ${liveFeed.items.length}건 · 연결 기업 ${companyCount}개 · ${liveFeed.persistence?.enabled ? 'DB 저장 연동' : '실시간 피드'}`
            : feedStatus === 'loading'
              ? 'DB 뉴스와 최신 금융 뉴스를 동기화하고 있습니다'
              : `연결 대기 · 기본 탐사 데이터 ${COMPANIES.length}개 종목 · ${dateLabel(0)} ~ ${dateLabel(N_DAYS - 1)}`}
        </div>
      </div>
    </div>
  );
}
