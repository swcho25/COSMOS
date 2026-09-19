# 사용자 검수 이후 관련성 기준 보완 실험

사용자가 AI 추천을 보고 고른 100건(채택69/제외31)을 개발 피드백으로 사용했다.
추천과 다른13건의 이유는 수집하지 않았으므로 이유를 추정하거나 선택을 수정하지 않았다.
최종 비교 결과와 채택 여부는 `RELEVANCE_POLICY_V2_RESULTS.md`를 참고한다.

## 바꾼 것

`relevance_policy_v2.py`는 원문 프롬프트와 기존 두 단계 출력 계약을 유지하면서 다음을 명시한다.

- 같은 그룹·브랜드·과거 사명과 동일 법인/종목을 구분한다. 회사 관계는 당시 자료가 필요하다.
- 산업/거시 지표의 적용 업종과 기업의 실제 사업 노출을 연결한다. 모든 기업으로 확대하지 않는다.
- 명시된 원인 사건에 대한 시장 심리 보도와 직접 영업 피해를 구분한다. 가격 숫자만으로 채택하지 않는다.
- 누적 투자자·약력·일정표·서평·회고와 새 계약·실적·소송·공개자료를 구분한다.
- 공시 모음, 다른 기업이 제목에 나온 기사에도 대상의 구체 경제 정보가 있으면 검토한다.

이것은 모델에 주는 지시이며 의미 검증을 보장하는 코드 규칙이 아니다. 원문 인용 검증은
인용이 존재함을 확인할 뿐 법인 귀속이나 신규성까지 증명하지 않는다. 같은 Qwen의 두 번
판정은 독립된 두 전문가가 아니다. 지시를 따르지 않는 사례도 평가에 포함한다.

## 비교 구분

1. **개발13건:** 기존 두 단계/보완 두 단계를 같은 기사로 실행한다. 기존 AI 추천은 Codex의
   참고 판단이므로 로컬 Qwen 기준선으로 재사용하지 않고 v1을 별도로 실행한다.
2. **새32건:** 기존 검토580기사와 기존 val/test11,969기사의 ID·본문·URL 중복을 배제한다.
   2020–2022 train 기사, 정제 본문200–6000자, 국내16/해외16 고정 표본이다.
   새 기사/참고판정/출력 이전에 정책을 동결하고, 두 모델 모두 company_context=null로 비교한다.
3. **개발 문맥 실험:** 공식 공시·보존 발표에서 확인한 과거 사업/자회사 관측사실을 개발 기사
   일부에만 추가한다. 공개일과 결산일을 분리하고 기사 이후 자료를 배제한다. 이전 관측부터
   기사일까지 관계가 불변이었다는 보장은 하지 않는다. 전체 기업 이력 사전이 아니다.

새32건 참고판정도 별도 AI가 원문 전체를 읽은 결과다. 사람 정확도나 새 미래기간의 검증으로
해석하지 않는다. 표본의 길이·지역 구성은 모집단을 대표하지 않는다. 사용자100건은 독립
사람 정답으로 재사용하지 않는다. GNN 방향/강도 라벨이나 실제 수익률을 사용하지 않는다.

## 실행 및 재현

개발 문맥 변환기는 `prepare_relevance_context.py`다. 검토된 연구파일 SHA와 개발13건을 고정해
5건에만 문맥을 추가하며 기사 이후 공개자료·다른 기사·원문 변경을 거부한다. 이는 해당 연구
재현용 도구이고 일반적인 기업 이력 Loader가 아니다. `context_manifest.json`이 있어야 완료다.
실제 GPU 문맥 비교는 그중 문맥이 추가된5건만 `context_only_inputs.jsonl`로 실행했다.

기존 모델 revision과 NF4, attention adapter, 시드, T=0.7/top_p=0.8/top_k=20,
6144 입력/768 출력 토큰 제한을 유지한다. 본문 잘림과 재시도는 없으며 초과/오류는 보류다.
길어진 지시 때문에 더 많은 기사가 길이 제한에 걸릴 수 있고 이것도 비교에 포함한다.
GPU 추론은 한 프로세스씩 실행한다. 모델 로딩 시간은 기사 추론 시간에서 제외된다.

```powershell
$data = 'AI/graph/artifacts/relevance_policy_v2_data_20260917_v1'
$run = 'AI/graph/artifacts/relevance_policy_v2_run_20260917_v1'
AI/ner/.venv/Scripts/python.exe -X utf8 AI/graph/run_relevance_two_stage.py --inputs "$data/new_inputs.jsonl" --out "$run/baseline"
AI/ner/.venv/Scripts/python.exe -X utf8 AI/graph/run_relevance_policy_v2.py --inputs "$data/new_inputs.jsonl" --out "$run/revised"
AI/ner/.venv/Scripts/python.exe -X utf8 AI/graph/compare_relevance_policy_v2.py --inputs "$data/new_inputs.jsonl" --baseline "$run/baseline" --revised "$run/revised" --references "$data/independent_reference_01.jsonl" "$data/independent_reference_02.jsonl" --out "$run/comparison"
```

원래 `run_relevance_two_stage.py`/`relevance_two_stage_policy.py`는 재현용으로 보존했다.
새 runner는 정책 import·의존 해시·protocol schema 외에 v1 추론 코드를 유지한다.
두 결과의 행 `policy=two_stage`는 같으므로 반드시 각 protocol의 schema와 code_hashes로
버전을 구분한다. 입력 해시·옵션·코드가 바뀐 재개는 거부한다.

모델/기사/판정은 Git 제외인 `local_models/`, `artifacts/`에 저장한다. 개발 원본과 사용자
내보내기는 불변 보존한다. 이번 작업에는 GNN/LLM 재학습, 서비스 반영, HDFS 재적재가 없다.
