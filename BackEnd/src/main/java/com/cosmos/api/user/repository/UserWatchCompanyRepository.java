package com.cosmos.api.user.repository;

import com.cosmos.api.user.entity.UserWatchCompany;
import com.cosmos.api.user.entity.UserWatchCompanyId;
import org.springframework.data.jpa.repository.JpaRepository;

public interface UserWatchCompanyRepository extends JpaRepository<UserWatchCompany, UserWatchCompanyId> {
}
