import type { Metadata } from "next";
import GalaxyPrototype from "./galaxy-prototype/GalaxyClient";
import { getChatGPTUser } from "./chatgpt-auth";
import "./galaxy-prototype/galaxy.css";

export const metadata: Metadata = {
  title: "COSMOS — 공급망 리스크 인텔리전스",
  description: "기업 간 공급망·지분 관계를 분석해 충격 전이 경로와 영향도를 탐색합니다.",
};

export const dynamic = "force-dynamic";
export const revalidate = 0;

export default async function Home() {
  const user = await getChatGPTUser();
  return <GalaxyPrototype authUser={user ? { displayName: user.displayName, email: user.email } : null} />;
}
