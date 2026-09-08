import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { ApiError } from "@/api";
import { fmtDateTime, fmtRelative } from "@/lib/format";
import { useScraps, useToggleScrap, useToggleWatch, useUpdateNickname, useUpdateWeight, useWatchlist, useWeightSetting } from "@/lib/queries";
import { useSession } from "@/store/session";
import { useUi } from "@/store/ui";
import { CompanyAvatar, Empty, Icon, Skeleton } from "@/components/ui";
import "./profile.css";

/** 내 페이지 — 프로필 메뉴 진입점. 내 정보 · 관심 기업 · 스크랩 · 분석 가중치 */
export default function MePage() {
  const status = useSession((s) => s.status);
  const user = useSession((s) => s.user);
  const openAuth = useUi((s) => s.openAuth);
  const navigate = useNavigate();
  const { hash } = useLocation();

  useEffect(() => {
    if (status === "guest") {
      openAuth("login");
      navigate("/", { replace: true });
    }
  }, [status, openAuth, navigate]);
  useEffect(() => {
    if (!hash) return;
    const el = document.querySelector(hash);
    if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [hash, status]);

  if (status !== "authed" || !user) return <div className="page">{status === "booting" && <Skeleton h={200} />}</div>;

  return (
    <div className="page me">
      <div className="page-head">
        <div>
          <span className="pill">my page</span>
          <h1>{user.nickname}</h1>
          <p>{user.email}</p>
        </div>
      </div>
      <div className="me-grid">
        <ProfileCard />
        <WeightCard />
        <WatchCard />
        <ScrapCard />
      </div>
    </div>
  );
}

function ProfileCard() {
  const user = useSession((s) => s.user)!;
  const [nick, setNick] = useState(user.nickname);
  const update = useUpdateNickname();
  const toast = useUi((s) => s.toast);
  const err = update.error instanceof ApiError ? update.error.message : null;
  return (
    <section className="card me-card" id="profile">
      <div className="kick">profile</div>
      <h2 className="sect" style={{ fontSize: 20, marginTop: 6 }}>
        내 정보
      </h2>
      <div className="lab mt-16">이메일</div>
      <div className="mt-8">{user.email}</div>
      <div className="lab mt-16">닉네임</div>
      <div className="row mt-8" style={{ gap: 8 }}>
        <label className="field grow">
          <input value={nick} onChange={(e) => setNick(e.target.value)} maxLength={30} aria-label="닉네임" />
        </label>
        <button type="button" className="btn btn-p" disabled={nick.trim().length < 2 || nick === user.nickname || update.isPending} onClick={() => update.mutate(nick.trim(), { onSuccess: () => toast("닉네임을 변경했습니다.", "success") })}>
          저장
        </button>
      </div>
      {err && <div className="field-err mt-8">{err}</div>}
      <div className="hint mt-12">닉네임은 2~30자이며 다른 사용자와 중복될 수 없습니다. 커뮤니티 댓글에 표시됩니다.</div>
    </section>
  );
}

function WeightCard() {
  const weight = useWeightSetting();
  const update = useUpdateWeight();
  const toast = useUi((s) => s.toast);
  const [v, setV] = useState<number | null>(null);
  const news = v ?? weight.data?.newsWeight ?? 50;
  return (
    <section className="card me-card" id="weight">
      <div className="kick">personalization</div>
      <h2 className="sect" style={{ fontSize: 20, marginTop: 6 }}>
        분석 가중치
      </h2>
      <p className="hint mt-8">관계 점수를 계산할 때 뉴스와 공시를 어떤 비율로 반영할지 정합니다. 저장하면 다음 그래프 조회부터 개인화 점수가 적용됩니다.</p>
      <div className="weight-bar mt-16">
        <div className="row between">
          <span>
            뉴스 <b className="num">{news}</b>
          </span>
          <span>
            공시 <b className="num">{100 - news}</b>
          </span>
        </div>
        <input type="range" min={0} max={100} step={5} value={news} onChange={(e) => setV(Number(e.target.value))} aria-label="뉴스 가중치" />
        <div className="bar" style={{ height: 8 }}>
          <i style={{ width: `${news}%` }} />
        </div>
      </div>
      <div className="row between mt-16">
        <span className="meta num">{weight.data?.updatedAt ? `마지막 변경 ${fmtDateTime(weight.data.updatedAt)}` : "저장된 설정 없음 · 기본 50 / 50"}</span>
        <button type="button" className="btn btn-p btn-sm" disabled={v === null || update.isPending} onClick={() => update.mutate(news, { onSuccess: () => (setV(null), toast("가중치를 저장했습니다. 그래프를 다시 계산합니다.", "success")) })}>
          저장
        </button>
      </div>
    </section>
  );
}

function WatchCard() {
  const watch = useWatchlist();
  const toggle = useToggleWatch();
  const navigate = useNavigate();
  const items = watch.data?.items ?? [];
  return (
    <section className="card me-card" id="watch">
      <div className="row between">
        <div>
          <div className="kick">watchlist</div>
          <h2 className="sect" style={{ fontSize: 20, marginTop: 6 }}>
            관심 기업 <span className="num" style={{ color: "var(--primary-2)" }}>{items.length}</span>
          </h2>
        </div>
      </div>
      <div className="stack mt-16">
        {watch.isLoading && <Skeleton h={60} />}
        {!watch.isLoading && !items.length && <Empty>은하에서 기업을 선택하고 ★ 관심 등록을 눌러보세요.</Empty>}
        {items.map((w) => (
          <div key={w.companyId} className="me-item">
            <CompanyAvatar name={w.name} stockCode={w.stockCode} industry={w.primaryIndustry.name} size={40} />
            <button type="button" className="grow" style={{ textAlign: "left" }} onClick={() => navigate(`/?company=${w.companyId}`)}>
              <b>{w.name}</b>
              <span className="meta num">
                {w.stockCode} · {w.primaryIndustry.name} · {fmtRelative(w.watchedAt)} 등록
              </span>
            </button>
            <button type="button" className="btn btn-g btn-xs" onClick={() => toggle.mutate({ companyId: w.companyId, watched: true })} aria-label="관심 해제">
              <Icon.Close />
            </button>
          </div>
        ))}
      </div>
    </section>
  );
}

function ScrapCard() {
  const scraps = useScraps();
  const toggle = useToggleScrap();
  const items = scraps.data?.items ?? [];
  return (
    <section className="card me-card" id="scraps">
      <div className="kick">scraps</div>
      <h2 className="sect" style={{ fontSize: 20, marginTop: 6 }}>
        스크랩한 뉴스 <span className="num" style={{ color: "var(--primary-2)" }}>{items.length}</span>
      </h2>
      <div className="stack mt-16">
        {scraps.isLoading && <Skeleton h={60} />}
        {!scraps.isLoading && !items.length && <Empty>뉴스 카드의 북마크 버튼으로 기사를 저장할 수 있습니다.</Empty>}
        {items.map((s) => (
          <div key={s.newsId} className="me-item" style={{ alignItems: "flex-start" }}>
            <div className="grow">
              <div style={{ fontSize: 15, fontWeight: 600, lineHeight: 1.45 }}>{s.title}</div>
              <div className="meta num mt-8">
                {s.publisher} · {fmtDateTime(s.publishedAt)} · {fmtRelative(s.scrappedAt)} 저장
              </div>
            </div>
            <a className="btn btn-g btn-xs" href={s.originalUrl} target="_blank" rel="noreferrer" aria-label="원문 보기">
              <Icon.External />
            </a>
            <button type="button" className="btn btn-g btn-xs" onClick={() => toggle.mutate({ newsId: s.newsId, scrapped: true })} aria-label="스크랩 해제">
              <Icon.Close />
            </button>
          </div>
        ))}
      </div>
    </section>
  );
}
