package com.cosmos.api.community.service;

import com.cosmos.api.community.dto.CommentResponse;
import com.cosmos.api.community.entity.CommunityComment;
import com.cosmos.api.community.error.CommunityErrorCode;
import com.cosmos.api.community.repository.CommunityCommentRepository;
import com.cosmos.api.company.entity.Company;
import com.cosmos.api.company.error.CompanyErrorCode;
import com.cosmos.api.company.repository.CompanyRepository;
import com.cosmos.api.global.error.BusinessException;
import com.cosmos.api.global.security.AuthErrorCode;
import com.cosmos.api.user.entity.User;
import com.cosmos.api.user.repository.UserRepository;
import java.util.UUID;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/** 기업 커뮤니티 댓글 작성·수정·삭제. 수정과 삭제는 본인 댓글만 가능하다. */
@Service
@RequiredArgsConstructor
public class CommunityCommentService {

    private final CommunityCommentRepository commentRepository;
    private final CompanyRepository companyRepository;
    private final UserRepository userRepository;

    @Transactional
    public CommentResponse create(Long userId, UUID companyId, String content) {
        Company company = companyRepository.findActiveById(companyId)
                .orElseThrow(() -> new BusinessException(CompanyErrorCode.COMPANY_NOT_FOUND));
        User user = findActiveUser(userId);

        CommunityComment comment = CommunityComment.create(company, user, content);
        return CommentResponse.from(commentRepository.save(comment));
    }

    @Transactional
    public CommentResponse update(Long userId, UUID commentId, String content) {
        CommunityComment comment = findOwnComment(userId, commentId);
        comment.changeContent(content);
        // 수정 시각(updatedAt)은 DB 반영 시점에 채워지므로, 응답에 담기 전에 먼저 반영시킨다
        commentRepository.flush();
        return CommentResponse.from(comment);
    }

    @Transactional
    public void delete(Long userId, UUID commentId) {
        commentRepository.delete(findOwnComment(userId, commentId));
    }

    /**
     * 댓글을 찾고 "내가 쓴 것인지"까지 확인한다.
     * 없으면 404, 남의 댓글이면 403으로 구분해 알려준다.
     */
    private CommunityComment findOwnComment(Long userId, UUID commentId) {
        CommunityComment comment = commentRepository.findById(commentId)
                .orElseThrow(() -> new BusinessException(CommunityErrorCode.COMMENT_NOT_FOUND));

        if (!comment.isWrittenBy(userId)) {
            throw new BusinessException(CommunityErrorCode.COMMENT_FORBIDDEN);
        }
        return comment;
    }

    // 토큰은 30분간 살아 있어서, 그 사이 탈퇴한 회원의 토큰이 들어올 수 있다
    private User findActiveUser(Long userId) {
        return userRepository.findById(userId)
                .orElseThrow(() -> new BusinessException(AuthErrorCode.TOKEN_INVALID));
    }
}
