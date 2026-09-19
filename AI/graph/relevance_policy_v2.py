"""Specificity/attribution revision of the frozen two-pass relevance policy.

The 100 assisted selections are DEVELOPMENT feedback, not blind human gold.
No article IDs, real company aliases or reference answers are embedded here.
The original prompt and v1 policy remain untouched. Output parsing, lossless
quotes, abstention and the two independent prompts use the existing contract.
This revision clarifies what economic information is new and attributable; it
does not label realized returns, estimate causality or promise price movement.
Historical identity facts must be supplied with dated provenance, never recalled
from model knowledge or inferred from a current company dictionary.
"""
from __future__ import annotations

import relevance_two_stage_policy as previous

POLICY_VERSION = "specific_event_attribution_two_stage_v2"
parse = previous.parse
decide = previous.decide

SPECIFICITY = '''

[구체성·귀속 보완 규칙]
판정 단위는 기사 전체가 아니라 '이 기사와 대상 기업'의 관계다. 아래 조건을 마음속으로
점검하되 출력은 decision/evidence_ids 두 키만 유지한다.

1. 대상 식별: 기사 속 회사가 대상 법인 자신인지 먼저 확인한다. 서로 다른 법인의 그룹명,
브랜드, 해외 현지법인, 투자사 이름만으로 동일시하지 마라. 기사 또는 제공된 시점 검증
company_context가 지배/사업 관계를 명시하면 그 범위에서 간접 관련성을 인정할 수 있다.
모회사로 연결하더라도 자회사의 매출/소송/장애를 모회사 자체의 사건으로 바꾸지 마라.
과거 사명은 당시 동일 법인·종목이라는 제공 근거가 있을 때만 연결한다. 분할 전 기업과
분할 후 기업, 매각 전후 소유주는 자동 연결하지 마라. 정보가 없으면 hold를 유지한다.

2. 구체 경제 정보: 수치가 반드시 있어야 하는 것은 아니다. 대상의 새 계약·제품·서비스,
실적 발표·전망 수정·자금조달·생산/영업 차질·수요/원가 변화·새 규제/소송은 근거가 될 수 있다.
기사의 주인공이 다른 회사이거나 여러 기업의 공시를 모은 기사라도 대상의 새 경제 사실이
구체적으로 담겨 있으면 검토한다. 수상·홍보·행사 자체만으로 채택하지 않되 그 안에 새 실적,
실제 사업 변화가 별도로 있으면 그 정보를 평가한다. 알려진 계약의 실제 인도·이행도 지금의
경제 활동으로 명시되면 검토하되 신규 수주라고 바꾸지 마라.

3. 산업·거시: 발표된 새 산업/거시 정보의 적용 범위와 대상의 실제 사업 노출이 기사/제공
자료에 연결돼야 한다. '경기 소식이라 모든 기업에 영향'은 근거가 아니다. 통계에서 명시적으로
제외한 업종을 포함한 것처럼 해석하지 마라. 별도 일정표·추천 목록의 회사 이름을 제목의
산업 전망에 붙이지 마라. 단순 '실적: 회사명'만으로 실적 수치/변화나 새 발표를 만들지 마라.

4. 시장 심리: 현재의 구체적인 원인 사건이 있고 원문이 대상에 대한 투자자 우려·수요·평가
변화를 그 사건과 명시적으로 연결하면 간접 관련성을 검토할 수 있다. 실적 손실이나 영업
장애까지 확인됐다고 확대하지 마라. 주가 상승/하락 숫자만 있거나 막연한 기대뿐이면
새 경제 원인이나 다음 주가 방향을 만들어 채택하지 마라.

5. 신규성: 기사 발행일과 사건 발생일은 다르다. 과거 투자자 명단, 인물 약력, 회고 인터뷰,
일반 재무분석·서평, 오래된 사업 소개는 새 투자/계약/발표가 아니다. 다만 같은 기사에 실제
새 경제 사건이 있으면 그것을 따로 평가한다. '향후 가능하다'는 일반 전망과 지금 발표된
계획/협상/예측을 구분하라. 새 계획 자체는 정보지만 실현 확정으로 바꾸지 마라.

6. 제외/보류: 원문을 모두 읽고 대상은 약력·도구·추천·과거 투자자 목록에만 나오거나,
대상에게 연결된 부분이 명백히 과거 회고뿐이면 exclude다. 경제 사건은 있으나 대상의 법인
귀속·산업 노출·현재성 연결이 부족하면 hold다. 판단보류를 억지로 해소하지 마라.
사용자 취향, 예상 정답, 채택 목표 비율을 추측하지 마라. 채택은 주가 변동의 증명이 아니다.
'''


def messages(row, prompt, stage):
    """Retain complete input, metadata isolation and a hidden first-stage answer."""
    result = previous.messages(row, prompt, stage)
    result[0]["content"] += SPECIFICITY
    if stage == 1:
        result[0]["content"] += "\n1단계에서는 경제 관련성만 판정한다. 위 신규성의 최종 제외 판단은 2단계에서 한다.\n"
    return result
