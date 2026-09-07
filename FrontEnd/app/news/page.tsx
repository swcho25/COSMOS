import type { Metadata } from "next";
import RiskDashboard from "../risk-dashboard";

export const metadata: Metadata = {
  title: "실시간 뉴스 — COSMOS",
  description: "NAVER API HUB에서 수집한 최신 금융 뉴스와 기업 영향을 확인합니다.",
};

export const dynamic = "force-dynamic";
export const revalidate = 0;

export default function NewsPage() {
  return <RiskDashboard initialView="news" />;
}
