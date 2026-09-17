package com.cosmos.api.user.error;

import com.cosmos.api.global.error.ErrorCode;
import org.springframework.http.HttpStatus;

public enum UserErrorCode implements ErrorCode {

    EMAIL_DUPLICATED(HttpStatus.CONFLICT, "이미 사용 중인 이메일입니다."),
    NICKNAME_DUPLICATED(HttpStatus.CONFLICT, "이미 사용 중인 닉네임입니다."),
    /** 이메일이 없는 경우와 비밀번호가 틀린 경우를 일부러 구분하지 않는다 (가입 여부 노출 방지). */
    INVALID_CREDENTIALS(HttpStatus.UNAUTHORIZED, "이메일 또는 비밀번호가 올바르지 않습니다."),
    WATCH_COMPANY_DUPLICATED(HttpStatus.CONFLICT, "이미 등록한 관심 기업입니다."),
    NEWS_SCRAP_DUPLICATED(HttpStatus.CONFLICT, "이미 스크랩한 뉴스입니다."),
    /** 같은 이메일로 인증 코드를 너무 자주 요청함 (메일 폭탄 방지). */
    EMAIL_CODE_TOO_FREQUENT(HttpStatus.TOO_MANY_REQUESTS, "잠시 후 다시 요청해 주세요.");

    private final HttpStatus status;
    private final String message;

    UserErrorCode(HttpStatus status, String message) {
        this.status = status;
        this.message = message;
    }

    @Override
    public HttpStatus status() {
        return status;
    }

    @Override
    public String code() {
        return name();
    }

    @Override
    public String message() {
        return message;
    }
}
