# exec 제출 자료

이 폴더는 프로젝트를 인계받은 사람이 소스 코드를 클론한 뒤 빌드·배포하고 서비스를 시연할 수 있도록 필요한 자료를 모으는 곳입니다. 현재 문서는 저장소에 확인되는 설정을 바탕으로 작성한 초안입니다. 실제 운영 값과 시연 동선은 팀이 최종 검증해야 합니다.

| 항목 | 파일 | 상태 |
| --- | --- | --- |
| 포팅 매뉴얼 | `PORTING_MANUAL.md` | 초안 |
| 외부 서비스 정보 | `EXTERNAL_SERVICES.md` | 초안 |
| DB 덤프 최신본 | 미첨부 | 운영 DB에서 생성 및 복원 검증 필요 |
| 시연 시나리오 | `DEMO_SCENARIO.md` | 초안 |

## 제출 전 확인

- 실제 DB 덤프 파일을 생성하고 복원 가능 여부를 확인한 뒤 이 폴더에 추가합니다. 덤프에 개인정보·비밀값이 포함되는지 검토합니다.
- 팀 담당자가 문서의 버전·환경 변수·외부 서비스 목록과 시연 순서를 실제 운영 환경과 대조합니다.
- 비밀번호, API 키, 토큰, 실제 `.env` 파일은 커밋하지 않습니다. 필요한 변수명과 발급·설정 방법을 문서화합니다.

기존 상세 문서: [`docs/CICD.md`](../docs/CICD.md), [`docs/HADOOP_CLUSTER.md`](../docs/HADOOP_CLUSTER.md), [`docs/infrastructure-design.md`](../docs/infrastructure-design.md), [`deploy/.env.example`](../deploy/.env.example).
