-- Runs once, the first time the Postgres volume is created.
-- Cheape Deal uses the POSTGRES_DB database (schema "cheapedeal"); n8n gets its own database.
CREATE DATABASE n8n;
