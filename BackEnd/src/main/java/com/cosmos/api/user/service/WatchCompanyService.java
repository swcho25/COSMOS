package com.cosmos.api.user.service;

import com.cosmos.api.company.entity.Company;
import com.cosmos.api.company.error.CompanyErrorCode;
import com.cosmos.api.company.repository.CompanyRepository;
import com.cosmos.api.global.error.BusinessException;
import com.cosmos.api.global.security.AuthErrorCode;
import com.cosmos.api.user.dto.WatchCompanyResponse;
import com.cosmos.api.user.entity.User;
import com.cosmos.api.user.entity.UserWatchCompany;
import com.cosmos.api.user.entity.UserWatchCompanyId;
import com.cosmos.api.user.error.UserErrorCode;
import com.cosmos.api.user.repository.UserRepository;
import com.cosmos.api.user.repository.UserWatchCompanyRepository;
import java.util.UUID;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/** 관심 기업 등록·해제. 사용자는 자기 관심 목록만 다룰 수 있다(토큰의 사용자 번호 기준). */
@Service
@RequiredArgsConstructor
public class WatchCompanyService {

    private final UserWatchCompanyRepository watchRepository;
    private final CompanyRepository companyRepository;
    private final UserRepository userRepository;

    /** 등록. 없는 기업이면 404, 이미 등록한 기업이면 409. */
    @Transactional
    public WatchCompanyResponse register(Long userId, UUID companyId) {
        Company company = companyRepository.findActiveById(companyId)
                .orElseThrow(() -> new BusinessException(CompanyErrorCode.COMPANY_NOT_FOUND));
        User user = findActiveUser(userId);

        if (watchRepository.existsById(new UserWatchCompanyId(userId, companyId))) {
            throw new BusinessException(UserErrorCode.WATCH_COMPANY_DUPLICATED);
        }

        UserWatchCompany saved = watchRepository.save(UserWatchCompany.create(user, company));
        return WatchCompanyResponse.from(saved);
    }

    /** 해제. 등록돼 있지 않아도 조용히 성공한다(멱등). 없는 기업 번호여도 마찬가지다. */
    @Transactional
    public void unregister(Long userId, UUID companyId) {
        watchRepository.findById(new UserWatchCompanyId(userId, companyId))
                .ifPresent(watchRepository::delete);
    }

    // 토큰은 30분간 살아 있어서, 그 사이 탈퇴한 회원의 토큰이 들어올 수 있다
    private User findActiveUser(Long userId) {
        return userRepository.findById(userId)
                .orElseThrow(() -> new BusinessException(AuthErrorCode.TOKEN_INVALID));
    }
}
