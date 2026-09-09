import { useEffect, useState, type FormEvent } from "react";
import { ApiError, api } from "@/api";
import { useSession } from "@/store/session";
import { useUi } from "@/store/ui";
import { Icon } from "@/components/ui";

/** 로그인 / 회원가입 모달. 화면 이동 없이 은하 위에서 처리한다. */
export default function AuthDialog() {
  const kind = useUi((s) => s.authDialog);
  const close = useUi((s) => s.closeAuth);
  useEffect(() => {
    if (!kind) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && close();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [kind, close]);
  if (!kind) return null;
  return (
    <div className="scrim" onMouseDown={(e) => e.target === e.currentTarget && close()} role="presentation">
      <div className="card dialog" role="dialog" aria-modal="true" aria-labelledby="auth-title">
        <div className="row between">
          <span className="kick">{kind === "login" ? "sign in" : "create account"}</span>
          <button type="button" className="btn btn-g btn-xs" onClick={close} aria-label="닫기">
            <Icon.Close />
          </button>
        </div>
        {kind === "login" ? <LoginForm /> : <SignupForm />}
      </div>
    </div>
  );
}

function fieldError(err: unknown, field: string) {
  if (err instanceof ApiError) return err.fieldErrors.find((f) => f.field === field)?.message ?? null;
  return null;
}
function formError(err: unknown) {
  if (!err) return null;
  if (err instanceof ApiError) return err.fieldErrors.length ? null : err.message;
  return "요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.";
}

function LoginForm() {
  const login = useSession((s) => s.login);
  const openAuth = useUi((s) => s.openAuth);
  const closeAuth = useUi((s) => s.closeAuth);
  const toast = useUi((s) => s.toast);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      await login(email.trim(), password);
      closeAuth();
      toast("로그인했습니다. 관심 기업과 가중치가 반영됩니다.", "success");
    } catch (x) {
      setErr(x);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} noValidate>
      <h2 id="auth-title">다시 만나서 반가워요</h2>
      <p className="hint">로그인하면 관심 기업, 뉴스 스크랩, 개인화 가중치를 사용할 수 있습니다.</p>
      <div className="form">
        <label className="field">
          <span className="sr-only">이메일</span>
          <input type="email" placeholder="이메일" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" autoFocus />
        </label>
        <label className="field">
          <span className="sr-only">비밀번호</span>
          <input type="password" placeholder="비밀번호" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />
        </label>
        {formError(err) && <div className="form-err">{formError(err)}</div>}
        <button type="submit" className="btn btn-p" disabled={busy || !email || !password} style={{ padding: 12 }}>
          {busy ? "확인 중…" : "로그인"}
        </button>
        <div className="hint" style={{ textAlign: "center" }}>
          아직 계정이 없나요?{" "}
          <button type="button" className="link-btn" onClick={() => openAuth("signup")}>
            회원가입
          </button>
        </div>
        <div className="hint" style={{ borderTop: "1px solid var(--border)", paddingTop: 12 }}>
          체험 계정 · <code>demo@cosmos.dev</code> / <code>cosmos123</code>
        </div>
      </div>
    </form>
  );
}

function SignupForm() {
  const signup = useSession((s) => s.signup);
  const openAuth = useUi((s) => s.openAuth);
  const closeAuth = useUi((s) => s.closeAuth);
  const toast = useUi((s) => s.toast);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [nickname, setNickname] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  const [avail, setAvail] = useState<{ email?: boolean; nickname?: boolean }>({});

  // 중복 확인 API 를 입력 후 잠시 뒤 호출한다
  useEffect(() => {
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) return;
    const t = setTimeout(() => api.auth.checkEmail(email).then((r) => setAvail((a) => ({ ...a, email: r.available }))).catch(() => undefined), 400);
    return () => clearTimeout(t);
  }, [email]);
  useEffect(() => {
    if (nickname.trim().length < 2) return;
    const t = setTimeout(() => api.auth.checkNickname(nickname.trim()).then((r) => setAvail((a) => ({ ...a, nickname: r.available }))).catch(() => undefined), 400);
    return () => clearTimeout(t);
  }, [nickname]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      await signup(email.trim(), password, nickname.trim());
      closeAuth();
      toast(`환영합니다, ${nickname.trim()}님.`, "success");
    } catch (x) {
      setErr(x);
    } finally {
      setBusy(false);
    }
  };

  const pwOk = /^(?=.*[A-Za-z])(?=.*\d).{8,64}$/.test(password);

  return (
    <form onSubmit={submit} noValidate>
      <h2 id="auth-title">탐사대에 합류하세요</h2>
      <p className="hint">이메일과 닉네임으로 가입합니다. 비밀번호는 8~64자, 영문과 숫자를 각 1자 이상 포함합니다.</p>
      <div className="form">
        <label className="field">
          <span className="sr-only">이메일</span>
          <input type="email" placeholder="이메일" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" autoFocus />
          {avail.email !== undefined && <span className="meta" style={{ color: avail.email ? "var(--pos-fg)" : "var(--neg-fg)" }}>{avail.email ? "사용 가능" : "사용 중"}</span>}
        </label>
        {fieldError(err, "email") && <div className="field-err">{fieldError(err, "email")}</div>}
        <label className="field">
          <span className="sr-only">닉네임</span>
          <input type="text" placeholder="닉네임 (2~30자)" value={nickname} onChange={(e) => setNickname(e.target.value)} autoComplete="nickname" maxLength={30} />
          {avail.nickname !== undefined && <span className="meta" style={{ color: avail.nickname ? "var(--pos-fg)" : "var(--neg-fg)" }}>{avail.nickname ? "사용 가능" : "사용 중"}</span>}
        </label>
        {fieldError(err, "nickname") && <div className="field-err">{fieldError(err, "nickname")}</div>}
        <label className="field">
          <span className="sr-only">비밀번호</span>
          <input type="password" placeholder="비밀번호" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" />
          {password && <span className="meta" style={{ color: pwOk ? "var(--pos-fg)" : "var(--text-4)" }}>{pwOk ? "규칙 충족" : "8자+ 영문·숫자"}</span>}
        </label>
        {fieldError(err, "password") && <div className="field-err">{fieldError(err, "password")}</div>}
        {formError(err) && <div className="form-err">{formError(err)}</div>}
        <button type="submit" className="btn btn-p" disabled={busy || !email || !pwOk || nickname.trim().length < 2 || avail.email === false || avail.nickname === false} style={{ padding: 12 }}>
          {busy ? "가입 중…" : "가입하고 시작하기"}
        </button>
        <div className="hint" style={{ textAlign: "center" }}>
          이미 계정이 있나요?{" "}
          <button type="button" className="link-btn" onClick={() => openAuth("login")}>
            로그인
          </button>
        </div>
      </div>
    </form>
  );
}
