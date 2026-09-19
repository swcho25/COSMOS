"""Outcome-blind, conservative article/company event screening, version 2.

The previous same-sentence rule confused a data supplier with an earnings
subject, customer sales with a bank's results, and old acquisitions with new
events. This module adds explicit subject/action templates and local historical
and attribution exclusions. It only reads title, body, publication date, literal
mention offsets and previous weak event suggestions from the supplied mapping.
It never reads prices, outcomes, predictions, source-provided sentiment or files.

``evaluate_article(article)`` returns one candidate/abstain decision per literal
ticker, exact evidence offsets into ``title + '\\n' + body``, and reasons. A
candidate is a RULE SUGGESTION with human review pending, not a verified event,
human label, causal finding, directional sentiment, or probability. In particular
publication-relative language cannot prove when an undated event first happened.
No existing text cleaners, availability times, labels or models are modified.

Only headline/first 2,400 body characters are eligible, with complete bounded
sentences and direct subject/action patterns. Later evidence can explain
abstention but cannot admit an article. Restriction trades recall for an auditable
review queue. Explicit old event years/months and historical markers cause
abstention; prior-year earnings periods are allowed only with a reporting action.
Unsupported clauses, pronouns, indirect subjects and uncertain forecasts abstain.
Revenue-sharing agreements are contracts, not earnings observations. The limits
and templates are protocol choices, not fitted or empirically calibrated values.

Version 2 fixes a development-review error: Samsung SDI inside a parenthetical
list of assets bought by Lotte was incorrectly treated as the acquirer. Korean
rules now require a subject/topic/headline marker, parenthetical mentions abstain,
and last-year/month expressions block non-earnings events. The original 60-article
AI review is now development material, not independent validation of this version.
"""

from datetime import date
import re


RULE_VERSION = 'current-event-subject-v2'
LEAD_BODY_CHARS = 2400
MAX_SENTENCE_CHARS = 700

_SPACE = r"[\s,，:：]*"
_EN_AUX = r"(?:(?:has|have|had|just|today|recently)\s+){0,2}"
_KO_SUBJECT = r"(?:(?:은|는|이|가)\s+|\s*[,，:：]\s*|\s+(?:역시|또한|도)\s+)"
_GAP = r"[^;\n!?。！？\"“”]{0,120}?"
_FIN = r"(?:earnings|revenue|net income|operating profit|quarterly results)"
_KFIN = r"(?:영업이익|순이익|매출(?:액)?|실적)"

# The trigger is preserved as its own literal span, independently of the quote.
# Anchoring after the company mention avoids matching an arbitrary nearby verb.
_SPECS = [
    ('en_report_results', 'earnings', _SPACE + _EN_AUX +
     r"(?:reported|reports|posted|posts|announced|announces)\b" + _GAP +
     r"(?P<trigger>" + _FIN + r")\b"),
    ('en_possessive_results', 'earnings',
     r"['’]s\s+(?:(?:quarterly|annual|first-quarter|second-quarter|third-quarter|fourth-quarter)\s+)?"
     r"(?P<trigger>" + _FIN + r")\s+(?:rose|fell|grew|jumped|dropped|increased|declined|was|were)\b"),
    ('ko_report_results', 'earnings', _KO_SUBJECT + _GAP +
     r"(?P<trigger>" + _KFIN + r")" + _GAP +
     r"(?:기록했|기록한|발표했|공시했|잠정\s*집계|증가했|감소했|늘었|줄었|달성했)"),
    ('en_contract_action', 'contract', _SPACE + _EN_AUX +
     r"(?:signed|signs|won|wins|secured|secures|confirmed|announced|announces|renewed)\b" + _GAP +
     r"(?P<trigger>(?:revenue[ -]shar(?:e|ing)\s+)?(?:agreement|contract|partnership|deal))\b"),
    ('en_possessive_revenue_agreement', 'contract',
     r"['’]s\s+[^;\n!?]{0,65}?\bdeal\b[^;\n!?]{0,65}?"
     r"(?P<trigger>revenue[ -]shar(?:e|ing)\s+agreement)\b"),
    ('ko_contract_action', 'contract', _KO_SUBJECT + _GAP +
     r"(?P<trigger>(?:공급|수익\s*배분|판매|납품|공동\s*개발|업무\s*협약)?\s*(?:계약|협약))"
     r"[^;\n!?]{0,50}?(?:체결했|체결한|맺었|수주했|수주한|체결키로|체결한다고)"),
    ('ko_contract_award', 'contract', _KO_SUBJECT + _GAP +
     r"(?P<trigger>수주했|수주한|수주했다고)"),
    ('en_acquisition_action', 'merger_acquisition', _SPACE + _EN_AUX +
     r"(?P<trigger>acquired|acquires|is\s+acquiring|agreed\s+to\s+(?:buy|acquire)|"
     r"agrees\s+to\s+(?:buy|acquire)|announced\s+(?:its\s+|the\s+|an\s+)?acquisition)\b"),
    ('ko_acquisition_action', 'merger_acquisition', _KO_SUBJECT + _GAP +
     r"(?P<trigger>인수한다|인수했다|인수하는\s*계약|인수키로\s*했다|인수를\s*결정했|합병을\s*결정했|합병한다)"),
    ('en_capital_action', 'capital_return', _SPACE + _EN_AUX +
     r"(?:announced|announces|approved|approves|declared|declares|increased|raised|cut|suspended)\b" + _GAP +
     r"(?P<trigger>dividend|buyback|share\s+repurchase|stock\s+repurchase|share\s+offering)\b"),
    ('ko_capital_action', 'capital_return', _KO_SUBJECT + _GAP +
     r"(?P<trigger>배당|자사주|유상\s*증자)" + _GAP +
     r"(?:결정했|결의했|발표했|공시했|확대했|매입했|소각했|지급했|실시한다고)"),
    ('en_legal_action', 'legal_operational', _SPACE + _EN_AUX +
     r"(?P<trigger>filed\s+for\s+bankruptcy|filed\s+(?:a\s+)?lawsuit|"
     r"faces\s+(?:a\s+)?lawsuit|is\s+being\s+sued|was\s+sued|recalled|recalls|"
     r"closed\s+(?:its\s+|a\s+)?(?:factory|plant)|"
     r"halted\s+production|suspended\s+production|laid\s+off)\b"),
    ('ko_legal_action', 'legal_operational', _KO_SUBJECT + _GAP +
     r"(?P<trigger>리콜(?:을)?\s*(?:실시|결정|발표)|소송(?:을)?\s*제기했|"
     r"파산(?:을)?\s*신청했|생산(?:을)?\s*중단했|공장(?:을)?\s*폐쇄했)"),
]
RULES = [(name, kind, re.compile(r'^(?:' + pattern + ')', re.I))
         for name, kind, pattern in _SPECS]

_HISTORY = re.compile(
    r'\b(?:previously|historically|formerly|once|years?\s+ago|months?\s+ago|'
    r'(?:one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+years?\s+(?:after|before)|'
    r'last\s+year|last\s+month|had\s+(?:acquired|bought|announced|signed|reported)|'
    r'back\s+in)\b|수년\s*전|몇\s*년\s*전|과거|당시|예전에|지난해\s*인수|작년\s*인수', re.I)
_UNCERTAIN = re.compile(
    r'\b(?:could|might|may|rumou?r|reportedly|expected\s+to|set\s+to|'
    r'will\s+report|plans?\s+to|considering|forecast|estimat(?:e[ds]?|ion))\b|'
    r'예상|전망|추정|검토\s*중|추진할\s*계획|가능성|관측|것으로\s*알려', re.I)
_NEGATED = re.compile(
    r"\b(?:did\s+not|didn't|has\s+not|hasn't|denied|denies|never|not\s+announced)\b|"
    r'부인했|사실이\s*아니|하지\s*않|한\s*바\s*없', re.I)
_ATTRIBUTION_BEFORE = re.compile(
    r'(?:according\s+to|data\s+(?:from|by)|survey.{0,45}\bby|'
    r'research\s+(?:from|by)|cites?\s+|quoted\s+by|분석\s*기관|자료\s*제공)\s*$', re.I)
_ATTRIBUTION_AFTER = re.compile(
    r"^\s*(?:['’]s\s+)?(?:research|survey|analysts?|estimates?|data|조사|자료|연구)\b|"
    r'^(?:에\s*따르면|의\s*(?:조사|자료|추정|전망|연구))', re.I)
_INDIRECT_AFTER = re.compile(
    r"^\s*(?:['’]s\s+)?(?:customers?|clients?|suppliers?|rivals?|competitors?|"
    r"products?|smartphones?|phones?|ranking|ranked|supplier|analysts?)\b|"
    r'^(?:의\s*)?(?:고객|이용자|협력사|경쟁사|제품|매출대금|전자금융)', re.I)
_NON_RESULT = re.compile(r'매출대금|매출\s*순위|판매\s*순위|고객(?:의)?\s*매출|'
    r'\b(?:sales\s+rank|best[ -]sell(?:er|ing)|revenue[ -]shar(?:e|ing))\b', re.I)
_RESEARCH_CONTEXT = re.compile(r'\b(?:survey|research|study)\b|조사에\s*따르면|연구\s*결과', re.I)
_MONTHS = {name.lower(): i for i, name in enumerate(
    ('January', 'February', 'March', 'April', 'May', 'June', 'July', 'August',
     'September', 'October', 'November', 'December'), 1)}


def _sentences(full):
    """Boundaries preserve decimal points and include quote-ending punctuation."""
    start = 0
    for boundary in re.finditer(r'\n+|(?<=[.!?。！？])(?=[\s“”"가-힣])\s*', full):
        end = boundary.start()
        if end > start:
            yield start, end
        start = boundary.end()
    if start < len(full):
        yield start, len(full)


def _temporal_reasons(sentence, published, kind):
    reasons = []
    if _HISTORY.search(sentence):
        reasons.append('historical_reference')
    if not published:
        return reasons + ['invalid_publication_date']
    # Earnings may report a prior fiscal year now; comparisons alone are not
    # proof of an old event. Other categories conservatively reject any old year.
    years = [int(y) for y in re.findall(r'\b((?:19|20)\d{2})(?:년|\b)', sentence)]
    if any(y < published.year - (1 if kind == 'earnings' else 0) for y in years):
        reasons.append('historical_year')
    if kind != 'earnings':
        if re.search(r'지난해|작년|지난달|전년도', sentence):
            reasons.append('historical_relative_period')
        months = [int(m) for m in re.findall(r'(?:지난\s*)?(\d{1,2})월', sentence)]
        months += [_MONTHS[m.group(1).lower()] for m in re.finditer(
            r'\bin\s+(' + '|'.join(_MONTHS) + r')\b', sentence, re.I)]
        if any(m != published.month for m in months):
            reasons.append('different_month_reference')
    return reasons


def _blocking_reasons(full, sentence, lo, hit, published, kind):
    before = full[max(lo, hit['start'] - 90):hit['start']]
    after = full[hit['end']:min(lo + len(sentence), hit['end'] + 90)]
    reasons = _temporal_reasons(sentence, published, kind)
    prefix = full[lo:hit['start']]
    if any(prefix.count(left) > prefix.count(right) for left, right in
           (('(', ')'), ('（', '）'), ('[', ']'), ('【', '】'))):
        reasons.append('company_mention_inside_parenthetical')
    if _ATTRIBUTION_BEFORE.search(before) or _ATTRIBUTION_AFTER.search(after):
        reasons.append('source_or_research_attribution')
    if _INDIRECT_AFTER.search(after):
        reasons.append('indirect_customer_product_or_third_party')
    if kind == 'earnings' and _NON_RESULT.search(sentence):
        reasons.append('customer_product_or_revenue_sharing_not_results')
    if _UNCERTAIN.search(sentence):
        reasons.append('forecast_or_uncertain_claim')
    if _NEGATED.search(sentence):
        reasons.append('negated_claim')
    # A research quote is not an operating event of the organization publishing
    # the research. Never admit an earnings observation from such a sentence.
    if kind == 'earnings' and _RESEARCH_CONTEXT.search(sentence):
        reasons.append('research_context_not_company_results')
    return reasons


def evaluate_article(article):
    """Return reproducible weak candidate decisions without mutating ``article``.

    Literal offsets refer to the exact joined supplied strings, not normalized
    text. ``accepted_tickers`` are not verified human events. Experimental
    return-label attachment must preserve weak-selection provenance and be
    reported as such. Unknown/malformed mentions or dates fail closed.
    A ticker can retain valid evidence and also have excluded evidence elsewhere;
    all attempted weak suggestions receive their own literal diagnostic record.
    """
    title, body = article.get('title') or '', article.get('body') or ''
    full = title + '\n' + body
    try:
        published = date.fromisoformat(str(article.get('published_date') or '')[:10])
    except ValueError:
        published = None
    raw_hits = article.get('literal_mentions') or []
    tickers = sorted({str(h.get('ticker')) for h in raw_hits if h.get('ticker')})
    hits = [h for h in raw_hits if isinstance(h.get('start'), int)
            and isinstance(h.get('end'), int) and 0 <= h['start'] < h['end'] <= len(full)
            and h.get('ticker')]
    decisions = {t: dict(ticker=t, status='abstain', reasons=[], evidence=[],
                        excluded_evidence=[]) for t in tickers}
    valid_tickers = {str(h['ticker']) for h in hits}
    for ticker in tickers:
        if ticker not in valid_tickers:
            decisions[ticker]['reasons'].append('invalid_literal_mention')
    weak = article.get('event_evidence') or []
    seen, excluded_seen = set(), set()
    for lo, hi in _sentences(full):
        sentence = full[lo:hi]
        local_hits = [h for h in hits if lo <= h['start'] and h['end'] <= hi]
        if not local_hits:
            continue
        for hit in local_hits:
            ticker = str(hit['ticker'])
            decision = decisions[ticker]
            tail = full[hit['end']:hi]
            attempted = []
            for rule_id, kind, pattern in RULES:
                match = pattern.match(tail)
                if not match:
                    continue
                event_lo = hit['end'] + match.start('trigger')
                event_hi = hit['end'] + match.end('trigger')
                reasons = _blocking_reasons(full, sentence, lo, hit, published, kind)
                if lo > len(title) + 1 + LEAD_BODY_CHARS:
                    reasons.append('outside_headline_lead')
                if hi - lo > MAX_SENTENCE_CHARS:
                    reasons.append('unbounded_sentence')
                if any(other['ticker'] != hit['ticker'] and
                       hit['end'] <= other['start'] < event_lo for other in local_hits):
                    reasons.append('intervening_company_subject_ambiguous')
                # Contract clauses for buying a company are acquisition events;
                # revenue-sharing remains contract even though it contains revenue.
                if kind == 'contract' and re.search(r'인수|\bacquisition\b', sentence, re.I):
                    reasons.append('acquisition_contract_use_acquisition_rule')
                evidence = dict(start=lo, end=hi, quote=sentence,
                    mention_start=hit['start'], mention_end=hit['end'],
                    event_start=event_lo, event_end=event_hi,
                    event_quote=full[event_lo:event_hi], event_type=kind,
                    rule_id=rule_id, recency='publication_relative_candidate',
                    human_review_status='pending')
                key = (ticker, lo, hi, kind)
                if reasons:
                    evidence['reasons'] = sorted(set(reasons))
                    if key not in excluded_seen:
                        decision['excluded_evidence'].append(evidence)
                        excluded_seen.add(key)
                elif key not in seen:
                    decision['evidence'].append(evidence)
                    seen.add(key)
                attempted.append(kind)
            # Explain a failed old weak suggestion instead of silently discarding
            # it. Old category is provenance only and cannot force acceptance.
            for old in weak:
                if str(old.get('ticker')) != ticker or not isinstance(old.get('start'), int):
                    continue
                if not (old['start'] < hi and old.get('end', -1) > lo):
                    continue
                kind = old.get('event_type', 'unknown')
                if kind in attempted:
                    continue
                reasons = _blocking_reasons(full, sentence, lo, hit, published, kind)
                reasons.append('no_direct_subject_action_template')
                if lo > len(title) + 1 + LEAD_BODY_CHARS:
                    reasons.append('outside_headline_lead')
                key = (ticker, lo, hi, kind)
                if key not in excluded_seen:
                    decision['excluded_evidence'].append(dict(start=lo, end=hi,
                        quote=sentence, mention_start=hit['start'], mention_end=hit['end'],
                        event_type=kind, rule_id='previous_weak_suggestion',
                        reasons=sorted(set(reasons)), human_review_status='pending'))
                    excluded_seen.add(key)
    for decision in decisions.values():
        if decision['evidence']:
            decision['status'] = 'candidate'
            decision['reasons'] = ['direct_subject_action_rule_with_no_local_time_conflict']
        else:
            decision['reasons'] = sorted(set(decision['reasons'] + [
                r for e in decision['excluded_evidence'] for r in e['reasons']]))
            if not decision['reasons']:
                decision['reasons'] = ['no_direct_subject_action_template']
    return dict(rule_version=RULE_VERSION, selection='conservative_subject_recency_rules',
        human_review_status='pending', label_status='unlabeled',
        accepted_tickers=[t for t in tickers if decisions[t]['status'] == 'candidate'],
        decisions=[decisions[t] for t in tickers])
