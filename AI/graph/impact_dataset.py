"""Story 3 공용 데이터 계층 — 당일 영향도(nowcast) 학습·평가용 패널.

event_study_propagation.py 의 판정 결과로 목표가 바뀌었다.
  (X) 다음 세션 수익률 예측 — 순전파가 0이라 애초에 넘을 베이스라인이 없다.
  (O) **당일 영향도**: A 의 비정상 수익률이 관측됐을 때 같은 세션에 누가 얼마나 움직였나를
      순위로 맞춘다. 이건 예측이 아니라 귀속(attribution)이다. 수익을 내는 신호가 아니라
      "이 뉴스로 어디가 흔들렸나"를 보여주는 화면용 지표다. 이 구분을 흐리면 안 된다.

이 모듈이 책임지는 것은 세 가지다.
  1) 잔차 패널 — 미래정보 없이 만든 비정상 수익률
  2) 시간 분할 — train / val / test 를 날짜로 자르고, 각 구간에서 쓸 수 있는 엣지만 남긴다
  3) 세션 정렬 — KOSPI/NASDAQ 의 서로 다른 달력을 한 패널로 붙인다

────────────────────────────────────────────────────────────────────────────
베타 추정을 왜 직전 연도로 하는가
────────────────────────────────────────────────────────────────────────────
build_correlation_edges.residualize() 는 주어진 구간 전체로 베타를 한 번에 추정한다.
엣지를 만들 때는 문제가 없다(그 구간의 관계를 기술하는 게 목적이므로). 하지만 학습/평가
라벨로 쓰면 그 해의 마지막 날 잔차가 그 해 전체로 추정한 베타에 의존하게 된다 — 미래정보다.
그래서 여기서는 **팩터(동일가중 LOO 평균)는 당일 것을 쓰되 베타는 직전 연도에서 추정**한다.
팩터 자체는 그날 관측되는 값이라 미래정보가 아니고, 추정 파라미터는 베타뿐이기 때문이다.

  beta_mode="prior"  직전 연도 베타 (기본, 평가에 쓸 것)
  beta_mode="inyear" 당해 연도 베타 (build_correlation_edges 와 동일, 비교용)

첫 해(2016)는 직전 연도가 없어 버린다. 실측 차이는 measure_beta_mode() 로 확인할 것.

────────────────────────────────────────────────────────────────────────────
엣지 as-of 필터
────────────────────────────────────────────────────────────────────────────
관계 엣지는 2026-09 시점의 스냅샷 하나다. 이걸 그대로 2016년에 적용하면 아직 생기지도 않은
관계로 과거를 설명하게 된다. edges_relation_scored.csv 의 first_date(그 관계가 기사에서 처음
관측된 날)로 거른다. 273개 중 first_date 분포는
  ~2023: 256개 | 2024: 12 | 2025: 3 | 2026: 2
라 test 시작을 2024-01-01 로 잡으면 17개만 빠진다.
지분(ownership/INVEST)은 DART 최대주주 현황이라 first_date 가 없다. 지배구조는 연 단위로
거의 안 변하므로 전 구간 유효한 것으로 둔다 — 한계로 남겨둔다.

correlation 엣지는 2025-09~2026-09 가격으로 만든 것이라 test 구간과 겹친다. 평가에 쓰려면
학습 구간 가격으로 다시 만들어야 하므로 여기서는 기본 제외한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from build_correlation_edges import MIN_FIT, MIN_GROUP, log_returns

HERE = Path(__file__).resolve().parent
PRICES = HERE / "data" / "prices_daily.parquet"
INDUSTRY = HERE.parent / "ner" / "data" / "company_industry.csv"
RELATIONS = HERE / "data" / "db" / "company_relationship.csv"
SCORES = HERE / "data" / "db" / "relationship_score_current.csv"
REL_SCORED = HERE / "data" / "edges_relation_scored.csv"

# 시간 분할. test 는 엣지 first_date 분포를 보고 2024-01-01 로 잡았다 (위 참고).
SPLITS = {"train": ("2017-01-01", "2022-12-31"),
          "val": ("2023-01-01", "2023-12-31"),
          "test": ("2024-01-01", "2026-09-11")}


def _loo_factor(block: pd.DataFrame) -> pd.DataFrame:
    """그룹 동일가중 평균에서 자기 자신을 뺀 것. residualize() 와 같은 정의."""
    tot, cnt = block.sum(axis=1, skipna=True), block.count(axis=1)
    out = {}
    for c in block.columns:
        x = block[c]
        out[c] = (tot - x.fillna(0.0)) / (cnt - x.notna().astype(int)).replace(0, np.nan)
    return pd.DataFrame(out, index=block.index)


def residualize_oos(frame: pd.DataFrame, groups: dict[object, list[str]],
                    fit_mask: pd.Series) -> pd.DataFrame:
    """fit_mask 구간에서 베타를 추정해 frame 전체에 적용한 잔차.

    팩터는 frame 의 당일 값을 쓴다(관측값이라 미래정보가 아니다). 추정되는 건 베타뿐이고
    그것만 과거 구간에서 온다.
    """
    out = frame.copy()
    for _, cols in sorted(groups.items(), key=lambda kv: str(kv[0])):
        cols = [c for c in cols if c in frame.columns]
        if len(cols) < MIN_GROUP:
            continue
        block = frame[cols]
        loo = _loo_factor(block)
        for c in cols:
            d = pd.concat([block[c], loo[c]], axis=1)[fit_mask].dropna()
            if len(d) < MIN_FIT or d.iloc[:, 1].var() == 0:
                out[c] = np.nan          # 추정 구간에 표본이 없는 종목은 통째로 버린다
                continue
            beta = d.iloc[:, 0].cov(d.iloc[:, 1]) / d.iloc[:, 1].var()
            out[c] = block[c] - beta * loo[c]
    return out


@dataclass
class Panel:
    """잔차 패널 + 시장/세션 인덱스. 학습 스크립트가 이것만 받아 쓴다."""
    resid: pd.DataFrame                 # index=거래일, columns=ticker
    market: pd.Series
    industry: pd.Series
    sess: dict[str, np.ndarray]         # 시장 -> resid 행 인덱스
    split: pd.Series                    # 거래일 -> train|val|test|None

    @property
    def tickers(self) -> list[str]:
        return list(self.resid.columns)

    def rows(self, name: str, mkt: str) -> np.ndarray:
        """그 분할 x 그 시장의 세션 행 인덱스."""
        want = self.split.to_numpy() == name
        return np.array([i for i in self.sess[mkt] if want[i]])

    def cols(self, mkt: str) -> np.ndarray:
        pos = {t: i for i, t in enumerate(self.tickers)}
        return np.array([pos[t] for t in self.tickers if self.market[t] == mkt])


def build_panel(prices: Path = PRICES, industry: Path = INDUSTRY, winsor: float = 0.005,
                beta_mode: str = "prior", factors: str = "market+industry") -> Panel:
    df = pd.read_parquet(prices, columns=["ticker", "market", "trading_at", "adj_close"])
    market = df.drop_duplicates("ticker").set_index("ticker")["market"]
    ind = pd.read_csv(industry, dtype=str).set_index("ticker")["industry_id"]

    px = df.pivot_table(index="trading_at", columns="ticker", values="adj_close")
    px.index = pd.to_datetime(px.index)
    r = log_returns(px.sort_index(), winsor)

    by_market: dict[object, list[str]] = {}
    by_industry: dict[object, list[str]] = {}
    for c in r.columns:
        by_market.setdefault(market[c], []).append(c)
        by_industry.setdefault((market[c], ind.get(c, "NA")), []).append(c)

    years = sorted(r.index.year.unique())
    parts = []
    for y in years:
        apply_mask = r.index.year == y
        fit_mask = pd.Series(r.index.year == (y - 1 if beta_mode == "prior" else y), r.index)
        if not fit_mask.any():
            continue                      # 직전 연도가 없는 첫 해는 버린다
        x = r if factors == "none" else residualize_oos(r, by_market, fit_mask)
        if factors == "market+industry":
            x = residualize_oos(x, by_industry, fit_mask)
        parts.append(x[apply_mask])
    resid = pd.concat(parts).sort_index()

    R = resid.to_numpy(dtype=float)
    pos = {t: i for i, t in enumerate(resid.columns)}
    cols = {m: np.array([pos[t] for t in resid.columns if market[t] == m])
            for m in ("KOSPI", "NASDAQ")}
    sess = {m: np.flatnonzero(np.isfinite(R[:, cols[m]]).any(axis=1)) for m in cols}

    split = pd.Series(None, index=resid.index, dtype=object)
    for name, (a, b) in SPLITS.items():
        split[(resid.index >= pd.Timestamp(a)) & (resid.index <= pd.Timestamp(b))] = name
    return Panel(resid=resid, market=market, industry=ind, sess=sess, split=split)


def load_relation_edges(relations: Path = RELATIONS, scores: Path = SCORES,
                        rel_scored: Path = REL_SCORED, as_of: str | None = None,
                        undirected: frozenset = frozenset({"PARTNER", "COMPETE"})) -> pd.DataFrame:
    """DB 시드 관계를 방향 있는 행으로 펴고, as_of 이전에 근거가 생긴 것만 남긴다.

    as_of 를 주면 edges_relation_scored.csv 의 first_date >= as_of 인 관계를 버린다.
    INVEST(지분)는 first_date 가 없으므로 항상 남긴다 — 위 docstring 의 한계 참고.
    """
    rel = pd.read_csv(relations, dtype=str)
    sc = pd.read_csv(scores, dtype=str)
    key = ["source_market", "source_stock_code", "target_market", "target_stock_code",
           "relationship_type_code"]
    rel = rel.merge(sc[key + ["impact_direction", "score", "confidence"]], on=key, how="left")

    if as_of and rel_scored.exists():
        rs = pd.read_csv(rel_scored, dtype={"src_ticker": str, "dst_ticker": str})
        first = {}
        for v in rs.itertuples(index=False):
            k = frozenset((v.src_ticker, v.dst_ticker)), v.rel_type
            first[k] = min(first.get(k, v.first_date), v.first_date)
        cut = pd.Timestamp(as_of)
        ok = []
        for v in rel.itertuples(index=False):
            k = (frozenset((v.source_stock_code, v.target_stock_code)), v.relationship_type_code)
            f = first.get(k)
            ok.append(f is None or pd.Timestamp(f) < cut)
        rel = rel[np.array(ok)]

    rows = []
    for v in rel.itertuples(index=False):
        base = dict(rel_type=v.relationship_type_code, impact=v.impact_direction,
                    score=float(v.score), confidence=float(v.confidence))
        rows.append(dict(src=v.source_stock_code, dst=v.target_stock_code,
                         orientation="forward", **base))
        rows.append(dict(src=v.target_stock_code, dst=v.source_stock_code,
                         orientation="forward" if v.relationship_type_code in undirected
                         else "reverse", **base))
    return pd.DataFrame(rows).drop_duplicates(["src", "dst", "rel_type", "orientation"])


def measure_beta_mode() -> None:
    """베타를 직전 연도로 바꾸면 잔차가 얼마나 달라지는지 실측. 추정하지 말고 재라."""
    a = build_panel(beta_mode="inyear").resid
    b = build_panel(beta_mode="prior").resid
    idx = a.index.intersection(b.index)
    x, y = a.loc[idx], b.loc[idx]
    both = x.notna() & y.notna()
    corr = pd.Series({c: x[c][both[c]].corr(y[c][both[c]]) for c in x.columns}).dropna()
    print(f"공통 거래일 {len(idx)} | 종목별 두 잔차의 상관: "
          f"중앙값 {corr.median():.4f} 최소 {corr.min():.4f} (1.0 이면 차이 없음)")
    print(f"표준편차 비 (prior/inyear) 중앙값 "
          f"{(y.std() / x.std()).median():.4f}")


if __name__ == "__main__":
    measure_beta_mode()
