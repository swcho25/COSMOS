import type { Metadata } from "next";
import Link from "next/link";
import { getChatGPTUser } from "../chatgpt-auth";
import PortalNav from "../portal-nav";

export const metadata: Metadata = {
  title: "내 페이지 — COSMOS",
  description: "관심 기업과 리스크 알림을 관리합니다.",
};

export const dynamic = "force-dynamic";

export default async function MyPage() {
  const user = await getChatGPTUser();
  const displayName = user?.displayName ?? "게스트";
  return (
    <main className="portal-page">
      <PortalNav active="mypage" user={user} />
      <div className="portal-content mypage-content">
        <section className="profile-hero">
          <div className="profile-avatar">{displayName.slice(0, 1)}</div>
          <div><span>MY INTELLIGENCE</span><h1>{displayName}</h1><p>{user?.email ?? "로그인하면 관심 기업·리스크 알림·저장한 관계 지도를 동기화할 수 있습니다."}</p></div>
          <a href={user ? "/signout-with-chatgpt?return_to=%2F" : "/signin-with-chatgpt?return_to=%2Fmypage%2F"}>{user ? "로그아웃" : "로그인"}</a>
        </section>
        <section className="mypage-grid">
          <article><span>관심 기업</span><strong>0</strong><p>관심 기업을 추가하면 뉴스와 관계 변화를 빠르게 확인할 수 있습니다.</p><Link href="/companies/">기업 찾기 →</Link></article>
          <article><span>리스크 알림</span><strong>0</strong><p>공급 차질·계약 종료·지배구조 변화 알림을 이곳에서 관리합니다.</p><button type="button">알림 설정</button></article>
          <article><span>저장한 관계 지도</span><strong>0</strong><p>중요한 기업 간 연결과 근거 뉴스를 저장해 다시 확인할 수 있습니다.</p><Link href="/">전체 은하 열기 →</Link></article>
        </section>
      </div>
    </main>
  );
}
