package com.cosmos.api.user.controller;

import com.cosmos.api.global.security.AuthUser;
import com.cosmos.api.user.dto.NewsScrapResponse;
import com.cosmos.api.user.service.NewsScrapService;
import java.util.UUID;
import lombok.RequiredArgsConstructor;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/** 내 뉴스 스크랩 API. 모두 Access Token이 필요하다. */
@RestController
@RequestMapping("/api/users/me/scraps")
@RequiredArgsConstructor
public class NewsScrapController {

    private final NewsScrapService newsScrapService;

    /** 뉴스 스크랩 등록. 바디 없이 뉴스 번호만 주소에 담는다. */
    @PostMapping("/{newsId}")
    public ResponseEntity<NewsScrapResponse> register(
            @AuthenticationPrincipal AuthUser authUser,
            @PathVariable UUID newsId
    ) {
        NewsScrapResponse response = newsScrapService.register(authUser.userId(), newsId);
        return ResponseEntity.status(HttpStatus.CREATED).body(response);
    }

    /** 뉴스 스크랩 해제. 이미 해제된 상태여도 204(멱등). */
    @DeleteMapping("/{newsId}")
    public ResponseEntity<Void> unregister(
            @AuthenticationPrincipal AuthUser authUser,
            @PathVariable UUID newsId
    ) {
        newsScrapService.unregister(authUser.userId(), newsId);
        return ResponseEntity.noContent().build();
    }
}
