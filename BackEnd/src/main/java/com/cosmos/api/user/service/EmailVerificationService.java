package com.cosmos.api.user.service;

import com.cosmos.api.global.error.BusinessException;
import com.cosmos.api.user.config.EmailVerificationProperties;
import com.cosmos.api.user.dto.EmailSendCodeResponse;
import com.cosmos.api.user.error.UserErrorCode;
import com.cosmos.api.user.repository.UserRepository;
import java.security.SecureRandom;
import java.util.Locale;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;

/**
 * 가입 전 이메일 인증. 순서는 "코드 발송 → 코드 확인 → 가입".
 * 이 클래스는 그중 발송을 맡는다. 확인·가입 연동은 다음 이슈(143·144)에서 붙인다.
 */
@Service
@RequiredArgsConstructor
public class EmailVerificationService {

    private static final int CODE_LENGTH = 6;
    private static final SecureRandom RANDOM = new SecureRandom();

    private final EmailVerificationStore store;
    private final VerificationMailSender mailSender;
    private final UserRepository userRepository;
    private final EmailVerificationProperties properties;

    /**
     * 6자리 코드를 만들어 메일로 보내고 Redis에 저장한다.
     * 이미 가입된 이메일이면 409, 60초 안에 다시 요청하면 429.
     * 메일 발송이 실패하면 코드도 저장하지 않는다 (받지 못한 코드가 남지 않게).
     */
    public EmailSendCodeResponse sendCode(String rawEmail) {
        String email = rawEmail.toLowerCase(Locale.ROOT);

        if (userRepository.existsByEmail(email)) {
            throw new BusinessException(UserErrorCode.EMAIL_DUPLICATED);
        }
        if (store.isInCooldown(email)) {
            throw new BusinessException(UserErrorCode.EMAIL_CODE_TOO_FREQUENT);
        }

        String code = generateCode();
        mailSender.sendCode(email, code, properties.codeTtl());
        store.saveCode(email, code);

        return new EmailSendCodeResponse(email, properties.codeTtl().toSeconds());
    }

    // 000000~999999 를 항상 6자리로 (앞자리 0 유지)
    private String generateCode() {
        return String.format("%0" + CODE_LENGTH + "d", RANDOM.nextInt(1_000_000));
    }
}
