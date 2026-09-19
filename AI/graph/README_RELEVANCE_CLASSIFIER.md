# 뉴스·기업 관련성 분류기 파일럿

이 파일럿은 기존 사용자의 채택/제외 100건으로 작은 분류기를 학습해,
아직 검토하지 않은 기사에서 Qwen 2단계 선별과 비교한다.
주가 방향 예측이나 GNN 학습이 아니다. 실측 결론은 `RELEVANCE_CLASSIFIER_RESULTS.md`에 기록한다.

## 입력과 학습

- `prepare_relevance_classifier_data.py`: 사용자가 AI 추천을 보고 선택한 100건을 보존한다.
  원문 ID·URL·정규화 본문의 전이적 중복 그룹을 먼저 만든 뒤 train/dev를 분리한다.
  이번 데이터는 80건(채택55/제외25), 20건(채택14/제외6)이다.
- 모델 입력은 기업명, 제목, 정제 본문뿐이다. 선택/이유/추천/수익률/전용 날짜·종목 코드 필드는
  입력하지 않는다. 원문 속 숫자·날짜는 보존하고 `sample_id`는 결합 키로만 사용한다.
- `relevance_classifier.py`: 기존 번들의 multilingual MiniLM encoder를 동결한다.
  기업명과 본문 조각을 쌍으로 넣고, 전체 기사 토큰을 겹침 없는 최대256토큰 창으로 처리한다.
  모든 조각의 원문 토큰을 합쳐 최초 토큰열과 정확히 같은지 확인한다.
- 384차원 창 임베딩의 가중 평균 및 차원별 최대값을 각각 정규화한 768차원 특징에
  로지스틱 회귀를 학습한다. **언어 모델 전체나 CrossEncoder를 미세조정한 것이 아니다.**
- `C = 0.1, 1, 10` 중 dev 클래스균형 log loss가 가장 작은 값을 고른다. 동률은 작은 C.
  클래스 균형 가중치를 사용하므로 출력 score를 실제 채택 확률이나 정답 확률로 해석하지 않는다.

## 채택·제외·보류

개발자료만으로 임계값을 선택하고 새 평가자료를 열기 전에 모델·설정을 동결한다.

- 채택 후보 임계값: 0.50~0.95, 간격0.05. dev에서 맞게 채택한 표본이 3건 이상이고
  제외 정답의 오채택이 0건이어야 한다. TP가 가장 많고 그 안에서 가장 높은 임계값을 사용한다.
- 제외 후보 임계값: 0.05~0.45, 간격0.05. dev에서 맞게 제외한 표본이 3건 이상이고
  채택 정답의 오제외가 0건이어야 한다. TN이 가장 많고 그 안에서 가장 낮은 임계값을 사용한다.
- 조건을 충족하지 못한 방향은 비활성화한다. 두 방향이 비활성화되면 전건 보류한다.
  보류를 제외 정답으로 바꾸거나 평가 결과를 보고 임계값을 낮추지 않는다.
- 보류 학습 정답은 없다. 보류는 별도 학습 클래스가 아니라 결정 정책이다.

이 기준은 작은 파일럿의 오류 제한이며 모집단에서 오류0을 보장하지 않는다.
개발20건을 C와 임계값 선택에 함께 사용한 낙관 편향이 있다.

## 비교 절차

`prepare_relevance_classifier_evaluation.py`는 기존 표본 추출기를 수정하지 않고 호출한다.
이미 검토한654기사와 가격예측 val/test11969기사를 ID·본문·URL로 제외하고,
국내 출처16·해외 출처16기사(2020~2022, 본문200~6000자)를 고정 해시로 선택한다.
같은 해시 seed에서 제외 목록을 확장한 후속 표본이며 독립 무작위 반복실험은 아니다.

두 AI 검토자가 모델 출력을 보지 않고 각각16건의 원문 전체를 읽어 참고판정을 작성한다.
이것은 **사람 정답이 아니다**. 이후 classifier와 기존 `run_relevance_two_stage.py`의
Qwen v1을 같은32건에서 비교한다. 모델 로딩을 포함한 전체 wall time과 개별 기사 처리시간은 구분한다.
두 GPU 작업은 순차 실행한다.

`evaluate_relevance_classifier.py`는 저장된 float32 특징과 계수를 float64 sigmoid로 다시 계산하고,
dev 임계값, 원천/코드 SHA, source identity, 원문 인용, LLM raw 출력을 확인한다.
전건채택·전건제외·전건보류 기준선과 함께 오채택·채택누락·오제외·보류를 보고한다.
저장 특징 검증은 encoder 추론 자체를 재실행한 검증과 다르다.

## 실행

저장소 루트, PowerShell, 기존 `AI/ner/.venv/Scripts/python.exe -X utf8`만 사용한다.
각 `--out`은 새 디렉터리여야 하며 기존 실험을 덮어쓰지 않는다.

```powershell
AI/ner/.venv/Scripts/python.exe -X utf8 AI/graph/relevance_classifier.py train --seed AI/graph/artifacts/relevance_classifier_v1_20260917_v1/seed_data --out AI/graph/artifacts/relevance_classifier_v1_20260917_v1/trained
AI/ner/.venv/Scripts/python.exe -X utf8 AI/graph/prepare_relevance_classifier_evaluation.py
AI/ner/.venv/Scripts/python.exe -X utf8 AI/graph/relevance_classifier.py predict --inputs AI/graph/artifacts/relevance_classifier_v1_20260917_v1/evaluation_data/new_inputs.jsonl --model-dir AI/graph/artifacts/relevance_classifier_v1_20260917_v1/trained --out AI/graph/artifacts/relevance_classifier_v1_20260917_v1/classifier_run
AI/ner/.venv/Scripts/python.exe -X utf8 AI/graph/run_relevance_two_stage.py --inputs AI/graph/artifacts/relevance_classifier_v1_20260917_v1/evaluation_data/new_inputs.jsonl --out AI/graph/artifacts/relevance_classifier_v1_20260917_v1/llm_baseline
AI/ner/.venv/Scripts/python.exe -X utf8 AI/graph/evaluate_relevance_classifier.py --inputs AI/graph/artifacts/relevance_classifier_v1_20260917_v1/evaluation_data/new_inputs.jsonl --classifier-run AI/graph/artifacts/relevance_classifier_v1_20260917_v1/classifier_run --baseline-run AI/graph/artifacts/relevance_classifier_v1_20260917_v1/llm_baseline --references AI/graph/artifacts/relevance_classifier_v1_20260917_v1/refs_01.jsonl AI/graph/artifacts/relevance_classifier_v1_20260917_v1/refs_02.jsonl --out AI/graph/artifacts/relevance_classifier_v1_20260917_v1/comparison
```

실제 실행에서는 **학습 후, 새 평가 추출 전에** `evaluation_plan.json`으로 계획·정책·모델을
동결했으며 AI 참고판정을 모델 출력과 분리했다. 위 명령만 연달아 실행한다고 그 절차가 보장되지는 않는다.
참고판정은 별도 작성·검증해야 하며 이 README는 재실행을 통한 새 평가를 허용하는 근거가 아니다.

## 산출물과 한계

실험 산출물은 Git에서 제외되는 `artifacts/relevance_classifier_v1_20260917_v1/`에 있다.
`trained/model.json`은 선형 계수만 담는다. 실제 추론에는 같은 encoder/tokenizer,
코드, 전처리, `training_manifest.json`의 선택 설정이 함께 필요하다.
기존 GNN 번들과 분류기 실험 산출물은 별개다.

100건은 AI 추천을 본 사용자 선택이며 독립 정답집이 아니다. 기업의 현재 표시명이
과거 법인·계열사·사업 관계를 증명하지 않는다. 전체 토큰을 읽었다고 경제적 경로를
정확히 이해하는 것도 아니다. 출처지역/본문길이를 제한한32건으로 전체 뉴스 정확도를 주장하지 않는다.
채택은 미래 주가변동의 보장이나 인과적 영향 판정이 아니다.
