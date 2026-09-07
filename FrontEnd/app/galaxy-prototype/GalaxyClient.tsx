"use client";

import { useEffect, useState, type ComponentType } from "react";
import Link from "next/link";

type AuthUser = { displayName: string; email: string } | null;

export default function GalaxyClient({ authUser = null }: { authUser?: AuthUser }) {
  const [Galaxy, setGalaxy] = useState<ComponentType<{ authUser?: AuthUser }> | null>(null);

  useEffect(() => {
    let active = true;
    void import("./App").then((module) => {
      if (active) setGalaxy(() => module.default);
    });
    return () => {
      active = false;
    };
  }, []);

  if (!Galaxy) {
    return (
      <>
        <main className="galaxy-loader" aria-label="3D 기업 관계 은하를 준비하고 있습니다">
          <span>NEWS × RISK NETWORK</span>
          <strong>GALAXY</strong>
          <i />
        </main>
        <div className="galaxy-ssr-contract">
          <Link className="brand-lockup" href="/" aria-label="코스모스 홈으로 이동">COSMOS</Link>
          <h1>뉴스 기반 기업 관계 지도</h1>
          <nav aria-label="주요 메뉴">
            <Link href="/">전체 은하</Link>
            <Link href="/news/">뉴스</Link>
            <Link href="/companies/">기업</Link>
            <Link href="/community/">커뮤니티</Link>
            <Link href="/mypage/">내 페이지</Link>
          </nav>
        </div>
      </>
    );
  }

  return <Galaxy authUser={authUser} />;
}
