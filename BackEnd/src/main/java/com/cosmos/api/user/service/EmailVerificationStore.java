package com.cosmos.api.user.service;

import com.cosmos.api.user.config.EmailVerificationProperties;
import java.util.Optional;
import lombok.RequiredArgsConstructor;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Component;

/**
 * 이메일 인증 상태를 Redis에 보관한다. 모두 TTL로 자동 만료되므로 따로 지우는 작업이 없다.
 *
 * <pre>
 * email-code:{email}      발송한 6자리 코드 (code-ttl)
 * email-cooldown:{email}  최근 발송 표시. 있으면 재발송 거부 (resend-cooldown)
 * </pre>
 * 이메일은 항상 소문자로 정규화해서 키를 만든다 (가입 시 소문자 저장 규칙과 맞춤).
 */
@Component
@RequiredArgsConstructor
public class EmailVerificationStore {

    private static final String CODE_PREFIX = "email-code:";
    private static final String COOLDOWN_PREFIX = "email-cooldown:";

    private final StringRedisTemplate redisTemplate;
    private final EmailVerificationProperties properties;

    /** 코드를 저장하고 재발송 제한 표시도 함께 남긴다. */
    public void saveCode(String email, String code) {
        redisTemplate.opsForValue().set(CODE_PREFIX + email, code, properties.codeTtl());
        redisTemplate.opsForValue().set(COOLDOWN_PREFIX + email, "1", properties.resendCooldown());
    }

    public Optional<String> findCode(String email) {
        return Optional.ofNullable(redisTemplate.opsForValue().get(CODE_PREFIX + email));
    }

    public boolean isInCooldown(String email) {
        return Boolean.TRUE.equals(redisTemplate.hasKey(COOLDOWN_PREFIX + email));
    }

    /** 테스트·재시도용. 이 이메일과 관련된 인증 상태를 모두 지운다. */
    public void clear(String email) {
        redisTemplate.delete(CODE_PREFIX + email);
        redisTemplate.delete(COOLDOWN_PREFIX + email);
    }
}
