package com.cosmos.api.user.service;

import com.cosmos.api.global.error.BusinessException;
import com.cosmos.api.global.security.JwtTokenProvider;
import com.cosmos.api.global.security.RefreshTokenStore;
import com.cosmos.api.user.dto.LoginRequest;
import com.cosmos.api.user.entity.User;
import com.cosmos.api.user.error.UserErrorCode;
import com.cosmos.api.user.repository.UserRepository;
import java.util.Locale;
import lombok.RequiredArgsConstructor;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/** 로그인·토큰 발급 흐름. 회원 정보 자체(가입·조회)는 UserService가 맡는다. */
@Service
@RequiredArgsConstructor
public class AuthService {

    private final UserRepository userRepository;
    private final PasswordEncoder passwordEncoder;
    private final JwtTokenProvider jwtTokenProvider;
    private final RefreshTokenStore refreshTokenStore;

    /** 로그인 성공 시 발급한 토큰 두 개. Refresh는 컨트롤러가 쿠키에 담는다. */
    public record LoginResult(String accessToken, String refreshToken) {
    }

    /**
     * 로그인 순서: 이메일로 회원 조회 → 비밀번호 대조 → 토큰 2개 발급 → Refresh는 Redis에 보관.
     * 이메일이 없어도, 비밀번호가 틀려도 같은 오류(INVALID_CREDENTIALS)를 낸다.
     */
    @Transactional(readOnly = true)
    public LoginResult login(LoginRequest request) {
        User user = userRepository.findByEmail(request.email().toLowerCase(Locale.ROOT))
                .orElseThrow(() -> new BusinessException(UserErrorCode.INVALID_CREDENTIALS));

        if (!passwordEncoder.matches(request.password(), user.getPassword())) {
            throw new BusinessException(UserErrorCode.INVALID_CREDENTIALS);
        }

        String accessToken = jwtTokenProvider.createAccessToken(user.getUserId(), user.getRole());
        String refreshToken = jwtTokenProvider.createRefreshToken(user.getUserId());
        refreshTokenStore.save(user.getUserId(), refreshToken);

        return new LoginResult(accessToken, refreshToken);
    }
}
