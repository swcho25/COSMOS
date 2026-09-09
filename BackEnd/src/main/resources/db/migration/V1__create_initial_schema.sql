-- cosmos 스키마 정의 (매 기동마다 실행되므로 전부 IF NOT EXISTS로 멱등하게 작성)
-- 이미 존재하는 테이블은 건너뛰므로, 기존 테이블 변경 시에는
-- 이 파일을 고친 뒤 로컬 DB를 리셋(docker compose down -v && up -d)하거나 직접 ALTER 할 것.

-- users: 회원 (user는 PostgreSQL 예약어라 users 사용)
-- 결정 기록
--   해시: BCrypt (VARCHAR로 알고리즘 교체 여지 확보)
--   소셜 로그인: 계획 없음 -> password NOT NULL
--   탈퇴: soft delete (deleted_at), 익명화 배치는 CAN
--   refresh token: Redis 보관 (테이블 없음)
CREATE TABLE IF NOT EXISTS users (
                                     id                BIGSERIAL     PRIMARY KEY,
                                     email             VARCHAR(255)  NOT NULL,
    password          VARCHAR(255)  NOT NULL,
    nickname          VARCHAR(30)   NOT NULL,
    role              VARCHAR(20)   NOT NULL DEFAULT 'ROLE_USER',
    email_verified_at TIMESTAMPTZ,
    created_at        TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ   NOT NULL DEFAULT now(),
    deleted_at        TIMESTAMPTZ,
    CONSTRAINT chk_users_role CHECK (role IN ('ROLE_USER', 'ROLE_ADMIN'))
    );

-- 활성 회원 기준 유니크 (soft delete된 이메일/닉네임은 재사용 허용)
-- 이메일은 대소문자 무시: 저장/조회 시 애플리케이션에서도 lower 정규화할 것
CREATE UNIQUE INDEX IF NOT EXISTS ux_users_email    ON users (lower(email)) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_users_nickname ON users (nickname)     WHERE deleted_at IS NULL;
