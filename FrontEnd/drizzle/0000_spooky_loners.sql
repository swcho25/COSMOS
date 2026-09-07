CREATE TABLE `news_items` (
	`storage_key` text NOT NULL,
	`article_id` text NOT NULL,
	`scenario_id` text NOT NULL,
	`source_url` text NOT NULL,
	`published_at_iso` text NOT NULL,
	`collected_at_iso` text NOT NULL,
	`provider` text NOT NULL,
	`payload_json` text NOT NULL,
	PRIMARY KEY(`storage_key`, `article_id`)
);
--> statement-breakpoint
CREATE INDEX `idx_news_items_storage_published` ON `news_items` (`storage_key`,`published_at_iso`);