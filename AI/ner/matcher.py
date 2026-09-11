"""Dictionary-based company mention matcher (Aho-Corasick + boundary rules + disambiguation).

    from matcher import CompanyMatcher
    m = CompanyMatcher.from_csv("data/aliases.csv")
    result = m.match(title, body)
    result.mentions      # list[Mention] with span, ticker, alias, method, confidence
    result.by_ticker()   # aggregated rows: ticker, n_mentions, first_pos, confidence, method
    result.unresolved    # group-name hits that could not be attributed

Rules (in order):
  1. Aho-Corasick finds every alias occurrence (case-sensitive).
  2. Boundary filter
     - left : previous char must not be Hangul or ASCII alnum        ("SKT" must not yield KT)
     - right: next char must not be ASCII alnum                      ("KTX" must not yield KT)
              next Hangul is allowed only when it starts a particle   ("삼성전자는" ok, "하이브리드" no)
     - ticker_only aliases need "(", "$", ":" before or ")" after     ("삼성전자(005930)")
  3. Leftmost-longest overlap resolution. Blockers win their span and emit nothing
     ("삼성전자우", "한화 이글스", "SK온").
  4. Group aliases (needs_context): dropped when a member company is present in the same
     document, otherwise emitted with the default ticker at low confidence, or reported as unresolved.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

try:  # fast C backend on dev machines; the Spark cluster has no pip, so fall back to a regex scanner
    import ahocorasick
except ImportError:  # pragma: no cover
    ahocorasick = None


class _RegexScanner:
    """Dependency-free stand-in for ahocorasick.Automaton.iter().

    Finds every alias occurrence, including overlapping ones, by running a longest-first
    alternation at every position where a match can start (re.finditer only reports
    non-overlapping matches, so we re-scan from start+1 after each hit).
    """

    def __init__(self, words: list[str]):
        pattern = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
        self._re = re.compile(pattern)
        # any alias that is a prefix of a longer alias must also be reported at the same start
        self._by_prefix: dict[str, list[str]] = {}
        wordset = set(words)
        for w in words:
            shorter = [w[:i] for i in range(2, len(w)) if w[:i] in wordset]
            if shorter:
                self._by_prefix[w] = shorter

    def iter(self, text: str):
        pos = 0
        n = len(text)
        while pos < n:
            m = self._re.search(text, pos)
            if not m:
                return
            s, e, w = m.start(), m.end(), m.group(0)
            yield e - 1, w
            for shorter in self._by_prefix.get(w, ()):
                yield s + len(shorter) - 1, shorter
            pos = s + 1

HANGUL_RE = re.compile(r"[가-힣]")
ALNUM_RE = re.compile(r"[A-Za-z0-9]")
# Korean particles / suffixes that may directly follow a company name
PARTICLES = (
    "은", "는", "이", "가", "을", "를", "의", "와", "과", "도", "에", "로", "만", "측", "등", "께",
    "서", "엔", "선", "랑", "나", "부터", "까지", "처럼", "보다", "조차", "마저", "밖에", "한테", "에게",
    "으로", "및", "대", "발", "산", "표", "판", "행", "채", "주", "株", "側", "내", "간", "식", "형",
    "다", "라", "였", "임", "이라", "이란", "이면", "이며", "이자", "라고", "라는", "라며",
)
# Scraped article bodies often end with sidebars / recommendation feeds / newsletter boilerplate that name other
# companies ("Stocks that made our list ... Nvidia", "[뉴스핌 베스트 기사]", blog hashtags followed by related posts).
# When one of these markers appears in the second half of the text, matching stops there.
BOILERPLATE_MARKERS = (
    "[뉴스핌 베스트 기사]", "많이 본 뉴스", "주요 뉴스", "오늘의 상승종목", "마켓 뉴스", "실시간 암호화폐 시세", "[주요포토]",
    "종목 추적기", "관련기사", "관련 기사", "함께 보면 좋은", "추천 기사", "인기 기사", "blog.naver.com",
    "Stocks that made our list", "Stock Advisor", "if you invested $", "made this list on", "The Motley Fool",
    "StockStory", "Our Top 5", "Read more here", "Sign up for", "Subscribe to", "Disclosure:", "Disclaimer:",
    "This article was originally published", "Benzinga does not provide",
)
HASHTAG_BLOCK_RE = re.compile(r"(?:#[^\s#]{2,}\s*){3,}")
BOILERPLATE_MIN_FRACTION = 0.4   # never cut inside the first 40% of the text


def trim_boilerplate(text: str) -> int:
    """Return the cut position (len(text) when nothing is trimmed)."""
    floor = int(len(text) * BOILERPLATE_MIN_FRACTION)
    cut = len(text)
    for mk in BOILERPLATE_MARKERS:
        i = text.find(mk, floor)
        if 0 <= i < cut:
            cut = i
    m = HASHTAG_BLOCK_RE.search(text, floor)
    if m and m.start() < cut:
        cut = m.start()
    return cut

GROUP_MEMBERS = {
    "SAMSUNG": ("삼성",), "HYUNDAI": ("현대", "HD현대"), "HYUNDAI_MOTOR": ("현대차", "기아", "현대모비스", "현대글로비스", "현대오토에버", "현대로템", "현대건설"),
    "HD_HYUNDAI": ("HD현대", "HD한국조선해양"), "LG": ("LG",), "SK": ("SK",), "GS": ("GS",), "LS": ("LS",),
    "HANWHA": ("한화",), "DOOSAN": ("두산",), "POSCO": ("POSCO", "포스코"), "MIRAE": ("미래에셋",),
    "KAKAO": ("카카오",), "KB": ("KB",), "SHINHAN": ("신한",), "CELLTRION": ("셀트리온",),
    "HANJIN": ("한진칼", "대한항공"), "ALPHABET": ("Alphabet",),
}
# "70억2000만달러(한화 약 10조849억원)" is 韓貨 - a won conversion of a foreign amount, not the Hanwha group.
# Both halves are required: a won amount right after the alias AND a foreign currency just before it.
KRW_CONVERSION_ALIASES = frozenset({"한화"})
KRW_AMOUNT_RE = re.compile(r"\s*(?:약|기준)?\s*\d[\d,.\s조억만천백]*\s*원")
FOREIGN_CURRENCY_RE = re.compile(
    r"달러|유로|파운드|위안|엔화|바트|페소|루피|링깃|프랑|루블|헤알|리라|디르함|크로나|\d\s*엔"
)
KRW_CONVERSION_LOOKBEHIND = 40


def is_krw_conversion(text: str, start: int, end: int) -> bool:
    if not KRW_AMOUNT_RE.match(text, end):
        return False
    return bool(FOREIGN_CURRENCY_RE.search(text[max(0, start - KRW_CONVERSION_LOOKBEHIND):start]))


GROUP_CONFIDENCE = 0.5
PRODUCT_CONFIDENCE = 0.7  # product name (페이스북, 아이폰) counted only when the company is also named

# Sports articles refer to teams by the sponsor's group name ("한화의 선발투수", "삼성과 신한 SOL 뱅크 KBO").
# When a document looks like sports coverage, group names are not attributed to the holding company.
SPORTS_KEYWORDS = (
    "KBO", "프로야구", "플레이오프", "포스트시즌", "한국시리즈", "선발투수", "선발 투수", "이닝", "홈런", "타율",
    "타점", "등판", "불펜", "구원", "K리그", "프로축구", "프로농구", "프로배구", "V리그", "V-리그", "KBL", "WKBL",
    "구단", "감독은", "감독이", "리그 ", "경기에서", "연승", "연패", "승리를", "패배", "결승", "준결승", "우승",
    "FC ", "FC와", "FC의", "라운드", "세트 ", "골을", "득점", "MVP", "배구", "농구", "세터", "리시브", "블로킹",
    "공격 성공률", "경기 기록", "선수들", "홈경기", "원정경기", "시즌 개막", "창단",
)
SPORTS_MIN_KEYWORDS = 2
# Companies that are also the name of a pro team or its sponsor. In a sports article their dictionary hits are
# downgraded to confidence 0.4 / method "rule" (kept for audit, excluded by the default 0.5 threshold).
SPORTS_SPONSOR_TICKERS = frozenset({
    "030200",  # KT (위즈, 소닉붐)          "024110",  # 기업은행 (알토스)
    "024110", "015760",  # 한국전력 (빅스톰)   "003490",  # 대한항공 (점보스)
    "003490", "000810",  # 삼성화재 (블루팡스) "000720",  # 현대건설 (힐스테이트)
    "000720", "033780",  # KT&G (정관장)      "105560",  # KB금융 (KB손보 스타즈, KB스타즈)
    "105560", "055550",  # 신한지주 (에스버드) "086790",  # 하나금융지주 (하나원큐)
    "086790", "316140",  # 우리금융지주 (우리WON) "032830",  # 삼성생명 (블루밍스)
    "032830", "005830",  # DB손해보험 (프로미) "000270",  # 기아 (타이거즈)
    "000270", "034730",  # SK (나이츠)         "003550",  # LG (트윈스, 세이커스)
    "003550", "000150",  # 두산 (베어스)        "000880",  # 한화 (이글스)
    "000880", "039490",  # 키움증권 (히어로즈)  "012330",  # 현대모비스 (피버스)
    "012330", "011200",  # HMM? no team — placeholder removed below
})
SPORTS_SPONSOR_TICKERS = SPORTS_SPONSOR_TICKERS - {"011200"}
SPORTS_SPONSOR_CONFIDENCE = 0.4
# When a team name is the ONLY sports signal, the article may just be naming a stadium or a
# sponsorship; downgrade the sponsor's mentions near that team name instead of the whole document.
SPORTS_DOWNGRADE_WINDOW = 200

# Teaser headlines of other articles inserted between paragraphs (StockStory, Barchart, Benzinga...):
# "→ Intel's Black Friday Breakout: ...", "Is Meta Stock a Buy?". Masked (replaced by spaces) before matching so
# offsets stay valid.
INSERTED_HEADLINE_RE = re.compile(
    r"^[ \t]*(?:→|▶|►|»|Related:|RELATED:|Read also:?|READ MORE:?|Read more:?|ALSO READ:?|See also:?|More:|Recommended:?|"
    r"Trending:?|Don't miss:?|Missed .{1,60}\?).*$"
    r"|^[ \t]*(?:Is|Should|Here's|Why|How|Can|Will|What|Where|Which|Buy|Sell|Forget|Time to|This|These|The)\b"
    r"(?![^\n]*\. )[^\n]{2,130}(?:Stock|Stocks|Buy\?|Sell\?|Rally|Breakout|Split|Earnings|Analyst|Bet|Dividend|Upside|Downside)[^\n]{0,60}$",
    re.MULTILINE,
)


def mask_inserted_headlines(text: str) -> str:
    def blank(m):
        return " " * (m.end() - m.start())
    return INSERTED_HEADLINE_RE.sub(blank, text)
SPORTS_TEAM_HINT = frozenset({
    "삼성 라이온즈", "삼성라이온즈", "한화 이글스", "한화이글스", "LG 트윈스", "LG트윈스", "두산 베어스", "두산베어스",
    "KT 위즈", "KT위즈", "kt wiz", "KT wiz", "기아 타이거즈", "KIA 타이거즈", "KIA타이거즈", "키움 히어로즈", "키움히어로즈",
    "SK 와이번스", "SSG 랜더스", "롯데 자이언츠", "NC 다이노스", "삼성 블루팡스", "삼성 썬더스", "현대캐피탈 스카이워커스",
    "LG 세이커스", "KT 소닉붐", "SK 나이츠", "SK 핸드볼", "한화 핸드볼", "현대모비스 피버스",
})


@dataclass
class Alias:
    alias: str
    ticker: str
    name_official: str
    alias_type: str
    ambiguity: str
    group_id: str


@dataclass
class Mention:
    ticker: str
    alias: str
    start: int
    end: int
    method: str          # dict | rule
    confidence: float
    name_official: str = ""
    group_id: str = ""


@dataclass
class MatchResult:
    text: str
    title_len: int
    mentions: list[Mention] = field(default_factory=list)
    unresolved: list[Mention] = field(default_factory=list)
    blocked: list[tuple[str, int, int]] = field(default_factory=list)
    industries: list[Mention] = field(default_factory=list)   # sector expressions; ticker="" and group_id=industry_id
    is_sports: bool = False
    trimmed_at: int | None = None   # char offset where sidebar/boilerplate was cut off (None = nothing cut)

    def by_industry(self) -> list[dict]:
        agg: dict[str, dict] = {}
        for m in sorted(self.industries, key=lambda x: x.start):
            a = agg.setdefault(m.group_id, {"industry_id": m.group_id, "industry_ko": m.name_official, "n_mentions": 0,
                                            "first_pos": self.position_of(m.start), "aliases": set()})
            a["n_mentions"] += 1
            a["aliases"].add(m.alias)
        for a in agg.values():
            a["aliases"] = sorted(a["aliases"])
        return list(agg.values())

    def position_of(self, start: int, lead_chars: int = 200) -> str:
        if start < self.title_len:
            return "title"
        if start < self.title_len + 1 + lead_chars:
            return "lead"
        return "body"

    def by_ticker(self) -> list[dict]:
        agg: dict[str, dict] = {}
        for m in sorted(self.mentions, key=lambda x: x.start):
            a = agg.setdefault(m.ticker, {
                "ticker": m.ticker, "name_official": m.name_official, "n_mentions": 0,
                "first_pos": self.position_of(m.start), "confidence": 0.0, "method": set(), "aliases": set(),
            })
            a["n_mentions"] += 1
            a["confidence"] = max(a["confidence"], m.confidence)
            a["method"].add(m.method)
            a["aliases"].add(m.alias)
        for a in agg.values():
            a["method"] = "+".join(sorted(a["method"]))
            a["aliases"] = sorted(a["aliases"])
        return list(agg.values())


class CompanyMatcher:
    def __init__(self, aliases: list[Alias]):
        self.aliases: dict[str, Alias] = {}
        for a in aliases:
            # groups win over same-string company aliases (build_aliases already avoids most cases)
            if a.alias in self.aliases and self.aliases[a.alias].alias_type == "group":
                continue
            self.aliases[a.alias] = a
        if ahocorasick is not None:
            self.automaton = ahocorasick.Automaton()
            for s in self.aliases:
                self.automaton.add_word(s, s)
            self.automaton.make_automaton()
            self.backend = "pyahocorasick"
        else:
            self.automaton = _RegexScanner(list(self.aliases))
            self.backend = "regex"

    @classmethod
    def from_rows(cls, rows: list[dict]) -> "CompanyMatcher":
        return cls([Alias(r["alias"], r["ticker"], r["name_official"], r["alias_type"], r["ambiguity"], r["group_id"])
                    for r in rows])

    @classmethod
    def from_csv(cls, path: str | Path) -> "CompanyMatcher":
        with Path(path).open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        return cls([Alias(r["alias"], r["ticker"], r["name_official"], r["alias_type"], r["ambiguity"], r["group_id"])
                    for r in rows])

    # ---------------------------------------------------------------- boundaries
    @staticmethod
    def _left_ok(text: str, start: int) -> bool:
        if start == 0:
            return True
        p = text[start - 1]
        return not (HANGUL_RE.match(p) or ALNUM_RE.match(p))

    @staticmethod
    def _right_ok(text: str, end: int) -> bool:
        if end >= len(text):
            return True
        n = text[end]
        if ALNUM_RE.match(n):
            return False
        if HANGUL_RE.match(n):
            tail = text[end:end + 3]
            return any(tail.startswith(p) for p in PARTICLES)
        return True

    @staticmethod
    def _ticker_context_ok(text: str, start: int, end: int, ticker: str) -> bool:
        """Numeric tickers: '(005930)'. Latin tickers: '$NVDA', 'NASDAQ: NVDA', '나스닥:NVDA'.

        A bare '(NVDA)' is NOT enough for Latin tickers because Korean news puts acronyms in
        parentheses ('황반변성(AMD)', '스트리밍(FAST)'). Such cases are accepted later only when
        the token right before '(' is a name alias of the same ticker (see match()).
        """
        prev = text[start - 1] if start > 0 else ""
        prev2 = text[max(0, start - 2):start]
        nxt = text[end] if end < len(text) else ""
        if ALNUM_RE.match(nxt or " "):
            return False
        if prev == "$" or prev2 in (": ", ":", "：") or prev == ":":
            return True
        if ticker.isdigit():
            return prev == "(" or (nxt == ")" and not (ALNUM_RE.match(prev or " ") or HANGUL_RE.match(prev or " ")))
        return prev == "(" and nxt == ")"  # provisional; confirmed against an adjacent name in match()

    # ---------------------------------------------------------------- matching
    def _candidates(self, text: str) -> list[tuple[int, int, Alias]]:
        out = []
        for end_idx, s in self.automaton.iter(text):
            end = end_idx + 1
            start = end - len(s)
            a = self.aliases[s]
            if a.ambiguity == "ticker_only":
                if not self._ticker_context_ok(text, start, end, a.ticker):
                    continue
            elif a.ambiguity == "blocked":
                # exclusion patterns only need a clean left edge and no ASCII continuation:
                # "IBK 기업은행 알토스전" must still consume "기업은행"
                nxt = text[end] if end < len(text) else ""
                if not self._left_ok(text, start) or ALNUM_RE.match(nxt or " "):
                    continue
            else:
                if not (self._left_ok(text, start) and self._right_ok(text, end)):
                    continue
                if a.alias in KRW_CONVERSION_ALIASES and is_krw_conversion(text, start, end):
                    continue
            out.append((start, end, a))
        # leftmost-longest
        out.sort(key=lambda x: (x[0], -(x[1] - x[0])))
        chosen, last_end = [], -1
        for start, end, a in out:
            if start < last_end:
                continue
            chosen.append((start, end, a))
            last_end = end
        return chosen

    def match(self, title: str | None, body: str | None) -> MatchResult:
        title = title or ""
        body = body or ""
        text = title + "\n" + body
        res = MatchResult(text=text, title_len=len(title))
        cut = trim_boilerplate(text)
        if cut < len(text):
            res.trimmed_at = cut
        scan_text = mask_inserted_headlines(text[:cut])
        group_hits: list[Mention] = []
        product_hits: list[Mention] = []
        pending_tickers: list[Mention] = []
        candidates = self._candidates(scan_text)
        for start, end, a in candidates:
            if a.ambiguity == "blocked":
                res.blocked.append((a.alias, start, end))
                continue
            if a.ambiguity == "needs_context":
                group_hits.append(Mention(a.ticker, a.alias, start, end, "rule", GROUP_CONFIDENCE, a.name_official, a.group_id))
                continue
            if a.ambiguity == "product":
                product_hits.append(Mention(a.ticker, a.alias, start, end, "product", PRODUCT_CONFIDENCE, a.name_official, "PRODUCT"))
                continue
            if a.ambiguity == "industry":
                res.industries.append(Mention("", a.alias, start, end, "industry", 1.0, a.name_official, a.group_id))
                continue
            if a.ambiguity == "ticker_only":
                m = Mention(a.ticker, a.alias, start, end, "dict", 0.9, a.name_official, a.group_id)
                strong = a.ticker.isdigit() or text[start - 1:start] == "$" or text[start - 1:start] == ":" \
                    or text[max(0, start - 2):start] in (": ", "：")
                (res.mentions if strong else pending_tickers).append(m)
                continue
            res.mentions.append(Mention(a.ticker, a.alias, start, end, "dict", 1.0, a.name_official, a.group_id))

        # '(NVDA)' style Latin tickers: keep only when a name alias of the same ticker sits right before '('
        if pending_tickers:
            name_ends = {(m.ticker, m.end) for m in res.mentions + group_hits}
            for m in pending_tickers:
                before = m.start - 1  # index of '('
                while before > 0 and text[before - 1] == " ":
                    before -= 1
                if (m.ticker, before) in name_ends:
                    res.mentions.append(m)

        sports_hits = sum(1 for k in SPORTS_KEYWORDS if k in text)
        team_spans = [(st, en) for _a, st, en in res.blocked if _a in SPORTS_TEAM_HINT]
        res.is_sports = bool(team_spans) or sports_hits >= SPORTS_MIN_KEYWORDS
        if res.is_sports:
            # "기업은행보다 잘했다" in a volleyball recap is the team, not the bank. A match recap is
            # sports end to end, but "수원 KT 위즈파크점" inside a phone-launch story is one stadium
            # name - there, only the mentions sitting next to the team name are the team.
            whole_doc = sports_hits >= SPORTS_MIN_KEYWORDS
            for m in res.mentions:
                if m.ticker not in SPORTS_SPONSOR_TICKERS or m.method != "dict":
                    continue
                if whole_doc or any(st - SPORTS_DOWNGRADE_WINDOW <= m.start <= en + SPORTS_DOWNGRADE_WINDOW
                                    for st, en in team_spans):
                    m.confidence, m.method = SPORTS_SPONSOR_CONFIDENCE, "rule"

        if group_hits:
            has_hangul = bool(HANGUL_RE.search(text))
            present_names = {m.name_official for m in res.mentions}
            present_tickers = {m.ticker for m in res.mentions}
            for g in group_hits:
                # "GS", "MS", "KB", "SK", "LG" mean other things in English text (Goldman Sachs, Morgan Stanley, kilobyte)
                if g.alias.isascii() and len(g.alias) <= 2 and not has_hangul:
                    continue
                prefixes = GROUP_MEMBERS.get(g.group_id, ())
                member_present = (g.group_id in present_tickers) or any(
                    n.startswith(p) for n in present_names for p in prefixes)
                if member_present:
                    continue  # attributed to the explicitly named member(s)
                if g.ticker and not res.is_sports:
                    res.mentions.append(g)
                else:
                    res.unresolved.append(g)

        if product_hits:
            # "페이스북에 글을 올렸다" is a platform mention, not a company mention. Keep product names only when
            # the company itself (메타/Meta, 애플...) is named somewhere in the same document.
            company_tickers = {m.ticker for m in res.mentions if m.method in ("dict", "rule")}
            for p in product_hits:
                (res.mentions if p.ticker in company_tickers else res.unresolved).append(p)
        res.mentions.sort(key=lambda m: m.start)
        return res
