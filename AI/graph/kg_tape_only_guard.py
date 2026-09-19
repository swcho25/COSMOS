"""Opt-in recognition of TWO complete observed Sedaily newsbot tape templates.

This is not a generic relevance classifier, keyword exclusion or company-name
filter. Every non-whitespace character must belong to a recognized title, one
of two ordered complete price/volume report grammars, and the exact observed
newsbot footer. Unknown/additional narrative makes the guard abstain. Earnings,
financing, contract, regulatory and halt clauses cannot be swallowed by a text
wildcard: there is no free narrative capture. Missing clauses/attribution abstain.

The narrowly allowed static outstanding-share count is the denominator inside
the complete execution-strength report, never a rule excluding share issuance
or capital-change reports. Literal spans prove syntax coverage only; the marker
does not authenticate the publisher or establish that the numbers are true.

Developed from explicitly exposed train/dev diagnostic cases only. No model,
network, file I/O, corpus edits, automatic integration or hidden truncation.
"""
from __future__ import annotations

import hashlib
import re

VERSION = "kg_tape_only_guard_v1"
INT = r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]{1,12})"
PCT = r"[+-]?[0-9]{1,6}(?:\.[0-9]{1,6})?"
NAME = r"[가-힣A-Za-z0-9&·.\-]{1,40}"
SECTOR = r"[가-힣A-Za-z0-9&· ]{1,24}업"
CLOCK = r"(?:오전|오후)\s+[0-9]{1,2}시\s+[0-9]{1,2}분"
QUANTITY = rf"(?=[0-9])(?:{INT}조)?(?:{INT}억)?(?:{INT}만)?(?:{INT})?"
WIDGET = r"(?:재무분석차트영역상세보기)?"
FOOTER = (r"\[이 기사는 증시분석 전문기자 서경뉴스봇\(newsbot@sedaily\.com\)이 "
          r"실시간으로 작성했습니다\.\](?:\s*/서경뉴스봇\s+newsbot@sedaily\.com)?")
STRENGTH_TITLE = re.compile(rf"<(?:유|코)>(?P<company>{NAME}),\s*{PCT}% (?:오르며|내리며) "
                            rf"체결강도 (?:강세|약세) 지속\({PCT}%\)")
ORDER_TITLE = re.compile(rf"<(?:유|코)>(?P<company>{NAME}),\s*매수잔량 {PCT}% 급증")


def _failure(text, reason):
    return {"guard_version": VERSION, "matched": False, "template": None, "spans": [],
            "reason": reason, "all_nonwhitespace_accounted_for": False,
            "source_sha256": hashlib.sha256(text.encode("utf8")).hexdigest(),
            "decision": None, "production_approved": False}


def _consume(text, position, pattern, kind, spans):
    result = re.compile(r"\s*(?:" + pattern + r")").match(text, position)
    if result is None:
        return None
    spans.append({"start": position, "end": result.end(), "quote": text[position:result.end()], "kind": kind})
    return result


def _strength(text, position, spans):
    first = _consume(text, position,
        rf"(?P<company>{NAME})\([0-9]{{6}}\)\s*{WIDGET}(?:이|가)\s+[0-9]{{1,2}}일\s+{CLOCK}\s+현재\s+"
        rf"{PCT}%\s+(?:오른|내린)\s+{INT}원에\s+거래\s+중이며,\s*체결강도는\s+{PCT}%로\s+(?:강세|약세)를\s+유지했다\.",
        "current_price_execution_strength", spans)
    if first is None:
        return None
    company, position = first["company"], first.end()
    c = re.escape(company)
    patterns = [
        (rf"총매수체결량은\s+{INT}주,\s*총매도체결량은\s+{INT}주를\s+각각\s+기록했다\.", "execution_volumes"),
        (r"\(※\s*체결강도\(%\)\s*=\s*\(총매수체결량/총매도체결량\)\*100\)", "metric_formula"),
        (rf"지난\s+1개월간\s+{c}(?:은|는)\s+{PCT}%\s+(?:상승|하락)했으며,\s*표준편차는\s+{PCT}%를\s+기록했다\.", "one_month_return_deviation"),
        (rf"이\s+기간\s+‘(?P<sector>{SECTOR})[‘’]\s+업종\s+평균\s+주가수익률은\s+{PCT}%\s*로\s+나타났으며,\s*변동성은\s+{PCT}%를\s+기록했다\.", "sector_return_volatility"),
    ]
    sector = None
    for pattern, kind in patterns:
        match = _consume(text, position, pattern, kind, spans)
        if match is None:
            return None
        position = match.end()
        if "sector" in match.groupdict():
            sector = match["sector"]
    patterns = [
        (rf"이\s+시각\s+현재\s+‘{re.escape(sector)}[‘’](?:은|는)\s+{PCT}%\s+(?:상승|하락)중이며,\s*"
         rf"[0-9]{{1,3}}\(매도\):[0-9]{{1,3}}\(매수\)의\s+(?:매도|매수)우위를\s+기록하고\s+있다\.", "sector_current_tape"),
        (rf"{c}의\s+총\s+상장\s+주식수는\s+{INT}주이며,\s*이\s+시각\s+현재\s+거래량은\s+{INT}주로\s+상장주식수\s+대비\s+거래량은\s+{PCT}%\s*로\s+나타난다\.", "outstanding_share_volume_denominator"),
        (rf"지난\s+1개월간\s+{c}의\s+거래량\s+회전율은\s+{PCT}%\s*를\s+기록했다\.", "one_month_turnover"),
    ]
    for pattern, kind in patterns:
        match = _consume(text, position, pattern, kind, spans)
        if match is None:
            return None
        position = match.end()
    return company, position


def _orderbook(text, position, spans):
    first = _consume(text, position,
        rf"{CLOCK}\s+현재\s+(?P<company>{NAME})\([0-9]{{6}}\)\s*{WIDGET}의\s+매수잔량이\s+{INT}주로\s+"
        rf"1분전\s+매수잔량\s+{INT}주\s+대비\s+{PCT}%\s+급증했다\.", "orderbook_one_minute_change", spans)
    if first is None:
        return None
    company, position = first["company"], first.end()
    patterns = [
        (rf"거래대금은\s+{QUANTITY}(?:원)?이며\s+거래량은\s+{QUANTITY}주로\s+거래량회전율은\s+{PCT}%로\s+나타났다\.", "trading_amount_volume_turnover"),
        (rf"이\s+시각\s+현재\s+{re.escape(company)}(?:은|는)\s+{PCT}%\s+(?:오른|내린)\s+{INT}원에\s+거래되고\s+있으며,\s*"
         rf"[0-9]{{1,3}}\(매도\):[0-9]{{1,3}}\(매수\)의\s+(?:매도|매수)우위를\s+기록하고\s+있다\.", "current_price_orderbook_ratio"),
    ]
    for pattern, kind in patterns:
        match = _consume(text, position, pattern, kind, spans)
        if match is None:
            return None
        position = match.end()
    return company, position


def detect_tape_only(article_text):
    """Recognize body alone, or a known tape title + newline + complete body.

    False means abstain, not 'economically relevant'. True returns exact offsets
    into the supplied unmodified string. Caller decides whether to opt in.
    """
    if not isinstance(article_text, str):
        raise TypeError("article_text must be a string")
    if not article_text.strip():
        return _failure(article_text, "empty_input")
    # Resource refusal is explicit abstention; no prefix is ever classified.
    if len(article_text) > 20000:
        return _failure(article_text, "outside_bounded_template_length")
    for template, parser, title_pattern in (
            ("sedaily_execution_strength_complete_v1", _strength, STRENGTH_TITLE),
            ("sedaily_orderbook_surge_complete_v1", _orderbook, ORDER_TITLE)):
        position, spans, title_company = 0, [], None
        leading = len(article_text) - len(article_text.lstrip())
        if article_text[leading:leading + 1] == "<":
            newline = article_text.find("\n", leading)
            if newline < 0:
                continue
            title = article_text[leading:newline].rstrip("\r")
            matched_title = title_pattern.fullmatch(title)
            if matched_title is None:
                continue
            title_company = matched_title["company"]
            spans.append({"start": 0, "end": newline + 1, "quote": article_text[:newline + 1], "kind": "known_tape_title"})
            position = newline + 1
        result = parser(article_text, position, spans)
        if result is None:
            continue
        company, position = result
        if title_company is not None and title_company != company:
            continue
        footer = _consume(article_text, position, FOOTER, "exact_observed_newsbot_footer", spans)
        if footer is None or article_text[footer.end():].strip():
            continue
        if footer.end() < len(article_text):
            spans.append({"start": footer.end(), "end": len(article_text), "quote": article_text[footer.end():], "kind": "trailing_whitespace"})
        if "".join(item["quote"] for item in spans) != article_text:
            raise AssertionError("Guard did not account for the whole input")
        return {"guard_version": VERSION, "matched": True, "template": template, "company_in_template": company,
                "spans": spans, "reason": "entire_input_matches_known_price_volume_template_and_footer",
                "all_nonwhitespace_accounted_for": True, "source_sha256": hashlib.sha256(article_text.encode("utf8")).hexdigest(),
                "decision": None, "production_approved": False,
                "does_not_authenticate_publisher_or_numbers": True}
    return _failure(article_text, "unrecognized_or_additional_content_abstain")
