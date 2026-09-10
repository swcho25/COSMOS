package com.cosmos.api.user.controller;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.cosmos.api.user.dto.SignupRequest;
import com.cosmos.api.user.entity.User;
import com.cosmos.api.user.repository.UserRepository;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.ResultActions;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.ObjectMapper;

@SpringBootTest
@AutoConfigureMockMvc
@Transactional
class AuthControllerTest {

    @Autowired
    MockMvc mockMvc;

    @Autowired
    ObjectMapper objectMapper;

    @Autowired
    UserRepository userRepository;

    private ResultActions signup(String email, String password, String nickname) throws Exception {
        SignupRequest request = new SignupRequest(email, password, nickname);
        return mockMvc.perform(post("/api/auth/signup")
                .contentType(MediaType.APPLICATION_JSON)
                .content(objectMapper.writeValueAsString(request)));
    }

    @Test
    @DisplayName("회원가입 성공 시 201과 회원 정보를 반환하고, 이메일은 소문자로 정규화된다")
    void signup_success() throws Exception {
        signup("Kim@Test.com", "password1", "우주탐험가")
                .andExpect(status().isCreated())
                .andExpect(jsonPath("$.userId").isNumber())
                .andExpect(jsonPath("$.email").value("kim@test.com"))
                .andExpect(jsonPath("$.nickname").value("우주탐험가"));
    }

    @Test
    @DisplayName("비밀번호는 평문이 아닌 BCrypt 해시로 저장된다")
    void password_is_hashed() throws Exception {
        signup("hash@test.com", "password1", "해시검증").andExpect(status().isCreated());

        User saved = userRepository.findByEmail("hash@test.com").orElseThrow();
        assertThat(saved.getPassword()).isNotEqualTo("password1");
        assertThat(saved.getPassword()).startsWith("$2"); // BCrypt 프리픽스
    }

    @Test
    @DisplayName("이미 가입된 이메일이면 대소문자가 달라도 409 EMAIL_DUPLICATED를 반환한다")
    void signup_duplicate_email() throws Exception {
        signup("dup@test.com", "password1", "첫번째").andExpect(status().isCreated());

        signup("DUP@test.com", "password1", "두번째")
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("EMAIL_DUPLICATED"));
    }

    @Test
    @DisplayName("이미 사용 중인 닉네임이면 409 NICKNAME_DUPLICATED를 반환한다")
    void signup_duplicate_nickname() throws Exception {
        signup("nick1@test.com", "password1", "같은닉네임").andExpect(status().isCreated());

        signup("nick2@test.com", "password1", "같은닉네임")
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("NICKNAME_DUPLICATED"));
    }

    @Test
    @DisplayName("비밀번호에 숫자가 없으면 400과 password 필드 에러를 반환한다")
    void signup_invalid_password() throws Exception {
        signup("weak@test.com", "passwordonly", "약한비번")
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("VALIDATION_FAILED"))
                .andExpect(jsonPath("$.fieldErrors[0].field").value("password"));
    }
}
