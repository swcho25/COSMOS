package com.cosmos.api.company.controller;

import com.cosmos.api.company.dto.CompanyDetailResponse;
import com.cosmos.api.company.dto.CompanySummaryResponse;
import com.cosmos.api.company.dto.IndustryListResponse;
import com.cosmos.api.company.repository.CompanySearchCondition;
import com.cosmos.api.company.service.CompanyService;
import com.cosmos.api.global.response.CursorPageResponse;
import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.Size;
import java.util.UUID;
import org.springframework.validation.annotation.Validated;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

@Validated
@RestController
@RequestMapping("/api")
public class CompanyController {

    private final CompanyService companyService;

    public CompanyController(CompanyService companyService) {
        this.companyService = companyService;
    }

    @GetMapping("/companies")
    public CursorPageResponse<CompanySummaryResponse> findCompanies(
            @RequestParam(required = false) @Size(max = 200) String keyword,
            @RequestParam(required = false) @Size(max = 30) String market,
            @RequestParam(required = false) UUID industryId,
            @RequestParam(required = false) String cursor,
            @RequestParam(defaultValue = "20") @Min(1) @Max(100) int size
    ) {
        return companyService.findCompanies(
                new CompanySearchCondition(keyword, market, industryId, cursor, size));
    }

    @GetMapping("/companies/{companyId}")
    public CompanyDetailResponse findCompany(@PathVariable UUID companyId) {
        return companyService.findCompany(companyId);
    }

    @GetMapping("/industries")
    public IndustryListResponse findIndustries() {
        return companyService.findIndustries();
    }
}
