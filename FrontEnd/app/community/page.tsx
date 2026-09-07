import type { Metadata } from "next";
import Link from "next/link";
import { getChatGPTUser } from "../chatgpt-auth";
import PortalNav from "../portal-nav";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "커뮤니티 — COSMOS",
  description: "기업 관계와 금융 리스크 분석을 함께 토론하는 커뮤니티입니다.",
};

const discussions = [
  {
    id: "hbm-equipment-sensitivity",
    tag: "반도체",
    title: "HBM 공급망에서 가장 민감한 장비 기업은 어디일까요?",
    author: "orbit_660",
    time: "12분 전",
    replies: 18,
    views: 246,
    summary: "HBM 생산능력 확대가 전공정·후공정 장비 기업에 같은 강도로 전달되는지, 뉴스 관계와 실제 공급망 구조를 함께 놓고 살펴본 분석입니다.",
    body: [
      "HBM 관련 뉴스에서는 메모리 제조사와 GPU 기업이 가장 많이 언급되지만, 실제 병목은 TSV·본딩·검사 공정처럼 대체가 어려운 장비 구간에서 먼저 나타날 수 있습니다.",
      "관계 강도를 계산할 때 단순 동시 언급 빈도보다 공급 품목의 대체 가능성, 단일 고객 매출 비중, 증설 리드타임을 함께 반영해야 한다고 봅니다.",
      "현재 관계 지도에서는 직접 언급 관계와 공시 기반 공급 관계를 분리한 뒤, 두 근거가 겹치는 기업에 더 높은 신뢰도를 주는 방식이 적절해 보입니다.",
    ],
    companies: ["SK하이닉스", "삼성전자", "한미반도체", "테크윙"],
  },
  {
    id: "ev-demand-material-lag",
    tag: "배터리",
    title: "완성차 수요 둔화가 소재 기업까지 전이되는 시차",
    author: "credit_note",
    time: "38분 전",
    replies: 11,
    views: 184,
    summary: "전기차 판매 둔화가 셀 제조사와 양극재·전해액 기업의 실적에 반영되는 시간 차이를 공급계약 구조 관점에서 정리했습니다.",
    body: [
      "완성차 주문 감소가 발생해도 장기 공급계약과 안전재고 때문에 소재 기업 실적에는 즉시 반영되지 않을 수 있습니다.",
      "우선 셀 제조사의 가동률과 재고일수가 변하고, 이후 발주량 조정과 단가 재협상을 거쳐 소재 기업으로 충격이 전달되는 순서를 가정했습니다.",
      "뉴스 이벤트의 발생일만 보는 대신 계약 기간과 재고 회전일을 엣지 지연값으로 사용하면 전이 시점을 더 현실적으로 표현할 수 있습니다.",
    ],
    companies: ["LG에너지솔루션", "포스코퓨처엠", "에코프로비엠", "현대자동차"],
  },
  {
    id: "governance-edge-weight",
    tag: "지배구조",
    title: "계열사 지분 변화는 관계 강도에 어떻게 반영해야 할까요?",
    author: "graph_reader",
    time: "1시간 전",
    replies: 7,
    views: 129,
    summary: "지분율과 의결권, 동일인 지배 여부를 하나의 관계 점수로 합칠 때 필요한 기준을 논의합니다.",
    body: [
      "지분율 50% 초과는 명확한 지배 관계로 볼 수 있지만, 30% 안팎의 지분도 분산된 주주 구조에서는 실질 지배력이 발생할 수 있습니다.",
      "따라서 단순 지분율 외에 최대주주 여부, 특수관계인 합산 지분, 이사회 선임권을 별도 특성으로 저장하는 편이 안전합니다.",
      "화면에는 원본 지분율과 모델이 계산한 지배력 점수를 동시에 보여줘 사용자가 추론 결과를 검증할 수 있어야 합니다.",
    ],
    companies: ["현대자동차", "기아", "현대모비스", "현대글로비스"],
  },
  {
    id: "news-disclosure-confidence",
    tag: "데이터",
    title: "뉴스 추출 관계와 공시 관계의 신뢰도 기준 공유",
    author: "ner_lab",
    time: "2시간 전",
    replies: 23,
    views: 318,
    summary: "뉴스 NER·관계 추출 결과와 공시 기반 관계가 충돌할 때 어떤 근거를 우선해야 하는지 정리한 데이터 품질 토론입니다.",
    body: [
      "공시는 정확도가 높지만 갱신 속도가 느리고, 뉴스는 빠르지만 기업명 오인식과 추측성 문장이 섞일 수 있다는 차이가 있습니다.",
      "뉴스 관계는 문장 내 기업 역할과 출처 신뢰도, 반복 보도 수를 반영하고 공시는 보고서 종류와 기준일을 함께 저장하는 방식이 필요합니다.",
      "두 데이터가 일치하면 확정 관계로 승격하고, 충돌하면 사용자에게 근거 문장을 노출한 채 검토 대기 상태로 유지하는 방식을 제안합니다.",
    ],
    companies: ["삼성전자", "LG전자", "SK하이닉스", "NVIDIA"],
  },
];

export default async function CommunityPage() {
  const user = await getChatGPTUser();
  return (
    <main className="portal-page">
      <PortalNav active="community" user={user} />
      <div className="portal-content community-page-content">
        <section className="community-hero">
          <div><span>COMMUNITY BETA</span><h1>시장보다 먼저, 관계를 이야기합니다</h1><p>뉴스에서 발견한 기업 연결과 리스크 전이 경로를 함께 검토하고 분석 관점을 나눠보세요.</p></div>
          <a href={user ? "#new-post" : "/signin-with-chatgpt?return_to=%2Fcommunity%2F"}>새 글 작성</a>
        </section>
        <div className="community-list-view">
          <div className="community-layout">
            <section className="community-feed">
              <header><div><span>실시간 토론</span><h2>지금 주목받는 분석</h2></div><button type="button">최신순⌄</button></header>
              {discussions.map((item) => (
                <Link className="community-post-link" href={`/community/#${item.id}`} key={item.id}>
                  <article>
                    <span>{item.tag}</span><div><h3>{item.title}</h3><p>{item.author} · {item.time}</p></div><b>{item.replies}<small>댓글</small></b>
                  </article>
                </Link>
              ))}
            </section>
            <aside className="community-aside">
              <section><span>오늘의 토픽</span><h2>AI 반도체 공급망</h2><p>HBM·패키징·장비 기업을 잇는 관계 42개가 오늘 뉴스에서 관측됐습니다.</p><Link href="/?sector=semi">은하에서 보기 →</Link></section>
              <section><span>커뮤니티 원칙</span><ul><li>투자 권유가 아닌 분석 근거를 공유해 주세요.</li><li>기사·공시 원문 출처를 함께 남겨주세요.</li><li>개인정보와 확인되지 않은 소문은 금지됩니다.</li></ul></section>
            </aside>
          </div>
        </div>
        <div className="community-post-details">
          {discussions.map((selectedPost) => (
            <section className="community-post-view" id={selectedPost.id} key={selectedPost.id}>
              <Link className="community-post-back" href="/community/">← 커뮤니티 목록</Link>
              <header className="community-post-header">
                <span>{selectedPost.tag}</span>
                <h2>{selectedPost.title}</h2>
                <p>{selectedPost.author} · {selectedPost.time} · 조회 {selectedPost.views}</p>
              </header>
              <div className="community-post-layout">
                <article className="community-post-body">
                  <strong>{selectedPost.summary}</strong>
                  {selectedPost.body.map((paragraph) => <p key={paragraph}>{paragraph}</p>)}
                  <div className="community-company-tags">
                    <span>관련 기업</span>
                    <div>{selectedPost.companies.map((company) => <Link key={company} href={`/companies/?company=${encodeURIComponent(company)}`}>{company}</Link>)}</div>
                  </div>
                </article>
                <aside className="community-comment-preview">
                  <header><span>DISCUSSION</span><h3>댓글 {selectedPost.replies}</h3></header>
                  <div><b>signal_reader</b><p>관계 강도 산식에 반영할 수 있는 좋은 기준입니다. 공시 근거와 함께 비교해 보고 싶어요.</p></div>
                  <div><b>data_orbit</b><p>시차와 신뢰도를 분리해서 보여주면 분석 결과를 이해하기 더 쉬울 것 같습니다.</p></div>
                  <button type="button">댓글 작성은 로그인 후 제공됩니다</button>
                </aside>
              </div>
            </section>
          ))}
        </div>
      </div>
    </main>
  );
}
