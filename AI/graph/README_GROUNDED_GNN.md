# 근거 문맥 기반 GNN 재학습 실험

**2026-09-19 완료:** 9회 학습과 독립 검증을 마쳤다. 새 모델의 성능 개선은 확인되지 않았다. 최종 수치·기존 모델 비교·검증 보고서 수정 이력은 [결과 문서](RESULTS_GROUNDED_GNN_20260919.md)를 먼저 확인한다. 최신 후속 검증 상태는 `training/independent_verification_stage_v2.json`이며, 원래 실패 기록은 보존했다. 순위 상관 12개의 NaN만 별도 `independent_verification.corrected_metrics.csv`에서 수정했고 정확도·F1·모델·예측은 바꾸지 않았다.

이번 산출물은 기존 모델을 교체하지 않는 연구 비교다. 선별 B와 근거 후보의 품질이 검증됐다는 뜻이 아니며, 가격 방향 정확도나 간접 영향의 개선을 미리 주장하지 않는다. 기존 번들·KG·평가 파일은 수정하지 않는다.

## 입력과 분모

입력 KG는 `artifacts/kg_grounded_full_20260918_v1`의 완료된 읽기 전용 SQLite다. 219,790개 기사, 246,264개 `candidate_ready` 뉴스–기업 쌍 중 2020년 이후 자료를 사용하고, 기존 202개 기업 및 실제 가격 라벨 제공 범위를 적용했다.

최종 학습 입력은 `artifacts/grounded_gnn_20260919_v1/dataset_v3`다. 정제 전 부모 입력과 전체 인코딩 캐시는 별도 `dataset_v2`에 보존한다.

| 구간 | 기사 | 가격 감독 쌍 | 입력 문맥 쌍 |
|---|---:|---:|---:|
| 학습 2020–2022 | 99,407 | 111,376 | 112,671 |
| 검증 2023 | 36,074 | 40,627 | 40,750 |
| 보조 최종 평가 2024–2026-09-11 | 33,210 | 36,518 | 36,654 |

가격 라벨이 없는 기업도 같은 기사에 유효한 감독 기업이 있으면 입력 문맥·감성·언급 정보는 유지한다. 이러한 입력 1,565개는 loss mask만 꺼져 있다. 모든 target의 라벨이 없는 기사는 학습/가격 평가의 모집단에서 제외된다. 이 차이를 회귀 테스트로 고정했다.

학습 전에 추천·광고 문맥 12기사와 시점 미확인 가격표 4기사를 원문 대조해 보수적으로 격리했다. train 6기사·8쌍은 학습과 train prior에서 제외했다. test 10기사·11쌍은 전체 36,518쌍 분모에 그대로 두고 미응답을 오답으로 계산한다(예측 가능 36,507쌍). val과 legacy val은 바뀌지 않았다. 가격표 4건의 미래 정보 유출이 입증됐다는 뜻은 아니며, 기사 자체의 영구적 무관 판정도 아니다.

`derive_grounded_training.py`가 최종 통합 그래프 v2와 고정 16기사 검수 정책을 적용했다. 별도 derivation protocol/receipt가 부모·정책·그래프·변경한 mask를 결속한다. 인코딩은 부모 v2의 원문 문맥/feature bank를 설명하는 기존 protocol 그대로이며 새 mask를 인코딩한 것처럼 기록하지 않는다. 격리된 문맥의 사용하지 않는 임베딩도 부모와 동일하게 보존한다.

기존 2023 직접 평가 4,813쌍은 `legacy_val`로 별도 보존한다. 기존 표현으로 예측 가능한 4,743쌍과 미응답 70쌍을 모두 분모에 포함하며 체크포인트 선택에 쓰지 않는다. 신규·기존 표현의 분포가 달라 이 결과는 보조 비교다. 2023년 및 2024–2026년 자료는 과거 실험에서 이미 노출되었으므로 새 독립 테스트가 아니다.

첫 `dataset`은 미래 라벨 유무가 입력 기업을 줄이는 결함을 발견하여 폐기된 준비 시도다. `SUPERSEDED.json`, `superseded_input_gate_audit.json`, 당시 코드가 보존되어 있으며 인코딩·학습에는 사용하지 않는다. `dataset_v2`는 그 오류를 고쳤으며 가격 pair와 감독 mask의 SHA는 첫 시도와 동일하다.

## 표현과 시간 규칙

- 기사 전체에 공통 감성을 부여하지 않고, 저장된 기업별 정제 근거 문맥과 기존 기업별 금융 감성 확률을 사용한다. 미분석/주체 모호/상반 문맥 감성은 `[0,0,0]` 결측값이다. 중립 확률로 채우지 않는다.
- 로컬 고정 multilingual MiniLM을 사용한다. 모든 원 토큰을 겹치지 않는 256토큰 이하 창으로 인코딩하고, special token을 제외한 내용 토큰 가중 평균 → L2 정규화 → 기존 train-only PCA64를 적용한다. 문맥 뒷부분을 버리지 않는다.
- 현재 Transformers의 일반 overflow API가 전체 원문을 반환하지 않는 경우를 발견했다. 실행에는 **`encode_grounded_features.py`**를 사용한다. 낮은 수준의 `Encoding` API로 창을 만들고, 창의 내용 token ID들을 이으면 원문 전체 token ID와 정확히 같아야 한다. 기존 준비 모듈의 구형 `encode` 하위 명령은 사용하지 않는다.
- 실패한 초기 인코딩 API 시도의 코드·protocol·정상 첫 256문맥 chunk는 `failed_encoding_api_attempt_001`에 보존한다. 수정한 인코더의 첫 512문맥은 전체 토큰 검사와 기존 정상 256개 결과 대조를 통과했다.
- 명시된 시간대는 UTC로 바꿔 보존한다. 시간대 없는 날짜/시각은 해당 날짜가 전 세계에서 끝난 다음 날 12:00 UTC로 처리한다. 기업 시장으로 뉴스 출처 시간대를 추정하지 않는다.
- 가격 라벨은 기존 수정주가·시장 지수 잔차의 1일/3일 정의와 고정 임계값·강도 스케일을 유지한다. 라벨 종료일이 다음 split으로 넘어가면 감독에서 제외한다.
- 같은 기사/원문/URL의 train–heldout 중복은 제거하고, 새 val의 test 중복도 격리한다. 기존 고정 `legacy_val`은 바꾸지 않는다.

## 그래프와 비교

3개 arm을 같은 입력·가격 라벨·3개 seed로 각각 학습한다(총 9회).

1. `candidate`: 근거 문맥/감성 + 기존 연도별 그래프 + 과거 공개된 뉴스·공시 관계.
2. `no_news`: 문맥 임베딩/감성만 0으로 바꾸며 대상 기업·언급 메타데이터·그래프는 유지한다.
3. `no_new_graph`: 같은 문맥/감성을 사용하되 새 관계 추가 없이 기존 연도별 그래프만 사용한다.

각 기사보다 엄격히 앞선 연간 스냅샷을 선택하고, 관계 `available_at`이 해당 스냅샷보다 엄격히 이전인 것만 넣는다. 과거 관계 주장과 현재 유효한 계약/지분은 같은 뜻이 아니다. 새 관계는 타입과 forward/reverse 특징을 가진 단위 가중치 링크이며 가격의 인과 부호가 아니다. 2024–2026 기본 그래프는 고정된 2023 기본 그래프를 유지한다. 최종 통합 관계는 `kg_integrated_20260919_v2`의 532근거이며, 해당 스냅샷 이후 근거는 자동으로 제외된다.

감독 대상은 직접 후보 기업이다. 간접 기업의 가격 변화를 이 학습으로 검증했다고 말할 수 없다. 필요하면 고정 모델에서 같은 기사·과거 확정 관계의 비언급 1-hop 기업을 별도 평가해야 하며, 그때도 실제 가격 잔차는 인과 영향의 정답이 아니다.

## 실행과 재개

저장소 루트에서 가상 환경 Python으로 실행한다.

```powershell
AI/ner/.venv/Scripts/python.exe -X utf8 AI/graph/encode_grounded_features.py --out AI/graph/artifacts/grounded_gnn_20260919_v1/dataset_v2 --device cuda
```

인코딩은 256문맥씩 hash가 확인된 chunk를 저장한다. 같은 명령으로 재개하면 완료 chunk를 건너뛴다. `dataset_v2/STOP_ENCODING` 파일이 있으면 다음 chunk 전에 중단한다. 재개 시 사용자가 중단 의사를 철회한 경우에만 해당 파일을 없앤다. 완료 표시는 `encoded.json`이다.

최종 그래프와 인코딩이 확인된 후:

```powershell
AI/ner/.venv/Scripts/python.exe -X utf8 AI/graph/train_grounded_news.py --dataset AI/graph/artifacts/grounded_gnn_20260919_v1/dataset_v3 --out AI/graph/artifacts/grounded_gnn_20260919_v1/training --device cuda --phase all
```

학습 설정은 기존 2층 GATv2/hidden64/dropout0.15, AdamW lr0.0008/weight decay0.001, 30epoch·patience6, 기사별 동일 가중 CE+Huber다. 2023 새 val의 최소 composite loss로 체크포인트를 고른다. 입력/코드/설정 hash가 달라지면 재개를 거절한다.

매 epoch 종료 시 optimizer·shuffle·NumPy·torch·CUDA RNG 상태를 저장한다. 강제 중단된 epoch는 마지막 완료 epoch부터 다시 실행한다. 출력 폴더의 `STOP`으로 배치 경계에서 중단할 수 있다. 9개 fit가 모두 완료되기 전에는 test/legacy_val 예측을 만들지 않는다.

평가는 전체 train 다수 클래스, 시장/종목별 train 사전확률과 함께 시장×1일/3일 accuracy·macro F1, 거래일 평균 accuracy, 강도 MAE·동일 기사 내 순위를 기록한다. 상대 성능의 불확실성은 거래일 단위 HAC와 Holm 보정으로 다룬다. CI는 근사적이고 탐색적이며, 서비스 배포 승인이나 미래 성능 보장이 아니다.

## 독립 검증 후속 단계

이번 실행에는 학습 프로세스가 `evaluation_complete.json`을 생성한 뒤
`verify_grounded_evaluation.py`를 실행하는 일회성 후속 프로세스도 붙였다.
예약 작업이나 알림은 만들지 않는다. 학습이 실패·중단되어 완료 표지가 없으면 검증 완료로
표시하지 않고 종료한다. `verification_launch.json`, `verification.stdout.log`,
`verification.stderr.log`에서 이 후속 프로세스를 확인할 수 있다.

독립 검사기는 원본 저장 예측으로 정확도·혼동행렬·F1·강도오차·순위를 재계산하고,
거래일 차이/HAC/Holm와 지표72조합·검정120조합의 누락을 확인한다. 각9개 모델을 CPU로
재로딩하여 고정 평가 기사의 저장 GPU 예측과 대조한다. 결과는
`training/independent_verification.json`과 `training/independent_verification_stage.json`이다.
새로 학습하거나 체크포인트를 다시 선택하는 단계가 아니다.

학습을 중단한 뒤 다른 PID로 재개했다면 이번 후속 프로세스는 재개되지 않는다.
학습·평가 완료 후 아래 검사만 수동으로 실행해도 된다. 기존 완료 보고서를 덮어쓰지 않는다.

```powershell
AI/ner/.venv/Scripts/python.exe -X utf8 AI/graph/verify_grounded_evaluation.py --dataset AI/graph/artifacts/grounded_gnn_20260919_v1/dataset_v3 --run AI/graph/artifacts/grounded_gnn_20260919_v1/training --out AI/graph/artifacts/grounded_gnn_20260919_v1/training/independent_verification.json
```
