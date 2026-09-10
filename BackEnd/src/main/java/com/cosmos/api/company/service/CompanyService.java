package com.cosmos.api.company.service;

import com.cosmos.api.company.dto.CompanyDetailResponse;
import com.cosmos.api.company.dto.CompanyIndustryResponse;
import com.cosmos.api.company.dto.CompanySummaryResponse;
import com.cosmos.api.company.dto.IndustryListResponse;
import com.cosmos.api.company.dto.IndustryReferenceResponse;
import com.cosmos.api.company.dto.IndustryResponse;
import com.cosmos.api.company.entity.Company;
import com.cosmos.api.company.error.CompanyErrorCode;
import com.cosmos.api.company.repository.CompanyIndustryRepository;
import com.cosmos.api.company.repository.CompanyQueryRepository;
import com.cosmos.api.company.repository.CompanyRepository;
import com.cosmos.api.company.repository.CompanySearchCondition;
import com.cosmos.api.company.repository.CompanySearchRow;
import com.cosmos.api.global.error.BusinessException;
import com.cosmos.api.global.response.CursorPageResponse;
import java.util.List;
import java.util.Objects;
import java.util.UUID;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
@Transactional(readOnly = true)
public class CompanyService {

    private final CompanyRepository companyRepository;
    private final CompanyIndustryRepository companyIndustryRepository;
    private final CompanyQueryRepository companyQueryRepository;
    private final CompanyCursorCodec cursorCodec;

    public CompanyService(
            CompanyRepository companyRepository,
            CompanyIndustryRepository companyIndustryRepository,
            CompanyQueryRepository companyQueryRepository,
            CompanyCursorCodec cursorCodec
    ) {
        this.companyRepository = companyRepository;
        this.companyIndustryRepository = companyIndustryRepository;
        this.companyQueryRepository = companyQueryRepository;
        this.cursorCodec = cursorCodec;
    }

    public CursorPageResponse<CompanySummaryResponse> findCompanies(CompanySearchCondition condition) {
        CompanyCursor cursor = cursorCodec.decode(condition.cursor());
        List<CompanySearchRow> rows = companyQueryRepository.search(
                condition,
                cursor == null ? null : cursor.searchRank(),
                cursor == null ? null : cursor.companyName(),
                cursor == null ? null : cursor.companyId());

        boolean hasNext = rows.size() > condition.size();
        List<CompanySearchRow> pageRows = hasNext ? rows.subList(0, condition.size()) : rows;
        List<CompanySummaryResponse> items = pageRows.stream()
                .map(this::toSummary)
                .toList();

        String nextCursor = null;
        if (hasNext && !pageRows.isEmpty()) {
            CompanySearchRow last = pageRows.getLast();
            nextCursor = cursorCodec.encode(new CompanyCursor(
                    last.searchRank(),
                    last.name(),
                    last.companyId()));
        }

        return CursorPageResponse.of(items, nextCursor, hasNext);
    }

    public CompanyDetailResponse findCompany(UUID companyId) {
        Company company = companyRepository.findActiveById(companyId)
                .orElseThrow(() -> new BusinessException(CompanyErrorCode.COMPANY_NOT_FOUND));
        List<CompanyIndustryResponse> industries = companyIndustryRepository
                .findAllWithIndustryByCompanyId(companyId).stream()
                .map(CompanyIndustryResponse::from)
                .toList();

        return CompanyDetailResponse.of(company, industries, false);
    }

    public IndustryListResponse findIndustries() {
        List<IndustryResponse> items = companyQueryRepository.findIndustries().stream()
                .map(row -> new IndustryResponse(
                        row.industryId(),
                        row.parentIndustryId(),
                        row.name(),
                        Objects.toString(row.description(), ""),
                        row.companyCount()))
                .toList();
        return new IndustryListResponse(items);
    }

    private CompanySummaryResponse toSummary(CompanySearchRow row) {
        return new CompanySummaryResponse(
                row.companyId(),
                row.name(),
                Objects.toString(row.nameEn(), ""),
                Objects.toString(row.stockCode(), ""),
                Objects.toString(row.market(), ""),
                new IndustryReferenceResponse(row.primaryIndustryId(), row.primaryIndustryName()),
                false);
    }
}
