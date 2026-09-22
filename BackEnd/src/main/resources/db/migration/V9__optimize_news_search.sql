-- Support case-insensitive substring searches such as lower(title) LIKE '%keyword%'.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Live Loader documents served from public until they are included in an analysis run.
CREATE INDEX ix_source_document_news_title_trgm
    ON public.source_document
    USING gin (lower(title) gin_trgm_ops)
    WHERE document_type = 'NEWS' AND title IS NOT NULL;

-- Immutable analysis-run documents. run_id is filtered separately by its btree indexes;
-- PostgreSQL can combine those indexes with this trigram index through a bitmap scan.
CREATE INDEX ix_analysis_source_document_news_title_trgm
    ON cosmos_analysis.source_document
    USING gin (lower(title) gin_trgm_ops)
    WHERE document_type = 'NEWS' AND title IS NOT NULL;

-- A news page needs the highest-relevance visible company mention for each document.
CREATE INDEX ix_company_document_visible_document_relevance
    ON public.company_document (document_id, relevance_score DESC, company_id)
    INCLUDE (sentiment)
    WHERE is_service_visible = TRUE;

CREATE INDEX ix_analysis_company_document_visible_document_relevance
    ON cosmos_analysis.company_document
        (run_id, document_id, relevance_score DESC, company_id)
    INCLUDE (sentiment)
    WHERE is_service_visible = TRUE;
