package com.cosmos.api.user.controller;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.cosmos.api.user.dto.SignupRequest;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.ObjectMapper;

@SpringBootTest
@AutoConfigureMockMvc
@Transactional
class DuplicateCheckControllerTest {

    @Autowired
    MockMvc mockMvc;

    @Autowired
    ObjectMapper objectMapper;

    private void signup(String email, String nickname) throws Exception {
        mockMvc.perform(post("/api/auth/signup")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(objectMapper.writeValueAsString(new SignupRequest(email, "password1", nickname))))
                .andExpect(status().isCreated());
    }

    @Test
    @DisplayName("아무도 안 쓰는 이메일이면 available true")
    void check_email_available() throws Exception {
        mockMvc.perform(get("/api/auth/check-email").param("email", "free@test.com"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.available").value(true));
    }

    @Test
    @DisplayName("이미 가입된 이메일이면 대소문자가 달라도 available false")
    void check_email_taken_case_insensitive() throws Exception {
        signup("taken@test.com", "선점닉네임");

        mockMvc.perform(get("/api/auth/check-email").param("email", "TAKEN@test.com"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.available").value(false));
    }

    @Test
    @DisplayName("이메일 형식이 틀리면 400 VALIDATION_FAILED")
    void check_email_invalid_format() throws Exception {
        mockMvc.perform(get("/api/auth/check-email").param("email", "not-an-email"))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("VALIDATION_FAILED"));
    }

    @Test
    @DisplayName("아무도 안 쓰는 닉네임이면 available true")
    void check_nickname_available() throws Exception {
        mockMvc.perform(get("/api/auth/check-nickname").param("nickname", "새닉네임"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.available").value(true));
    }

    @Test
    @DisplayName("이미 쓰는 닉네임이면 available false")
    void check_nickname_taken() throws Exception {
        signup("nick@test.com", "쓰는중닉네임");

        mockMvc.perform(get("/api/auth/check-nickname").param("nickname", "쓰는중닉네임"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.available").value(false));
    }

    @Test
    @DisplayName("닉네임이 1자면 400 VALIDATION_FAILED")
    void check_nickname_too_short() throws Exception {
        mockMvc.perform(get("/api/auth/check-nickname").param("nickname", "a"))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("VALIDATION_FAILED"));
    }
}
