/* eslint-disable @next/next/no-html-link-for-pages -- AWS 정적 호스팅에서는 RSC 전환 대신 전체 문서 이동이 필요합니다. */
import type { ChatGPTUser } from "./chatgpt-auth";

const navItems = [
  { id: "home", label: "전체 은하", href: "/" },
  { id: "news", label: "뉴스", href: "/news/" },
  { id: "companies", label: "기업", href: "/companies/" },
  { id: "community", label: "커뮤니티", href: "/community/" },
  { id: "mypage", label: "내 페이지", href: "/mypage/" },
];

export default function PortalNav({ active, user }: { active: string; user?: ChatGPTUser | null }) {
  return (
    <header className="portal-nav">
      <a href="/" className="portal-brand" aria-label="COSMOS 전체 은하 홈"><span aria-hidden="true"><i /><i /></span><b>COSMOS</b></a>
      <nav aria-label="주요 메뉴">
        {navItems.map((item) => <a key={item.id} href={item.href} className={active === item.id ? "active" : ""} aria-current={active === item.id ? "page" : undefined}>{item.label}</a>)}
      </nav>
      <div className="portal-account">
        {user ? (
          <><a href="/mypage/" className="portal-user"><i>{user.displayName.slice(0, 1)}</i><span>{user.displayName}</span></a><a href="/signout-with-chatgpt?return_to=%2F" className="portal-login">로그아웃</a></>
        ) : (
          <><a href="/signin-with-chatgpt?return_to=%2F" className="portal-login">로그인</a><a href="/signin-with-chatgpt?return_to=%2F" className="portal-signup">회원가입</a></>
        )}
      </div>
    </header>
  );
}
