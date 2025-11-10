-- KB schema (jeśli nie korzystasz z Alembic)
CREATE TABLE IF NOT EXISTS kb_items (
id bigserial PRIMARY KEY,
key text NOT NULL,
version integer NOT NULL,
etag uuid NOT NULL,
content_type text NOT NULL,
value jsonb NOT NULL,
tags text[] NOT NULL DEFAULT '{}',
session_id text,
created_at timestamptz NOT NULL DEFAULT now(),
created_by text NOT NULL,
deleted boolean NOT NULL DEFAULT false
);
CREATE UNIQUE INDEX IF NOT EXISTS kb_items_key_version_uq ON kb_items(key, version);
CREATE INDEX IF NOT EXISTS kb_items_key_desc_idx ON kb_items(key, version DESC);
CREATE INDEX IF NOT EXISTS kb_items_session_idx ON kb_items(session_id);
CREATE INDEX IF NOT EXISTS kb_items_tags_gin ON kb_items USING GIN (tags);