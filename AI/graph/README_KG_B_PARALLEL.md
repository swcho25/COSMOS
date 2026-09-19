# 두 노트북에서 남은 B 뉴스 선별 실행

**2026-09-18 17:41 최신 상태:** 저장분 **2,514,445쌍**의 별도 부분 병합·원문 대조·분포 및 표본 감사를 완료했다.
채택 후보666,282쌍은 고유 기사483,738건이다. pc1 미처리142,361쌍과 STOP은 그대로 보존했다.
결과는 `artifacts/kg_b_partial_merged_20260918_v1/`, 상세는 [부분 병합·품질 감사](README_KG_B_PARTIAL_AUDIT.md).
이는 저장된 부분 자료의 완료이며 전체 선별 완료나 품질 게이트 통과가 아니다. 아래 재개 명령은 사용자 명시 요청이 있을 때만 실행한다.

**2026-09-18 16:40 상태:** pc1은 사용자 요청으로 734,970쌍에서 정상 중단(142,361쌍 남음).
pc2 `output.zip`의 877,175쌍 완료 결과를 `pc2/output`에 수신하고 내부 무결성 검증을 통과했다.
감사 기록은 분할 루트 `pc2_received_audit_20260918.json`. pc1 STOP은 유지하며 전체 병합은 아직 실행하지 않았다.

2026-09-18에 중단한 결과 902,300쌍을 보존하고, 남은 1,754,506쌍만 나눴다.
모델 재학습이 아니라 기존 B 모델의 잠정 채택·제외·보류 추론이다.

| 담당 | 뉴스–기업 쌍 |
| --- | ---: |
| 기존 완료·보존 | 902,300 |
| 현재 노트북 `pc1` | 877,331 |
| 추가 노트북 `pc2` | 877,175 |
| 전체 | 2,656,806 |

패키지 루트: `AI/graph/artifacts/kg_b_parallel_20260918_v1`.
`base`에는 기존 SQLite의 일관된 백업, `split_plan.json`에는 두 작업의 분할 범위와 원본 위치 매핑이 있다.
원본 `kg_b_provisional_full_20260918_v1`의 STOP은 유지한다.
**이제 두 패키지를 사용할 때 예전 `start_kg_b_provisional.ps1`을 함께 실행하면 안 된다. 같은 남은 데이터를 중복 처리한다.**

## 추가 노트북: 파일 하나 복사한 뒤 실행


1. `pc2_worker.zip`을 추가 노트북에 복사한다.
2. 압축을 풀어 `C:\cosmos\pc2`처럼 최종 사용할 짧은 경로에 둔다. 최초 실행 후 폴더 위치를 바꾸지 않는다.
3. Python 3.12와 NVIDIA 드라이버가 설치돼 있어야 한다. 첫 실행은 인터넷으로 가상환경에 라이브러리를 설치한다.
4. Git Bash에서 압축을 푼 `pc2` 폴더로 들어가 실행한다.

```bash
bash run.sh
```

Git Bash가 없으면 같은 폴더의 PowerShell에서 실행한다.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\run.ps1
```

파일 검사 → 필요 라이브러리 설치 → GPU 확인 → 이 노트북에 배정된 뉴스만 처리한다.
콘솔을 열어 두고 충전기를 연결한다. 화면 잠금·화면 꺼짐과 달리 절전·종료 중에는 계산하지 않는다.
현재 PC에서 실제 GPU 실행을 검증했지만, 추가 노트북의 최초 설치와 GPU 상태는 그곳에서 확인한다.

## 현재 노트북

2026-09-18 09:28 사용자의 재개 요청으로 백그라운드 실행을 시작했다.
실행 중인 작업을 다시 시작하지 말고 `pc1/output/status.json`에서 진행을 확인한다.
중단 후 현재 노트북에서 숨김 백그라운드로 재개할 때는 저장소 루트에서 사용한다.

```powershell
& .\AI\graph\start_kg_b_parallel_pc1.ps1
```

`pc1/output/STOP`으로 중단했다면 위 명령에 `-ResumeAfterStop`을 붙인다.
시작 영수증과 콘솔 로그는 분할 루트 `launches`에 저장한다. 예약 작업은 생성하지 않는다.

저장소 루트에서 기존 가상환경을 그대로 사용할 수 있다.

```powershell
& .\AI\ner\.venv\Scripts\python.exe -X utf8 -u .\AI\graph\artifacts\kg_b_parallel_20260918_v1\pc1\bootstrap.py --skip-install
```

`pc1` 안에서도 `bash run.sh` 또는 `run.ps1`을 사용할 수 있지만, 첫 기본 실행은 독립 `.venv`를 설치한다.
두 노트북에는 각각 `pc1`, `pc2`만 배정한다. 같은 `pc2`를 양쪽에서 실행하지 않는다.

## 중단·재개·결과 전달

진행률은 각 패키지의 `output/status.json`, 상세 로그는 `logs`에 저장한다.
중단은 `output/STOP` 파일을 만들거나 실행 중 Ctrl+C를 누른다.
STOP을 만든 후 다시 시작할 때는 다음을 사용한다.

```bash
bash run.sh --resume-after-stop
```

갑작스러운 종료 뒤 STOP이 없으면 일반 `bash run.sh`로 다시 실행한다.
SQLite에 저장된 묶음은 재사용하고 저장 전 끊긴 묶음만 다시 계산한다.
`output/state.sqlite3`와 `-wal`, `-shm` 파일을 삭제하지 않는다.

`output/status.json`의 `state`가 `provisional_complete`이고 `complete: true`이며
`output/provisional_receipt.json`이 만들어진 뒤, 프로세스가 종료되면 배정분 완료다.
추가 노트북의 **`output` 폴더 전체**를 현재 PC의 `pc2/output`으로 복사한다.
다른 경로에서 재개하려는 용도가 아니라 결과 병합용 복사다.

두 작업이 끝난 후 저장소 루트에서 새 폴더로 검증·병합한다.

```powershell
& .\AI\ner\.venv\Scripts\python.exe -X utf8 AI/graph/merge_kg_provisional_partitions.py `
  --plan AI/graph/artifacts/kg_b_parallel_20260918_v1/split_plan.json `
  --pc1-out AI/graph/artifacts/kg_b_parallel_20260918_v1/pc1/output `
  --pc2-out AI/graph/artifacts/kg_b_parallel_20260918_v1/pc2/output `
  --out AI/graph/artifacts/kg_b_provisional_merged_20260918_v1
```

병합은 원본 위치·입력·모델·코드·결과 체크섬, 누락·중복, 총 2,656,806쌍을 검사한다.
기존 902,300쌍을 다시 추론하지 않는다. 이 명령은 두 작업이 완료되기 전에는 실행하지 않는다.
잠정 분류만 끝나는 것이며 지식그래프·기업별 감성·서비스 적재·GNN 학습은 후속 작업이다.

## 준비와 확인 기록

- 분할 CPU 검사 7개, 병합 CPU 검사 6개 통과.
- 실제 분할 계획의 합계·파일 바인딩 검사 통과.
- 패키지 무결성·실행 기록은 `pc1_package_receipt.json`, `pc2_package_receipt.json`과 각 `logs` 참고.
- GPU 및 재개 검증 결과는 `portable_resume_verified.json` 참고.
- 원본 입력·B 모델·실행 코드와 중단 결과는 변경하지 않았다. 예약 실행은 등록하지 않았다.
