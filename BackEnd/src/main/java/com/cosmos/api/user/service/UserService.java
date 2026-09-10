package com.cosmos.api.user.service;

import com.cosmos.api.global.error.BusinessException;
import com.cosmos.api.user.dto.SignupRequest;
import com.cosmos.api.user.dto.UserResponse;
import com.cosmos.api.user.entity.User;
import com.cosmos.api.user.error.UserErrorCode;
import com.cosmos.api.user.repository.UserRepository;
import java.util.Locale;
import lombok.RequiredArgsConstructor;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
@RequiredArgsConstructor
public class UserService {

    private final UserRepository userRepository;
    private final PasswordEncoder passwordEncoder;

    @Transactional(readOnly = true)
    public boolean isEmailAvailable(String email) {
        return !userRepository.existsByEmail(email.toLowerCase(Locale.ROOT));
    }

    @Transactional(readOnly = true)
    public boolean isNicknameAvailable(String nickname) {
        return !userRepository.existsByNickname(nickname);
    }

    @Transactional
    public UserResponse signup(SignupRequest request) {
        String email = request.email().toLowerCase(Locale.ROOT);

        if (userRepository.existsByEmail(email)) {
            throw new BusinessException(UserErrorCode.EMAIL_DUPLICATED);
        }
        if (userRepository.existsByNickname(request.nickname())) {
            throw new BusinessException(UserErrorCode.NICKNAME_DUPLICATED);
        }

        User user = User.create(email, passwordEncoder.encode(request.password()), request.nickname());
        return UserResponse.from(userRepository.save(user));
    }
}
