"""V3 sentence-local four-type relation extraction, with explicit role grammar.

This is a small Korean/English grammar, not a semantic classifier or a claim
that relations are true in the world. Accepted article/company relevance is a
prerequisite, not evidence that every pair of accepted companies is related.
Only an explicit two-party clause with resolved grammatical roles becomes an
edge. Co-occurrence, nearby keywords, shared industry and price comparison do
not establish an edge. Unsupported clauses remain review candidates.

The public types are exactly SUPPLY, INVEST, PARTNER and COMPETE. SUPPLY points
supplier -> customer. INVEST is the investment/equity umbrella: ordinary
investment, stake acquisition, explicit shareholding, and explicit acquisition
of a whole company have separate relation_subtype values. Whole-company
acquisition keeps buyer -> target semantics; it is never inferred from buying a
product, brand, division, plant or other asset. PARTNER and COMPETE are symmetric
and stored in ticker order. Recorded ownership events do not establish current
holdings. Competition never determines either company's financial sentiment.
Negation, plans, uncertain language, biographies, group/name extensions and
multi-party clauses are deliberately deferred. A repeated mention of one of the
same two companies is allowed only when the full grammar resolves its role,
such as an agreement signed at that company's research center. Repetition alone
does not select endpoints or make an edge. Historical assertions can be
recorded; neither their present validity nor a stock-price effect is inferred.

Offsets are Python Unicode character offsets into EXACTLY title + '\\n' + body;
no normalization changes the evidence. Every returned source/target span is a
literal alias occurrence inside its evidence sentence. No model, network,
filesystem, financial price, or existing production graph is consulted.

The grammar trades recall for precision. Reviews include harmless co-mentions
and are NOT asserted relationships. In particular, callers must not turn the
review list into fallback edges or interpret rule hits as calibrated confidence.

Caller-owned accepted identities are still a prerequisite: lexical group names
and historical name mappings require upstream issuer verification. This module
cannot turn an undated alias into proof of legal identity. This is a separate
revision; the original extractor and completed graph remain unchanged.
"""
from __future__ import annotations

import re
from collections import defaultdict

SCHEMA = "kg-grounded-relations-3"
POLICY_VERSION = "explicit-two-party-role-grammar-3"
RELATION_TYPES = ("SUPPLY", "INVEST", "PARTNER", "COMPETE")
OFFSET_BASIS = "title_newline_body"

# Predicate slots cannot contain a comma, another entity token, a quoted
# clause, sentence punctuation or a Korean grammatical subject. They are
# bounded so a relation verb cannot reach across a long intervening clause.
KO_OBJECT = r"[가-힣A-Za-z0-9%$€₩ +/·()_-]{1,60}?"
EN_OBJECT = r"[A-Za-z0-9%$€£ +/()_-]{1,80}?"
KO_TIME = r"(?:(?:지난|오는)\s+)?(?:[0-9]{4}년\s*)?(?:[0-9]{1,2}월\s*)?[0-9]{1,2}일|(?:최근|현재|이날|전날|올해|지난해)(?:\s+(?:상반기|하반기))?(?:부터)?|(?:지난\s*)?(?:[0-9]{4}년\s*)?[0-9]{1,2}월\s*(?:말\s*(?:기준)?|초|기준)|지난\s+[0-9]{1,2}월"
KO_TIME_SLOT = rf"(?:(?:{KO_TIME})\s+){{0,2}}"
KO_SUBJECT = rf"(?:은|는|이|가|도)\s+{KO_TIME_SLOT}"
KO_END = r"(?:했다|하였다|한다|해왔다|해\s+왔다|하고\s+있다|한\s+바\s+있다|중이다|했다는\s+사실이\s+확인됐다)"
KO_VERB = r"(?:순차적으로\s+|직접\s+|지속적으로\s+)?(?:공급|납품|제공)" + KO_END
MARK_A, MARK_B = "⟦A⟧", "⟦B⟧"
# A named operating division is part of the named company in this syntax.
# A subsidiary/group/affiliate is NOT normalized to the parent. Recipient
# sites likewise must be explicitly possessed by the named customer.
KO_DIVISION = r"\s+[가-힣A-Za-z0-9]{1,20}(?:부문|사업부)(?:\([가-힣A-Za-z0-9 +_-]{1,30}\))?"
KO_SUPPLY_SUBJECT = rf"(?:{KO_DIVISION})?{KO_SUBJECT}"
KO_CUSTOMER_SITE = r"(?:의)?\s+(?:(?:국내|해외)\s+)?(?:사업장|공장)에\s+"
KO_SITE_PRODUCT = r"(?:설치된|설치될)\s+[‘'\"][가-힣A-Za-z0-9 ]{1,40}[’'\"]에\s+"
KO_SITE_OBJECT = r"[가-힣A-Za-z0-9%$€₩㎿㎾㎡㎥ +/·()_-]{1,80}?"
KO_STAKE_AMOUNT = r"(?:\s+(?:약\s+)?[0-9]+(?:\.[0-9]+)?%(?:가량|\s*수준)?)?"
KO_HOLDS = r"(?:보유(?:하고\s+(?:있다|있습니다)|한다|했다)|(?:갖고|가지고)\s+(?:있다|있습니다))"
KO_COMPETITION_ACTION = r"(?:치열한\s+|열띤\s+|치열하게\s+)?경쟁(?:을\s+)?(?:펼치고\s+있다|벌이고\s+있다|이어가고\s+있다|펼쳐왔다|펼쳐\s+왔다|벌여왔다|벌여\s+왔다|벌인다|펼친다)"
# These finite matcher-only envelopes preserve all original source offsets.
# A site is a place belonging to an explicit endpoint, never a third company.
KO_REGION = r"(?:(?:서울|경기|경기도|경남|경북|충남|충북|전남|전북|부산|인천|대전|울산|대구|세종)(?:\s+[가-힣]{1,12}(?:시|군|구|동))?\s+)?"
KO_OWN_SITE = rf"{KO_REGION}⟦[AB]⟧(?:의)?\s+(?:[가-힣A-Za-z0-9]{{1,20}}\s*)?(?:본사|공장|연구소|기술연구소|사옥|오피스|센터)에서\s+"
KO_UNNAMED_SITE = rf"{KO_REGION}(?:본사|사옥|공장|연구소)에서\s+"
KO_SITE_SLOT = rf"(?:{KO_OWN_SITE}|{KO_UNNAMED_SITE})?"
KO_AGREEMENT_OBJECT = r"[가-힣A-Za-z0-9%$€₩ +/·()_&–—:-]{0,160}?"
KO_AGREEMENT_NAME = r"(?:협력\s+협약|업무협약|양해각서|MOU|제휴\s+계약|협력\s+계약)(?:\s*\(MOU\))?"
# Optional matching punctuation is paired explicitly, not stripped arbitrarily.
KO_AGREEMENT = rf"(?:{KO_AGREEMENT_OBJECT}{KO_AGREEMENT_NAME}|‘{KO_AGREEMENT_OBJECT}{KO_AGREEMENT_NAME}’|'" + rf"{KO_AGREEMENT_OBJECT}{KO_AGREEMENT_NAME}'|“{KO_AGREEMENT_OBJECT}{KO_AGREEMENT_NAME}”|\"{KO_AGREEMENT_OBJECT}{KO_AGREEMENT_NAME}\")"


def _rule(name, rel, direction, expression):
    return name, rel, direction, re.compile(expression, re.IGNORECASE)


# Full clause matching is intentional. There is no keyword/proximity fallback.
RULES = (
    _rule("ko_partner_joint_role_envelope", "PARTNER", "symmetric",
          rf"{MARK_A}(?:와|과)\s+{MARK_B}(?:는|은|가)\s+{KO_TIME_SLOT}{KO_SITE_SLOT}{KO_AGREEMENT}(?:을|를)\s+체결{KO_END}"),
    _rule("ko_partner_with_role_envelope", "PARTNER", "symmetric",
          rf"{MARK_A}{KO_SUBJECT}{KO_SITE_SLOT}{MARK_B}(?:와|과)\s+{KO_SITE_SLOT}{KO_AGREEMENT}(?:을|를)\s+체결{KO_END}"),
    _rule("ko_partner_nominal_headline", "PARTNER", "symmetric",
          rf"{MARK_A}(?:와|과|·|-)\s*{MARK_B}\s+업무협약\s+체결"),
    _rule("ko_supplier_object_first", "SUPPLY", "ab",
          rf"{MARK_A}{KO_SUBJECT}{KO_OBJECT}(?:을|를)\s+{MARK_B}(?:에게|에)\s+{KO_VERB}"),
    _rule("ko_supplier_to_contract", "SUPPLY", "ab",
          rf"{MARK_A}{KO_SUBJECT}{MARK_B}(?:에게|에)\s+(?:[0-9.,조억만천원달러$ ]+\s*규모의\s+)?{KO_OBJECT}(?:을|를)\s+공급하는\s+계약(?:을|를)\s+체결{KO_END}"),
    _rule("ko_supplier_order_amount", "SUPPLY", "ab",
          rf"{MARK_A}{KO_SUBJECT}{MARK_B}(?:로부터|에게서)\s+{KO_SITE_OBJECT}(?:을|를)\s+수주{KO_END}"),
    _rule("ko_holds_stake_unmarked", "INVEST", "ab",
          rf"{MARK_A}{KO_SUBJECT}{MARK_B}(?:의)?\s+지분{KO_STAKE_AMOUNT}\s+{KO_HOLDS}"),
    _rule("ko_holds_stake_largest", "INVEST", "ab",
          rf"{MARK_A}{KO_SUBJECT}{MARK_B}(?:의)?\s+지분{KO_STAKE_AMOUNT}(?:을|를)?\s+보유한\s+(?:최대\s*주주|대주주)(?:다|이다)"),
    _rule("ko_holds_stake_postparticle", "INVEST", "ab",
          rf"{MARK_A}{KO_SUBJECT}{MARK_B}(?:의)?\s+지분(?:을|를)\s+(?:약\s+)?[0-9]+(?:\.[0-9]+)?%\s+보유\s+중이다"),
    _rule("ko_majority_shareholder_flexible", "INVEST", "ba",
          rf"{MARK_A}의\s+최대\s*주주(?:는|가)\s+{MARK_B}(?:으로|로),?\s+지분{KO_STAKE_AMOUNT}(?:을|를)\s+{KO_HOLDS}"),
    _rule("ko_supplier_customer", "SUPPLY", "ab",
          rf"{MARK_A}{KO_SUBJECT}{MARK_B}(?:에게|에)\s+{KO_OBJECT}(?:을|를)\s+{KO_VERB}"),
    _rule("ko_supplier_operating_site", "SUPPLY", "ab",
          rf"{MARK_A}{KO_SUPPLY_SUBJECT}{MARK_B}{KO_CUSTOMER_SITE}(?:{KO_SITE_PRODUCT})?{KO_SITE_OBJECT}(?:을|를)\s+{KO_VERB}"),
    _rule("ko_customer_receives", "SUPPLY", "ba",
          rf"{MARK_A}{KO_SUBJECT}{MARK_B}(?:로부터|으로부터|에게서)\s+{KO_OBJECT}(?:을|를)\s+(?:공급받|납품받)(?:았다|는다|고\s+있다)"),
    _rule("ko_supplier_wins_order", "SUPPLY", "ab",
          rf"{MARK_A}{KO_SUBJECT}{MARK_B}(?:로부터|에게서)\s+{KO_OBJECT}(?:을|를)\s+수주{KO_END}"),
    _rule("ko_named_supplier", "SUPPLY", "ab",
          rf"{MARK_A}{KO_SUBJECT}{MARK_B}의\s+(?:{KO_OBJECT}\s+)?(?:공급업체|납품업체|공급사)(?:다|이다|였다)"),
    _rule("ko_relative_supplier", "SUPPLY", "ba",
          rf"{MARK_A}(?:에게|에)\s+{KO_OBJECT}(?:을|를)\s+(?:공급|납품)하는\s+{MARK_B}"),
    _rule("ko_partner_joint_subject", "PARTNER", "symmetric",
          rf"{MARK_A}(?:와|과)\s+{MARK_B}(?:는|은|가)\s+(?:{KO_OBJECT}\s+)?(?:협력\s+협약|업무협약|양해각서|MOU|제휴\s+계약)(?:을|를)\s+체결{KO_END}"),
    _rule("ko_partner_with", "PARTNER", "symmetric",
          rf"{MARK_A}{KO_SUBJECT}{MARK_B}(?:와|과)\s+(?:(?:{KO_OBJECT}\s+)?(?:업무협약|양해각서|MOU|제휴\s+계약)(?:을|를)\s+체결{KO_END}|(?:협력|제휴){KO_END})"),
    _rule("ko_partner_joint_work", "PARTNER", "symmetric",
          rf"{MARK_A}(?:와|과)\s+{MARK_B}(?:는|은|가)\s+{KO_OBJECT}(?:을|를)\s+공동(?:으로\s+)?(?:개발|연구|설립){KO_END}"),
    _rule("ko_partner_strengthens", "PARTNER", "symmetric",
          rf"{MARK_A}(?:와|과)\s+{MARK_B}{KO_SUBJECT}(?:합작사\s+설립\s+등\s+)?(?:상호\s+)?협력(?:\s+관계)?(?:을|를)\s+(?:강화|확대)(?:{KO_END}|하며\s+{KO_OBJECT}(?:을|를)\s+(?:완성|구축){KO_END})"),
    _rule("ko_competes_with", "COMPETE", "symmetric",
          rf"{MARK_A}{KO_SUBJECT}{MARK_B}(?:와|과)\s+(?:{KO_OBJECT}(?:에서|을\s+두고)\s+)?경쟁{KO_END}"),
    _rule("ko_competitors", "COMPETE", "symmetric",
          rf"{MARK_A}(?:와|과)\s+{MARK_B}(?:는|은|가)\s+(?:서로\s+)?경쟁(?:사|업체|관계)(?:다|이다)"),
    _rule("ko_competition_joint_action", "COMPETE", "symmetric",
          rf"{MARK_A}(?:와|과|,)\s+{MARK_B}(?:는|은|가)\s+(?:{KO_OBJECT}(?:에서|을\s+두고)\s+)?{KO_COMPETITION_ACTION}"),
    _rule("ko_competition_product_expansion", "COMPETE", "symmetric",
          rf"{MARK_A}(?:와|과)\s+{MARK_B}(?:는|은|가)\s+{KO_OBJECT}(?:을|를)\s+확대하며\s+{KO_COMPETITION_ACTION}"),
    _rule("ko_competition_with_action", "COMPETE", "symmetric",
          rf"{MARK_A}{KO_SUBJECT}{MARK_B}(?:와|과)\s+(?:{KO_OBJECT}(?:에서|을\s+두고)\s+)?{KO_COMPETITION_ACTION}"),
    _rule("ko_invests", "INVEST", "ab",
          rf"{MARK_A}{KO_SUBJECT}{MARK_B}에\s+(?:[0-9.,조억만천원달러$% ]+(?:을|를)\s+)?투자{KO_END}"),
    _rule("ko_stake_acquisition", "INVEST", "ab",
          rf"{MARK_A}{KO_SUBJECT}{MARK_B}의\s+지분(?:\s+[0-9.]+%)?(?:을|를)\s+(?:인수|취득|매입){KO_END}"),
    _rule("ko_holds_stake", "INVEST", "ab",
          rf"{MARK_A}{KO_SUBJECT}{MARK_B}(?:의)?\s+지분{KO_STAKE_AMOUNT}(?:을|를)\s+{KO_HOLDS}"),
    _rule("ko_majority_shareholder_holding", "INVEST", "ba",
          rf"{MARK_A}의\s+최대주주(?:는|가)\s+{MARK_B}(?:으로|로)\s+지분{KO_STAKE_AMOUNT}(?:을|를)\s+{KO_HOLDS}"),
    _rule("ko_acquires_company", "INVEST", "ab",
          rf"{MARK_A}{KO_SUBJECT}{MARK_B}(?:을|를)\s+인수{KO_END}"),
    _rule("en_supplies_to", "SUPPLY", "ab",
          rf"{MARK_A}\s+(?:supplies|supplied|provides|provided|sells|sold)\s+{EN_OBJECT}\s+to\s+{MARK_B}"),
    _rule("en_supplies_with", "SUPPLY", "ab",
          rf"{MARK_A}\s+(?:supplies|supplied|provides|provided)\s+{MARK_B}\s+with\s+{EN_OBJECT}"),
    _rule("en_buys_from", "SUPPLY", "ba",
          rf"{MARK_A}\s+(?:buys|bought|purchases|purchased|receives|received)\s+{EN_OBJECT}\s+from\s+{MARK_B}"),
    _rule("en_is_supplier", "SUPPLY", "ab",
          rf"{MARK_A}\s+(?:is|was)\s+(?:a|the)\s+(?:supplier|vendor)\s+(?:to|of)\s+{MARK_B}"),
    _rule("en_is_supplied_by", "SUPPLY", "ba",
          rf"{MARK_A}\s+(?:is|was)\s+supplied\s+by\s+{MARK_B}"),
    _rule("en_partner_with", "PARTNER", "symmetric",
          rf"{MARK_A}\s+(?:partnered|partners|collaborates|collaborated)\s+with\s+{MARK_B}(?:\s+on\s+{EN_OBJECT})?"),
    _rule("en_partner_joint_subject", "PARTNER", "symmetric",
          rf"{MARK_A}\s+and\s+{MARK_B}\s+(?:signed|sign|have\s+signed)\s+(?:a|an)\s+(?:partnership\s+agreement|cooperation\s+agreement|memorandum\s+of\s+understanding|MOU)"),
    _rule("en_competes_with", "COMPETE", "symmetric",
          rf"{MARK_A}\s+(?:competes|competed)\s+with\s+{MARK_B}(?:\s+(?:in|for)\s+{EN_OBJECT})?"),
    _rule("en_competitors", "COMPETE", "symmetric",
          rf"{MARK_A}\s+and\s+{MARK_B}\s+are\s+(?:direct\s+)?competitors"),
    _rule("en_invests", "INVEST", "ab",
          rf"{MARK_A}\s+(?:invested|invests|has\s+invested)\s+(?:[0-9.,$€£ ]+(?:million|billion|trillion|dollars)?\s+)?in\s+{MARK_B}"),
    _rule("en_stake_acquisition", "INVEST", "ab",
          rf"{MARK_A}\s+(?:acquired|bought|purchased)\s+(?:a|an|the)\s+(?:[0-9.]+%\s+)?stake\s+in\s+{MARK_B}"),
    _rule("en_holds_stake", "INVEST", "ab",
          rf"{MARK_A}\s+(?:holds|held|owns|owned)\s+(?:a\s+)?[0-9]+(?:\.[0-9]+)?%\s+(?:stake\s+in|of(?:\s+the\s+shares\s+of)?)\s+{MARK_B}"),
    _rule("en_majority_shareholder", "INVEST", "ab",
          rf"{MARK_A}\s+is\s+(?:the\s+)?(?:largest|majority)\s+shareholder\s+of\s+{MARK_B}"),
    _rule("en_acquires_company", "INVEST", "ab",
          rf"{MARK_A}\s+(?:acquired|acquires|bought|has\s+acquired)\s+{MARK_B}"),
)

# A small number of relation-bearing noun/relative clauses are independently
# explicit even inside a longer sentence. These require exactly two literal
# entity occurrences, complete grammatical roles, and the same whole-sentence
# negation/uncertainty/identity guards as full rules. No proximity fallbacks.
ATOMIC_RULES = (
    _rule("atom_ko_named_supplier", "SUPPLY", "ba",
          rf"{MARK_A}의\s+(?:(?:주요|핵심|독점)\s+)?(?:공급사|공급업체|납품업체)인\s+{MARK_B}"),
    _rule("atom_ko_named_customer", "SUPPLY", "ab",
          rf"{MARK_A}의\s+(?:(?:주요|핵심)\s+)?고객사인\s+{MARK_B}"),
    _rule("atom_ko_named_competitor", "COMPETE", "symmetric",
          rf"{MARK_A}의\s+(?:(?:주요|직접적인|최대)\s+)?경쟁사(?:인)?\s+{MARK_B}"),
    _rule("atom_ko_competes_relative", "COMPETE", "symmetric",
          rf"{MARK_A}(?:와|과)\s+(?:{KO_OBJECT}에서\s+)?경쟁하는\s+{MARK_B}"),
    _rule("atom_ko_holds_stake_relative", "INVEST", "ba",
          rf"{MARK_A}(?:의)?\s+지분\s+(?:약\s+)?[0-9]+(?:\.[0-9]+)?%를\s+보유한\s+{MARK_B}"),
    _rule("atom_en_competitor", "COMPETE", "symmetric",
          rf"{MARK_A}(?:['’]s)?\s+(?:(?:direct|biggest|main)\s+)?competitor,?\s+{MARK_B}"),
    _rule("atom_en_competitor_of", "COMPETE", "symmetric",
          rf"{MARK_A},?\s+(?:a|the)\s+(?:direct\s+)?competitor\s+(?:of|to)\s+{MARK_B}"),
    _rule("atom_en_supplier", "SUPPLY", "ba",
          rf"{MARK_A}(?:['’]s)?\s+(?:(?:key|main|exclusive)\s+)?supplier,?\s+{MARK_B}"),
)

NEGATION = re.compile(r"않|아니|없|거부|중단|철회|취소|결렬|무산|해지|안\s|못\s|\b(?:not|never|no|without|denied|denies|cancelled|canceled|terminated|rejected)\b", re.I)
UNCERTAINTY = re.compile(r"계획|예정|검토|추진|전망|가능성|소문|루머|추측|가정|조건부|(?:한다|된다)면|할\s*(?:수|것)|\b(?:will|would|could|may|might|plan|plans|planned|planning|considering|rumou?r|reportedly|alleged|allegedly|if|expects|expected|intends|proposed)\b", re.I)
BIOGRAPHY = re.compile(r"출신|전직|약력|퇴직|이직|경력|\b(?:formerly|former|biography|resume|résumé|alumnus|previously\s+worked)\b", re.I)
COMPARISON = re.compile(r"보다|대비|비교|반면|점유율이|\b(?:than|whereas|unlike|compared|versus|vs\.?|outperformed)\b", re.I)
INSTRUCTION = re.compile(r"프롬프트|출력하|지시문|가짜\s*JSON|\b(?:ignore\s+(?:previous|prior)|prompt|instruction|output\s+json|pretend)\b", re.I)
OTHER_CLAUSE = re.compile(r"(?:[가-힣A-Za-z0-9]+(?:은|는|이|가))\s+|\b(?:and|but|while|which|who|that|said|says|announced|announces)\b", re.I)
PARTICLE = re.compile(r"^(?:으로부터|로부터|에게서|에게|에서는|에서|으로|보다|은|는|이|가|와|과|의|에|을|를|로|도|만)(?=$|[^가-힣A-Za-z0-9_])")
LATIN_WORD = re.compile(r"[A-Za-z0-9_]")
# Crawled Korean articles often omit spaces before a Latin company name,
# date or section marker. A finite Korean sentence ending in 다. is a safe
# extra boundary. Do not generally split decimal numbers or English names.
BOUNDARY = re.compile(r"[.!?。！？]+(?=\s|[가-힣]|$)|(?<=다)\.(?=\S)|[\r\n]+")
REPORT_END = re.compile(rf"(?P<ending>했다|하였다|한다|있다|중이다)[\"”’']?고\s+{KO_TIME_SLOT}(?:밝혔다|전했다|발표했다|설명했다)$")
TIME_PREFIX = re.compile(rf"^(?:(?:{KO_TIME})\s+){{1,2}}")


def _clause_surface(masked):
    """Remove only a grammatical date/report envelope, never an actor.

    This is a matcher view, not evidence normalization: returned offsets and
    sentences remain the original bytes' decoded Unicode string. Reporting
    is an article assertion, not independent confirmation of its truth.
    """
    surface = masked.strip().rstrip(".!?。！？").strip()
    # Exchange-code annotations are not actors. Only exact code-shaped
    # parentheses following an explicit entity are removed in matcher view.
    surface = re.sub(r"(⟦[AB]⟧)\s*(?:\([0-9A-Z]{6}\)|\[[0-9A-Z]{6}\])", r"\1", surface)
    surface = TIME_PREFIX.sub("", surface, count=1)
    # A balanced whole-clause quote can be followed by the reporting ending.
    report = REPORT_END.search(surface)
    if report:
        if surface[:1] in ('"', '“', '‘', "'"):
            pairs = {'"': '"', '“': '”', '‘': '’', "'": "'"}
            between = surface[report.start() + len(report.group("ending")):report.end()]
            if not between.startswith(pairs[surface[0]] + "고"):
                return surface
            surface = surface[1:report.start()] + report.group("ending")
        else:
            # A closing quote without an opening quote is not stripped.
            between = surface[report.start() + len(report.group("ending")):report.end()]
            if between.startswith("고"):
                surface = surface[:report.start()] + report.group("ending")
    # Quoted agreement/product names are matcher punctuation, not actors.
    # Never remove quotes around an entity token or unbalanced quotations.
    for opening, closing in (("‘", "’"), ("“", "”"), ("'", "'"), ('"', '"')):
        surface = re.sub(re.escape(opening) + r"([^⟦⟧.!?\n]{1,180})" + re.escape(closing), r"\1", surface)
    return surface


def _entity_aliases(entities):
    if not isinstance(entities, (list, tuple)):
        raise TypeError("accepted_entities must be a list/tuple of entity mappings")
    by_alias, tickers = defaultdict(set), set()
    for entity in entities:
        if not isinstance(entity, dict):
            raise TypeError("Each accepted entity must be a mapping")
        ticker, name = entity.get("ticker"), entity.get("company_name")
        if not isinstance(ticker, str) or not ticker.strip() or ticker in tickers:
            raise ValueError("Each entity needs a unique nonempty ticker")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Each entity needs a nonempty company_name")
        aliases = entity.get("aliases", [])
        if not isinstance(aliases, (list, tuple)) or any(not isinstance(a, str) or not a.strip() for a in aliases):
            raise ValueError("aliases must be nonempty strings")
        tickers.add(ticker)
        for alias in [name, *aliases]:
            if "\n" in alias or "\r" in alias or MARK_A in alias or MARK_B in alias:
                raise ValueError("Aliases may not contain newlines or internal markers")
            by_alias[alias.strip().casefold()].add(ticker)
    return by_alias


def _mentions(text, aliases):
    # Match longer names first. A short accepted alias never steals the prefix
    # of a longer accepted company. Extension of an unregistered alias is NOT
    # resolved to its parent company, even when a ticker appears nearby.
    found = []
    for alias in sorted(aliases, key=lambda x: (-len(x), x)):
        for match in re.finditer(re.escape(alias), text, re.I):
            start, end = match.span()
            if start and (text[start - 1].isalnum() or text[start - 1] == "_"):
                continue
            suffix = text[end:]
            extended = bool(suffix and (suffix[0].isalnum() or suffix[0] == "_")
                            and not PARTICLE.match(suffix))
            # Never map an ASCII substring embedded in a larger Latin name.
            if extended and LATIN_WORD.search(alias[-1:]) and LATIN_WORD.match(suffix):
                continue
            found.append({"start": start, "end": end, "quote": text[start:end],
                          "tickers": sorted(aliases[alias]), "ambiguous": len(aliases[alias]) != 1,
                          "extended": extended})
    result = []
    for item in sorted(found, key=lambda x: (x["start"], -(x["end"] - x["start"]))):
        if any(item["start"] < prev["end"] and item["end"] > prev["start"] for prev in result):
            continue
        result.append(item)
    return result


def _sentences(text, mentions):
    start = 0
    for boundary in BOUNDARY.finditer(text):
        p = boundary.start()
        if text[p] == ".":
            # Company suffixes / names and ordinary English abbreviations.
            if any(m["start"] <= p < m["end"] for m in mentions):
                continue
            if re.search(r"\b(?:Inc|Corp|Co|Ltd|Mr|Mrs|Dr|U\.S)\.$", text[max(0, p - 12):p + 1], re.I):
                continue
        end = p if text[p] in "\r\n" else boundary.end()
        left, right = start, end
        while left < right and text[left].isspace():
            left += 1
        while right > left and text[right - 1].isspace():
            right -= 1
        if left < right:
            yield left, right, text[left:right]
        start = boundary.end()
    left, right = start, len(text)
    while left < right and text[left].isspace():
        left += 1
    while right > left and text[right - 1].isspace():
        right -= 1
    if left < right:
        yield left, right, text[left:right]


def extract_grounded_relations(title, body, accepted_entities, *, affiliate_pairs=()):
    """Return literal ``relationships`` and non-edge ``review_candidates``.

    accepted_entities: [{ticker, company_name, aliases: [literal strings]}].
    Aliases must already be approved identities; this function never invents
    corporate ownership, name changes or industry equivalence. affiliate_pairs
    optionally supplies known company pairs whose competition claim should be
    reviewed. Uncertain/planned/negative statements remain review evidence.
    """
    if not isinstance(title, str) or not isinstance(body, str):
        raise TypeError("title and body must be strings")
    text = title + "\n" + body
    aliases = _entity_aliases(accepted_entities)
    mentions = _mentions(text, aliases)
    affiliates = set()
    for pair in affiliate_pairs:
        if not isinstance(pair, (list, tuple, set, frozenset)) or len(pair) != 2:
            raise ValueError("affiliate_pairs must contain two distinct ticker strings")
        if any(not isinstance(t, str) or not t for t in pair) or len(set(pair)) != 2:
            raise ValueError("Invalid affiliate pair")
        affiliates.add(frozenset(pair))
    edges, reviews, flags = [], [], set()
    for start, end, sentence in _sentences(text, mentions):
        hits = [m for m in mentions if start <= m["start"] and m["end"] <= end]
        tickers = sorted({t for m in hits for t in m["tickers"]})
        if len(tickers) < 2:
            continue
        common = {"sentence": sentence, "sentence_start": start, "sentence_end": end,
                  "offset_basis": OFFSET_BASIS,
                  "evidence_source": "title" if end <= len(title) else "body"}

        def review(reason):
            reviews.append({**common, "candidate_tickers": tickers, "decision": "review",
                            "reason": reason, "is_asserted_relationship": False})
            flags.add(reason)

        if any(m["ambiguous"] for m in hits):
            review("ambiguous_alias_identity")
            continue
        if any(m["extended"] for m in hits):
            review("group_or_other_entity_extension")
            continue
        if len(tickers) != 2:
            review("multi_party_or_repeated_mentions")
            continue
        blocked = next((name for name, pattern in (
            ("instruction_like_text", INSTRUCTION), ("negated_or_ended_relation", NEGATION),
            ("uncertain_or_planned_relation", UNCERTAINTY), ("biography_context", BIOGRAPHY),
            ("comparison_context", COMPARISON)) if pattern.search(sentence)), None)
        if blocked:
            review(blocked)
            continue
        first = hits[0]
        second = next(m for m in hits if m["tickers"][0] != first["tickers"][0])
        # Resolve all repeats to their known ticker, preserving the first
        # grammatical occurrences as the endpoint spans. Every remaining
        # marker must be consumed by an explicit grammar role (e.g. own site).
        markers = {first["tickers"][0]: MARK_A, second["tickers"][0]: MARK_B}
        pieces, previous = [], start
        for hit in hits:
            pieces.extend((text[previous:hit["start"]], markers[hit["tickers"][0]]))
            previous = hit["end"]
        pieces.append(text[previous:end])
        masked = "".join(pieces)
        masked = _clause_surface(masked)
        # A company alias ending in Inc. keeps its own period inside the token.
        matches = [(name, rel, direction) for name, rel, direction, pattern in RULES
                   if pattern.fullmatch(masked)]
        atomic_match = False
        if not matches and len(hits) == 2 and '?' not in sentence:
            matches = [(name, rel, direction) for name, rel, direction, pattern in ATOMIC_RULES
                       if pattern.search(masked)]
            atomic_match = bool(matches)
        if not matches:
            review("multi_party_or_repeated_mentions" if len(hits) > 2 else "co_mention_without_explicit_supported_relation")
            continue
        # For a regex object slot, an unknown third subject remains possible.
        # Remove only the two known subject particles and the grammar's joint
        # subject/with connector before checking additional clause markers.
        remainder = masked
        if matches[0][0] == "ko_supplier_operating_site":
            remainder = re.sub(rf"{MARK_A}{KO_DIVISION}{KO_SUBJECT}", "", remainder)
        if matches[0][0].startswith("ko_majority_shareholder"):
            # Already full-matched ownership syntax: 최대주주는 is the role
            # label for B, not an unidentified third grammatical actor.
            remainder = re.sub(rf"{MARK_A}의\s+최대\s*주주(?:는|가)", MARK_A, remainder)
        if "role_envelope" in matches[0][0]:
            remainder = re.sub(KO_OWN_SITE, "", remainder)
            remainder = re.sub(KO_UNNAMED_SITE, "", remainder)
        remainder = re.sub(r"⟦[AB]⟧(?:은|는|이|가|와|과|도)?", "", remainder)
        remainder = re.sub(r"^\s*and\s*", "", remainder, flags=re.I)
        if "role_envelope" in matches[0][0] and re.search(r"\b[가-힣A-Za-z0-9]+(?:와|과)\s+", remainder):
            review("additional_conjoined_counterparty")
            continue
        if matches[0][0] == "ko_relative_supplier":
            remainder = re.sub(r"(?:공급|납품)하는\s*$", "", remainder)
        if matches[0][0] == "ko_supplier_to_contract":
            remainder = remainder.replace("공급하는", "공급")
        if not atomic_match and OTHER_CLAUSE.search(remainder):
            review("additional_clause_or_unresolved_actor")
            continue
        signatures = {(r, d) for _, r, d in matches}
        if len(signatures) != 1:
            review("ambiguous_relation_or_direction")
            continue
        pattern_name, rel_type, direction = matches[0]
        if rel_type not in RELATION_TYPES:
            raise AssertionError("Unexpected public relationship type")
        if pattern_name.endswith("acquires_company"):
            subtype = "company_acquisition"
        elif "stake_acquisition" in pattern_name:
            subtype = "stake_acquisition"
        elif "holds_stake" in pattern_name or "majority_shareholder" in pattern_name:
            subtype = "shareholding"
        else:
            subtype = {"SUPPLY": "supplier_customer", "INVEST": "investment",
                       "PARTNER": "business_cooperation", "COMPETE": "market_competition"}[rel_type]
        src, dst = (second, first) if direction == "ba" else (first, second)
        if direction == "symmetric" and src["tickers"][0] > dst["tickers"][0]:
            src, dst = dst, src
        if rel_type == "COMPETE" and frozenset(tickers) in affiliates:
            review("competition_conflicts_with_affiliate_evidence")
            continue
        edges.append({**common, "src_ticker": src["tickers"][0], "dst_ticker": dst["tickers"][0],
                      "rel_type": rel_type, "directed": direction != "symmetric",
                      "relation_subtype": subtype,
                      "direction_semantics": ("buyer_to_target" if subtype == "company_acquisition" else
                                              {"SUPPLY": "supplier_to_customer", "INVEST": "investor_to_investee",
                                               "PARTNER": "symmetric", "COMPETE": "symmetric"}[rel_type]),
                      "source_start": src["start"], "source_end": src["end"], "source_quote": src["quote"],
                      "target_start": dst["start"], "target_end": dst["end"], "target_quote": dst["quote"],
                      "pattern": pattern_name, "assertion_status": "asserted_in_article",
                      "temporal_validity": "not_established", "economic_effect_verified": False})
    return {"schema": SCHEMA, "policy_version": POLICY_VERSION, "offset_basis": OFFSET_BASIS,
            "relationships": edges, "review_candidates": reviews, "flags": sorted(flags)}
