import { useEffect } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import Header from "@/components/Header";
import Toasts from "@/components/Toasts";
import AuthDialog from "@/features/auth/AuthDialog";
import GalaxyPage from "@/features/galaxy/GalaxyPage";
import NewsPage from "@/features/news/NewsPage";
import CompaniesPage from "@/features/companies/CompaniesPage";
import MePage from "@/features/profile/MePage";
import { useSession } from "@/store/session";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 60_000, retry: 1, refetchOnWindowFocus: false },
  },
});

function SessionBoot() {
  const bootstrap = useSession((s) => s.bootstrap);
  const sessionKey = useSession((s) => s.sessionKey);
  useEffect(() => {
    void bootstrap();
  }, [bootstrap]);
  // 로그인 사용자가 바뀌면 개인화 응답(watched·scrapped·personalized)을 다시 받는다
  useEffect(() => {
    void queryClient.invalidateQueries();
  }, [sessionKey]);
  return null;
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <SessionBoot />
        <div className="app">
          <Header />
          <Routes>
            <Route path="/" element={<GalaxyPage />} />
            <Route path="/news" element={<NewsPage />} />
            <Route path="/companies" element={<CompaniesPage />} />
            <Route path="/me" element={<MePage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </div>
        <AuthDialog />
        <Toasts />
      </BrowserRouter>
    </QueryClientProvider>
  );
}
