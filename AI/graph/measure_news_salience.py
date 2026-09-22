"""뉴스 KG 특징이 |잔차 수익률| 과 관계가 있는지 먼저 잰다. 모델은 그 다음이다.

  PYTHONUTF8=1 ../ner/.venv/Scripts/python.exe measure_news_salience.py

왜 크기(|수익률|)인가
────────────────────────────────────────────────────────────────────────────
프론트(lib/newsImpact.ts)는 relevance * |impact| 로 순위를 매긴다. 부호를 버린다.
그리고 방향 예측은 이미 여러 번 실패했다 (GNN 39.51% < 기준선 40.82%, 뉴스를 빼면
오히려 39.78% 로 올랐다). 크기는 아직 재본 적이 없고, relevance_score 의 정의에 맞다.

읽는 법
────────────────────────────────────────────────────────────────────────────
Spearman 은 단조 관계만 본다. 0 근처면 그 특징으로 relevance 를 만들 근거가 없다.
비교 기준은 '특징 없이 기업별 평균 |잔차| 만 쓴 것'이다. 이걸 못 이기면 뉴스 특징이
기여하는 게 없다는 뜻이다 — 기업마다 변동성이 다른 걸 맞힌 것뿐이다.

세션 정렬
────────────────────────────────────────────────────────────────────────────
publication_precision='date' 인 89.5% 는 시각이 없어 장중·장후를 못 가른다. 그래서
보수적으로 '다음 거래일' 에 붙인다. timestamp 인 10.5% 는 따로 떼어 같은 방식으로
재고, 두 결과가 크게 다르면 시각 부재가 결과를 만든 것이므로 신뢰하지 않는다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats

import impact_dataset as ds

FEATURES = ["n_sent", "first_pos", "n_comp", "is_pos", "is_neg", "is_neu"]


def load_news(path="data/news_features.csv") -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"ticker": str})
    df["sort_at"] = pd.to_datetime(df["sort_at"], utc=True, errors="coerce")
    df = df.dropna(subset=["sort_at"])
    df["is_pos"] = (df["sentiment"] == "POSITIVE").astype(int)
    df["is_neg"] = (df["sentiment"] == "NEGATIVE").astype(int)
    df["is_neu"] = (df["sentiment"] == "NEUTRAL").astype(int)
    return df


def attach_session(news: pd.DataFrame, days: pd.DatetimeIndex) -> pd.DataFrame:
    """기사를 '그 다음 거래일' 에 붙인다. 시각이 없는 기사가 89.5% 라 장중 배정은 못 한다.

    시간대는 양쪽 다 naive 로 맞춘다. sort_at 은 UTC 로 저장돼 있고 거래일 인덱스는
    날짜만 있으므로, 둘을 같은 축에 두지 않으면 비교 자체가 안 된다.
    """
    at = news["sort_at"].dt.tz_convert("UTC").dt.tz_localize(None).values
    idx = np.searchsorted(days.values, at, side="right")
    ok = idx < len(days)
    out = news[ok].copy()
    out["session"] = days[idx[ok]]
    return out


def main() -> None:
    panel = ds.build_panel()
    resid = panel.resid
    if resid.index.tz is not None:
        resid.index = resid.index.tz_localize(None)
    days = pd.DatetimeIndex(sorted(resid.index))

    news = load_news()
    news = attach_session(news, days)

    long = resid.stack().rename("r").reset_index()
    long.columns = ["session", "ticker", "r"]
    long["abs_r"] = long["r"].abs()

    df = news.merge(long, on=["session", "ticker"], how="inner")
    print(f"기사-기업 {len(news):,} -> 주가 붙은 것 {len(df):,}")
    if df.empty:
        print("겹치는 게 없다. 종목 코드나 날짜 정렬을 확인하라.")
        return

    tr0, tr1 = ds.SPLITS["train"]
    te0, te1 = ds.SPLITS["test"]
    s = df["session"].dt.tz_localize(None) if df["session"].dt.tz else df["session"]
    tr = df[(s >= tr0) & (s <= tr1)]
    te = df[(s >= te0) & (s <= te1)]
    print(f"train {len(tr):,}  test {len(te):,}")

    print("\n=== 특징별 Spearman (train, 대상 = |잔차 수익률|)")
    for f in FEATURES:
        if tr[f].nunique() < 2:
            continue
        rho, p = stats.spearmanr(tr[f], tr["abs_r"])
        flag = "" if abs(rho) >= 0.03 else "   <- 사실상 0"
        print(f"  {f:<10} rho {rho:+.4f}   p {p:.2e}{flag}")

    # 기준선: 뉴스 특징 없이 기업별 train 평균 |잔차| 만으로 test 를 맞힌다
    base = tr.groupby("ticker")["abs_r"].mean()
    te2 = te[te["ticker"].isin(base.index)].copy()
    te2["baseline"] = te2["ticker"].map(base)
    rho_b, _ = stats.spearmanr(te2["baseline"], te2["abs_r"])

    fit = tr[tr["ticker"].isin(base.index)].copy()
    X = np.column_stack([fit[f].values for f in FEATURES] +
                        [fit["ticker"].map(base).values])
    y = np.log1p(fit["abs_r"].values)
    keep = np.isfinite(X).all(1) & np.isfinite(y)
    coef, *_ = np.linalg.lstsq(np.column_stack([X[keep], np.ones(keep.sum())]), y[keep], rcond=None)
    Xt = np.column_stack([te2[f].values for f in FEATURES] + [te2["baseline"].values,
                                                              np.ones(len(te2))])
    pred = Xt @ coef
    rho_m, _ = stats.spearmanr(pred, te2["abs_r"])

    print(f"\n=== test rank-IC (n={len(te2):,})")
    print(f"  기업 평균만 (기준선)   {rho_b:+.4f}")
    print(f"  + 뉴스 특징            {rho_m:+.4f}")
    print(f"  차이                   {rho_m - rho_b:+.4f}")
    print("\n뉴스 특징이 기준선을 못 이기면 relevance 를 학습으로 만들 근거가 없다.")

    ts = df[df["prec"] == "timestamp"]
    if len(ts) > 500:
        rho_t, _ = stats.spearmanr(ts["n_sent"], ts["abs_r"])
        rho_d, _ = stats.spearmanr(df[df["prec"] == "date"]["n_sent"],
                                   df[df["prec"] == "date"]["abs_r"])
        print(f"\n시각 있는 표본만 n_sent rho {rho_t:+.4f} (n={len(ts):,}) / "
              f"날짜만 {rho_d:+.4f}")


if __name__ == "__main__":
    main()
