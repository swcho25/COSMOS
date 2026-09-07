import type { Metadata } from "next";
import RiskDashboard from "../risk-dashboard";

export const metadata: Metadata = {
  title: "기업 데이터 — COSMOS",
  description: "뉴스와 공급망·지배구조 그래프에 연결된 기업별 데이터와 노출도를 조회합니다.",
};

export default function CompaniesPage() {
  return <RiskDashboard initialView="companies" />;
}
