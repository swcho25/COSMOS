package com.cosmos.api.community.service;

import java.time.Instant;
import java.util.UUID;

/**
 * 댓글 목록의 "여기까지 봤다" 표시.
 * 작성 시각이 같은 댓글이 있을 수 있어 댓글 번호까지 함께 기억한다.
 */
public record CommentCursor(
        Instant createdAt,
        UUID commentId
) {
}
