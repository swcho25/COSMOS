# 포팅 매뉴얼 (초안)

> 기준: `dev` 브랜치의 코드와 `docs/CICD.md`, `deploy/.env.example` (2026-09-28 확인). 팀 담당자가 실제 운영 환경에서 명령과 버전을 검증해 확정해야 합니다.

## 1. 구성과 버전

| 구성 | 저장소에서 확인한 내용 | 확인할 사항 |
| --- | --- | --- |
| 백엔드 | Spring Boot, Java 21 빌드 | 정확한 JDK·Gradle·IDE 버전 |
| 프론트엔드 | TypeScript / Vite, ESLint | Node.js·패키지 매니저·IDE 버전 |
| 웹서버 | 호스트 Nginx 및 프론트 Nginx | 서버의 Nginx 버전 |
| 실행 | Docker Compose, Jenkins CI/CD | Docker·Compose·Jenkins 버전 |
| 데이터 | PostgreSQL, Redis; 별도 Hadoop 클러스터 | 각 제품 버전과 실제 초기 데이터 |

백엔드·프론트엔드·배포 설정은 각각 `BackEnd/`, `FrontEnd/`, `deploy/`에 있습니다. 서버 구성은 [`docs/CICD.md`](../docs/CICD.md), 데이터 클러스터는 [`docs/HADOOP_CLUSTER.md`](../docs/HADOOP_CLUSTER.md)를 따릅니다.

## 2. 클론 후 빌드·배포 순서

1. 팀 GitLab 저장소를 클론하고 배포할 커밋을 선택합니다. 운영 배포 파이프라인은 `dev` 푸시를 대상으로 합니다.
2. `deploy/.env.example`을 참고해 운영 환경 파일을 서버에 준비합니다. 실제 비밀값을 저장소에 올리지 않습니다.
3. 운영 서버의 Docker, Compose, Nginx, 인증서, Jenkins와 필요한 포트·도메인을 점검합니다. 초기 서버 설치와 인증서 설정은 기존 운영 가이드의 `서버 최초 구성` 절을 따릅니다. 기존 데이터가 있는 서버에 초기화 스크립트를 그대로 재실행하지 않습니다.
4. Jenkins 파이프라인은 프론트 빌드·Lint, Java 21 백엔드 빌드·테스트, 이미지 생성, 배포·상태 확인을 수행합니다. 수동 검증용 스크립트는 `deploy/scripts/ci.sh <test-tag>`와 `deploy/scripts/build-images.sh <release-tag>`이며, 운영 배포 절차는 `docs/CICD.md`에 있습니다.
5. 배포 후 `https://j15c205.p.ssafy.io/healthz`와 실제 주요 화면을 확인합니다. 데이터 클러스터 작업은 웹 배포와 별도로 점검합니다.

## 3. 환경 변수와 설정 파일

`deploy/.env.example`에는 `PUBLIC_URL`, `DB_PASSWORD`, `REDIS_PASSWORD`, `JWT_SECRET`이 정의되어 있습니다. `IMAGE_TAG`는 릴리스 스크립트가 전달합니다. 운영 파일은 서버의 `/etc/cosmos/app.env`이며 접근 권한을 제한합니다. 프론트 빌드에는 `VITE_API_MODE=live`, `VITE_API_BASE=/api`가 필요합니다. 환경 변수를 나중에 바꾸어도 이미 빌드한 Vite 번들에는 반영되지 않으므로 다시 빌드해야 합니다.

DB·Redis는 기본 Compose에서 외부 포트를 공개하지 않습니다. DB 접속에 사용되는 실제 계정과 URL은 운영 담당자가 확인해야 합니다. DB·인증 관련 속성이 정의된 소스 파일과 ERD에 필요한 계정 목록도 제출 전 보완해야 합니다. 실제 비밀번호·토큰은 이 문서에 기록하지 않습니다.

## 4. 배포 시 주의사항

- 웹 서비스와 Jenkins는 동일 EC2의 Docker Compose 기반이며, 데이터 클러스터는 별도 구성입니다. 포트·접근 범위는 `docs/CICD.md`의 `구성과 포트` 절을 확인합니다.
- Flyway 마이그레이션은 애플리케이션 시작 시 DB를 변경합니다. 이미지 롤백은 DB 스키마를 되돌리지 않습니다. 파괴적 변경 전에는 DB 백업과 복구 절차를 준비합니다.
- 릴리스 구성은 `/opt/cosmos/releases/<IMAGE_TAG>/compose.yml`, 현재 릴리스는 `/opt/cosmos/releases/current`에 있습니다. 상태 확인 실패 시 배포 스크립트가 직전 정상 릴리스 복구를 시도합니다.
- 최신 DB 덤프는 아직 이 폴더에 없습니다. 운영 DB에서 생성하고 별도 환경에 복원해 확인한 뒤 추가해야 합니다.

## 5. 제출 전 팀 확인 항목

- [ ] JDK, Node.js, Docker, Compose, Nginx, PostgreSQL, Redis, Jenkins 및 IDE의 실제 버전 기록
- [ ] 클론부터 빌드·실행까지 새 환경에서 재현
- [ ] 운영 환경 변수와 속성 파일 위치·용도 점검
- [ ] DB 덤프 생성·복원 검증 및 파일 추가
- [ ] 외부 서비스 계정 인계 방법과 시연 경로 검증
