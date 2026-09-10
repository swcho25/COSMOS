package com.cosmos.api.company.repository;

import com.cosmos.api.company.entity.CompanyIndustry;
import com.cosmos.api.company.entity.CompanyIndustryId;
import java.util.List;
import java.util.UUID;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

public interface CompanyIndustryRepository extends JpaRepository<CompanyIndustry, CompanyIndustryId> {

    @Query("""
            select ci
            from CompanyIndustry ci
            join fetch ci.industry i
            where ci.company.companyId = :companyId
            order by ci.primary desc, i.name asc
            """)
    List<CompanyIndustry> findAllWithIndustryByCompanyId(@Param("companyId") UUID companyId);
}
