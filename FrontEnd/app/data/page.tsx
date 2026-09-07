import type { Metadata } from "next";
import RiskDashboard from "../risk-dashboard";

export const metadata: Metadata = {
  title: "데이터 현황 — COSMOS",
  description: "뉴스·공시·지식 그래프의 수집 상태와 분석 파이프라인을 확인합니다.",
};

export default function DataPage() {
  return <RiskDashboard initialView="data" />;
}
