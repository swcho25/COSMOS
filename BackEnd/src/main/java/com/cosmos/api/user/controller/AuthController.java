package com.cosmos.api.user.controller;

import com.cosmos.api.user.dto.AvailabilityResponse;
import com.cosmos.api.user.dto.SignupRequest;
import com.cosmos.api.user.dto.UserResponse;
import com.cosmos.api.user.service.UserService;
import jakarta.validation.Valid;
import jakarta.validation.constraints.Email;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;
import lombok.RequiredArgsConstructor;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.validation.annotation.Validated;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/auth")
@RequiredArgsConstructor
@Validated // 쿼리 파라미터 검증 활성화 (check-email, check-nickname)
public class AuthController {

    private final UserService userService;

    @PostMapping("/signup")
    public ResponseEntity<UserResponse> signup(@Valid @RequestBody SignupRequest request) {
        UserResponse response = userService.signup(request);
        return ResponseEntity.status(HttpStatus.CREATED).body(response);
    }

    @GetMapping("/check-email")
    public AvailabilityResponse checkEmail(
            @RequestParam
            @NotBlank(message = "이메일을 입력해 주세요.")
            @Email(message = "올바른 형식의 이메일 주소여야 합니다.")
            @Size(max = 255, message = "이메일은 255자 이하여야 합니다.")
            String email
    ) {
        return new AvailabilityResponse(userService.isEmailAvailable(email));
    }

    @GetMapping("/check-nickname")
    public AvailabilityResponse checkNickname(
            @RequestParam
            @NotBlank(message = "닉네임을 입력해 주세요.")
            @Size(min = 2, max = 30, message = "닉네임은 2자 이상 30자 이하여야 합니다.")
            String nickname
    ) {
        return new AvailabilityResponse(userService.isNicknameAvailable(nickname));
    }
}
