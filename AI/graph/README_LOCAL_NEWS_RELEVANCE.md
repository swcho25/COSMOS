# 로컬 LLM으로 뉴스·기업 관련성 선별

최종 실행48건은 완료했다. 실제40건 중 모델 채택10건, 별도 AI도 채택한 검토 후보7건,
충돌3건이다. 무검수 전량 선별에 적용할 품질은 아직 아니다.
실측·오판 사례는 [LOCAL_RELEVANCE_PILOT_RESULTS.md](LOCAL_RELEVANCE_PILOT_RESULTS.md)를 읽는다.

사용자가 제공한 경제적 관련성 판정 프롬프트를 `news_relevance_prompt.txt`에
보존했다. 모델은 Qwen/Qwen3-4B-Instruct-2507, revision
`cdbee75f17c01a7cc42f958dc650907174af0554`다. 원본 Safetensors를 받아 로드할 때
bitsandbytes NF4 4비트로 변환한다. 모델을 새로 학습하는 작업은 아니다.

## 범위와 판정 의미

- 입력 단위는 **기사 × 대상 기업**이다. 기사 하나가 어떤 기업에는 관련되고
  다른 기업에는 단순 언급일 수 있다. 한 기업의 판정을 다른 기업에 전파하지 않는다.
- 입력은 발행 시각, 제목, 정제 본문 전체, 대상 티커/이름, 당시 출처가 있는 기업 정보다.
  이번 실제 40건에는 인증된 당시 기업 프로필이 없어 `company_context=null`이다.
- 사용자 프롬프트에 JSON 출력 형식만 덧붙인다. `직접관련/간접관련/단순언급/판단보류`,
  `신규/과거/불명`, `채택/제외/보류`를 각각 보존한다.
- 최종 방식은 모델이 원문 구간의 ID를 선택하고 코드가 인용문을 복원한다. 원문을
  모델이 다시 쓰게 하지 않는다. ID는 전체 제목·본문에서 결정적으로 만들며 누락 없이
  원래 텍스트로 재결합되는지 검사한다. 언어학적으로 완벽한 문장 분리라고 주장하지 않는다.
- 없는 근거 ID, 잘린 생성, 잘못된 JSON은 최종 `보류`다. 긴 기사를 몰래 잘라
  판정하지 않고 전체 입력 제한 초과로 보류한다. 이번 GPU 설정은 입력 최대6,144토큰이다.
- **GNN 후보 채택**에는 직접/간접 관련, 신규 사건, 원문 인용과 경제적 경로가 필요하다.
  과거 회고와 불명 시점의 자동 채택은 보류한다. 이것은 명시적인 후보 적격 조건이다.
- 인용의 문자 일치는 의미적 정확성을 보증하지 않는다. 출력은 자동 생성 후보이며
  주가 변동 보장이나 사람 검수 완료 표시가 아니다. 실제 무반응 기사도 유효한
  경제적 사건이면 남겨야 한다. 향후 수익률을 보고 기사 후보를 골라서는 안 된다.

## Git·실행 환경

`AI/graph/.gitignore`의 `local_models/`에 가중치, HF 캐시, 4비트 패키지를 둔다.
본문·결과는 기존 `artifacts/` 제외 규칙 아래에 있다. 소스·프롬프트·테스트만 Git에
올릴 수 있다. 이번 작업에서 commit/push/MR, DB 적재, 서비스 연결은 하지 않는다.

기존 `AI/ner/.venv`의 torch2.6.0+cu124 / transformers5.16.1 / accelerate1.14.0을
사용하고 bitsandbytes0.50.2만 `local_models/runtime_packages`에 별도 설치했다.
기존 GNN 패키지 버전을 바꾸지 않는다. PC의 RTX4050 Laptop GPU는 약6GB다.
다운로드 파일은 약8GB이지만 GPU 로드 때 4비트로 줄인다. 실행 후 실측은 결과 기록을
참고하며 디스크 용량과 GPU 메모리는 다른 수치다.

```powershell
# 필요 시 별도 실행 패키지 설치(기존 환경을 수정하지 않는 target 설치)
uv pip install --python AI/ner/.venv/Scripts/python.exe --target AI/graph/local_models/runtime_packages --no-deps bitsandbytes==0.50.2

# 공식 모델 다운로드. 큰 파일 전송이 멈추면 범위 전송 옵션을 사용한다.
AI/ner/.venv/Scripts/python.exe -X utf8 -u AI/graph/download_relevance_model.py --revision cdbee75f17c01a7cc42f958dc650907174af0554 --range-transfer --download-workers 16

# 아래 combined_inputs는 가상8건 + 실제40건의 고정 파일럿이다.
AI/ner/.venv/Scripts/python.exe -X utf8 -u AI/graph/local_news_relevance.py --inputs AI/graph/artifacts/local_relevance_pilot_20260916_v1/combined_inputs.jsonl --out AI/graph/artifacts/local_relevance_pilot_20260916_v1/inference_v4 --evidence-mode sentence_ids --attn-implementation sdpa_explicit_kv
```

공식 파일12개 총8,060,915,998바이트를 받았고, 세 가중치 파일 모두 게시자 SHA256과
일치한다. 로드할 때 로컬 모델·토크나이저 해시를 다시 대조한다. 추론은
`local_files_only=True`, `HF_HUB_OFFLINE=1`, `trust_remote_code=False`이며 기사를
외부 모델 API로 전송하지 않는다. 원문·출력은 로컬에만 기록한다.

실행 중 결과는 매 쌍마다 디스크에 기록한다. 같은 명령으로 재개할 때 입력·모델·프롬프트·
코드·옵션이 모두 일치해야 하며, 기존 출력도 다시 검사한다. 출력 폴더 동시 실행은
`inference.lock`으로 차단한다. 비정상 종료로 잠금이 남으면 기록된 PID의 종료를
확인한 후 해당 잠금만 제거한다. PC를 끄면 이 로컬 추론은 중단된다.
별도로 서버에서 실행한 HDFS 전체 이력 배치는 이 작업과 독립적이다.

## 검증 자료와 산출물

`artifacts/local_relevance_pilot_20260916_v1/`:

- `inputs.jsonl`: 2020–2022 학습 구간의 실제40기사·40기업쌍. 기존 val/test와
  ID·정규 URL·본문 해시 중복0. 직접 사건/부수 언급/과거 배경/불명확 각10개인 개발 표본.
- `evaluation.jsonl`: 이전 AI 사건 검토의 참고 판정. 더 넓은 경제관련성 정답과 다를 수 있다.
- `synthetic_inputs.jsonl`, `synthetic_reference.jsonl`: 전직 소속·명확한 사건·산업 정보
  제공 여부·기사 속 지시문 등 가상8건. 실제 뉴스 표본 수에 섞지 않는다.
  재현용 가상 입력·참조는 `tests/fixtures/local_relevance_synthetic_*.jsonl`에도 있다.
- `independent_reference_*.jsonl`: 이번 사용자 기준으로 모델 출력을 보기 전에 수행한
  추가 AI 검토. 사람 정답셋이라고 부르지 않는다.
- `inference_v4/protocol.json`: 모델 revision, 코드/입력/프롬프트/근거 복원기 해시, 환경, 설정.
- `inference_v4/results.jsonl`: 모델 원출력의 근거 ID, 코드가 복원한 원문 인용, 시간·토큰·GPU 메모리, 최종 선택.
- `inference_v4/candidate_pairs.jsonl`, `inference_v4/excluded_pairs.jsonl`, `inference_v4/held_pairs.jsonl`:
  각각 실제 기사·기업의 자동 채택 후보/제외/보류. 가상 기사와 합치지 않는다.
- `evaluation_v4_independent/summary.json`, `pair_audit.jsonl`, `review_packet.jsonl`:
  동결된 별도 AI 검토와의 비교, 개별 대조, 불일치·오류 검토 자료.
- `review_candidates_v4/review_agreed_candidates.jsonl`, `disputed_candidates.jsonl`, `audit.json`:
  로컬 모델과 별도 AI 검토의 채택 선택이 일치한 후보, 불일치 후보와 출처 감사 기록.
  두 AI의 일치는 사람이 확인한 정답이나 전체 판정 필드의 일치를 뜻하지 않는다.

추론48건을 완료한 뒤 아래 명령으로 평가와 후보 파일을 만든다. 기존 출력 폴더를
덮어쓰지 않으므로 재평가할 때는 새 `--out`을 쓴다. 후보 생성기는 실제40쌍 전체와
독립 검토의 동결 해시를 요구하며, `candidate_pairs.jsonl`만 입력하면 안 된다.

```powershell
$pilot = 'AI/graph/artifacts/local_relevance_pilot_20260916_v1'
AI/ner/.venv/Scripts/python.exe -X utf8 AI/graph/evaluate_local_relevance.py --inputs "$pilot/combined_inputs.jsonl" --reference "$pilot/independent_reference_01.jsonl" "$pilot/independent_reference_02.jsonl" "$pilot/synthetic_reference.jsonl" --results "$pilot/inference_v4/results.jsonl" --out "$pilot/evaluation_v4_independent"
AI/ner/.venv/Scripts/python.exe -X utf8 AI/graph/build_relevance_review_candidates.py --inputs "$pilot/combined_inputs.jsonl" --results "$pilot/inference_v4/results.jsonl" --reference "$pilot/independent_reference_01.jsonl" "$pilot/independent_reference_02.jsonl" --out "$pilot/review_candidates_v4"
```

보류는 비관련 정답으로 바꾸지 않는다. 기존 GNN 데이터셋은 수정하지 않으며,
후속 학습에서는 `(article_id, ticker)`로 후보 목록을 결합하고 기존의 가격 기반 라벨
생성 절차를 적용해야 한다. 이 파일럿의 후보 몇 건만으로 GNN을 다시 학습하지 않는다.

초기 `inference_v1`은 greedy 생성이 인용을 반복하여 토큰 한도에 걸리는 문제를
발견하고11건 후 중단했다. `STOPPED.json`, 원출력, 당시 코드와 설정을 남겼다.
`inference_v2`는 제작사 권장값인 temperature0.7/top_p0.8/top_k20으로 생성하며,
기사·기업 키별로 고정한 난수 시드를 사용한다. 의미 판정 프롬프트는 그대로이고,
JSON 형식 안내에 인용 최대2문장·중복 금지·간결한 출력 조건을 명시했다.
이 개발 표본에서 발견한 출력 문제를 수정한 것이며 미사용 평가라고 주장하지 않는다.
`inference_v2`도17건 후 인용문 변형·과도한 복사·JSON 오류를 확인하고 중단했다.
`inference_v3`부터 같은 판정 기준으로 근거 ID를 선택하고 코드가 원문을 복원한다.
`inference_v3`는24건 완료 후 긴 입력의 GPU 메모리·속도 문제로 중단해 보존했다.
`inference_v4`는 아래 실행 방식을 적용하여 같은48건 전체를 새로 측정했다.
초반 실패 결과는 지우지 않고 보관한다. 출력 형식 개선과 경제적 관련성
정확도 개선은 구분하며, 새 방식도 실제 검증이 필요하다.

## 해석의 한계

### Windows GPU 실행 방식

이 PC의 torch2.6 Windows 빌드에는 Flash Attention이 없다. 기본 Transformers SDPA가
Qwen3의 GQA를 그대로 요청하면 메모리를 크게 사용하는 math 커널로 실행된다.
실제 소형 GPU 검사에서도 GQA 상태로는 efficient 커널을 쓸 수 없었고, KV head를
명시적으로 반복한 같은 수식은 efficient 커널로 실행됐다. 이를
`local_relevance_attention.py`에 별도 등록하며 기존 패키지 코드를 수정하지 않는다.
동일한 SDPA 마스크를 등록해 causal/padding 의미를 유지하고, CUDA에서는 efficient
커널을 쓰지 못하면 중단한다. 무거운 math 커널로 조용히 바뀌지 않는다.
CPU11테스트로 prefill, cache decode, padding, 중복 확장 방지 등을 확인했다.
GPU 수치 비교는 `attention_gpu_probe.json`에 있다. 연산 커널에 따른 반올림 차이와
샘플링 출력 차이가 가능하므로 이전 결과에 이어 붙이지 않고48건을 새로 실행했다.

근거: [PyTorch2.6 SDPA](https://docs.pytorch.org/docs/2.6/generated/torch.nn.functional.scaled_dot_product_attention.html),
[Transformers attention 등록](https://huggingface.co/docs/transformers/attention_interface).

### 품질·학습에 대한 한계

40건은 균등 구성한 개발용 사례이며 모집단 정밀도/재현율이나 GNN 주가 정확도가 아니다.
가상 반례 성공도 실제 전체 뉴스의 오탐률을 보증하지 않는다. 사람 검토 완료는0건이다.
더구나 2025년 모델로 과거 기사를 읽으므로 파라미터에 후대 지식이 있을 가능성을
없앴다고 주장할 수 없다. 본문 근거를 제한해도 당시 배포 가능한 모델의 성능은 아니다.
성능 개선을 입증하려면 새 표본의 사람 검수와 동결된 선택 정책에 대한 후속 기간 평가가
필요하다. 전체1,300만 건 처리나 기존 GNN보다 정확도 향상을 이 결과로 주장하지 않는다.

공식 출처: [Qwen 모델 카드](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507),
[Transformers bitsandbytes 안내](https://huggingface.co/docs/transformers/quantization/bitsandbytes).
