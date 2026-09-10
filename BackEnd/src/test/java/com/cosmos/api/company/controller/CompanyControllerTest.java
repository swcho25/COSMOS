package com.cosmos.api.company.controller;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.cosmos.api.company.dto.CompanyDetailResponse;
import com.cosmos.api.company.dto.CompanyIndustryResponse;
import com.cosmos.api.company.dto.CompanySummaryResponse;
import com.cosmos.api.company.dto.IndustryListResponse;
import com.cosmos.api.company.dto.IndustryReferenceResponse;
import com.cosmos.api.company.dto.IndustryResponse;
import com.cosmos.api.company.repository.CompanySearchCondition;
import com.cosmos.api.company.service.CompanyService;
import com.cosmos.api.global.error.GlobalExceptionHandler;
import com.cosmos.api.global.response.CursorPageResponse;
import java.util.List;
import java.util.UUID;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

class CompanyControllerTest {

    private static final UUID COMPANY_ID = UUID.fromString("20000000-0000-0000-0000-000000000001");
    private static final UUID INDUSTRY_ID = UUID.fromString("10000000-0000-0000-0000-000000000001");

    private CompanyService companyService;
    private MockMvc mockMvc;

    @BeforeEach
    void setUp() {
        companyService = mock(CompanyService.class);
        mockMvc = MockMvcBuilders.standaloneSetup(new CompanyController(companyService))
                .setControllerAdvice(new GlobalExceptionHandler())
                .build();
    }

    @Test
    void returnsCompanyCursorPageWithoutSuccessWrapper() throws Exception {
        CompanySummaryResponse company = new CompanySummaryResponse(
                COMPANY_ID,
                "테스트기업",
                "Test Company",
                "T00001",
                "KOSPI",
                new IndustryReferenceResponse(INDUSTRY_ID, "반도체"),
                false);
        when(companyService.findCompanies(any()))
                .thenReturn(CursorPageResponse.of(List.of(company), "next-token", true));

        mockMvc.perform(get("/api/companies")
                        .param("keyword", "테스트")
                        .param("market", "kospi")
                        .param("industryId", INDUSTRY_ID.toString())
                        .param("cursor", "cursor-token")
                        .param("size", "10"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.items[0].companyId").value(COMPANY_ID.toString()))
                .andExpect(jsonPath("$.items[0].primaryIndustry.industryId").value(INDUSTRY_ID.toString()))
                .andExpect(jsonPath("$.items[0].watched").value(false))
                .andExpect(jsonPath("$.nextCursor").value("next-token"))
                .andExpect(jsonPath("$.hasNext").value(true));

        ArgumentCaptor<CompanySearchCondition> captor = ArgumentCaptor.forClass(CompanySearchCondition.class);
        verify(companyService).findCompanies(captor.capture());
        CompanySearchCondition condition = captor.getValue();
        org.assertj.core.api.Assertions.assertThat(condition.keyword()).isEqualTo("테스트");
        org.assertj.core.api.Assertions.assertThat(condition.market()).isEqualTo("KOSPI");
        org.assertj.core.api.Assertions.assertThat(condition.industryId()).isEqualTo(INDUSTRY_ID);
        org.assertj.core.api.Assertions.assertThat(condition.cursor()).isEqualTo("cursor-token");
        org.assertj.core.api.Assertions.assertThat(condition.size()).isEqualTo(10);
    }

    @Test
    void returnsCompanyDetail() throws Exception {
        when(companyService.findCompany(COMPANY_ID)).thenReturn(new CompanyDetailResponse(
                COMPANY_ID,
                "테스트기업",
                "Test Company",
                "T00001",
                "KOSPI",
                "기업 설명",
                List.of(new CompanyIndustryResponse(INDUSTRY_ID, "반도체", true)),
                false));

        mockMvc.perform(get("/api/companies/{companyId}", COMPANY_ID))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.companyId").value(COMPANY_ID.toString()))
                .andExpect(jsonPath("$.industries[0].primary").value(true));
    }

    @Test
    void returnsIndustryList() throws Exception {
        when(companyService.findIndustries()).thenReturn(new IndustryListResponse(List.of(
                new IndustryResponse(INDUSTRY_ID, null, "반도체", "산업 설명", 2))));

        mockMvc.perform(get("/api/industries"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.items[0].industryId").value(INDUSTRY_ID.toString()))
                .andExpect(jsonPath("$.items[0].companyCount").value(2));
    }
}
