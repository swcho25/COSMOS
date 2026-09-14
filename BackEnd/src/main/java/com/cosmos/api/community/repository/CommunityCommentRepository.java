package com.cosmos.api.community.repository;

import com.cosmos.api.community.entity.CommunityComment;
import java.util.UUID;
import org.springframework.data.jpa.repository.JpaRepository;

public interface CommunityCommentRepository extends JpaRepository<CommunityComment, UUID> {
}
