import type { Metadata } from "next";
import GalaxyPrototype from "../galaxy-prototype/GalaxyClient";
import { getChatGPTUser } from "../chatgpt-auth";
import "../galaxy-prototype/galaxy.css";

export const metadata: Metadata = {
  title: "GALAXY — COSMOS",
  description: "실시간 금융 뉴스와 기업 관계를 3D 은하처럼 탐색합니다.",
};

export const dynamic = "force-dynamic";

export default async function NetworkPage() {
  const user = await getChatGPTUser();
  return <GalaxyPrototype authUser={user ? { displayName: user.displayName, email: user.email } : null} />;
}
