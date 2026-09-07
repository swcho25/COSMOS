import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  metadataBase: new URL(process.env.NEXT_PUBLIC_SITE_URL ?? "https://nexus-risk-intelligence.auddnjs0708.chatgpt.site"),
  title: "COSMOS — 공급망 리스크 인텔리전스",
  description: "공급망·지배구조 그래프로 기업 충격 전이 경로와 영향도를 분석합니다.",
  openGraph: {
    title: "COSMOS — 공급망 리스크 인텔리전스",
    description: "공급망·지배구조 그래프로 기업 충격 전이 경로와 영향도를 분석합니다.",
    images: [{ url: "/og.png", width: 1727, height: 910, alt: "COSMOS Supply Chain Intelligence" }],
    type: "website",
  },
  twitter: {
    card: "summary_large_image",
    title: "COSMOS — 공급망 리스크 인텔리전스",
    description: "공급망·지배구조 그래프로 기업 충격 전이 경로와 영향도를 분석합니다.",
    images: ["/og.png"],
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="ko">
      <body>{children}</body>
    </html>
  );
}
