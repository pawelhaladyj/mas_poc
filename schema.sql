-- schemat (dla pewności)
CREATE SCHEMA IF NOT EXISTS mas AUTHORIZATION mas_poc_user;
ALTER DATABASE mas_poc_db SET search_path = mas, public;

-- tabele
CREATE TABLE IF NOT EXISTS mas.facts (
  id              bigserial PRIMARY KEY,
  conversation_id text    NOT NULL,
  fact_key        text    NOT NULL,
  fact_value      jsonb   NOT NULL,
  version         integer NOT NULL DEFAULT 1,
  is_current      boolean NOT NULL DEFAULT true,
  source          text,
  confidence      numeric(4,3),
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT facts_unique_ver UNIQUE (conversation_id, fact_key, version)
);
CREATE INDEX IF NOT EXISTS facts_idx_conv_key_current
  ON mas.facts (conversation_id, fact_key) WHERE is_current;

CREATE TABLE IF NOT EXISTS mas.offers (
  id              bigserial PRIMARY KEY,
  conversation_id text    NOT NULL,
  offer_payload   jsonb   NOT NULL,
  status          text    NOT NULL DEFAULT 'draft',
  version         integer NOT NULL DEFAULT 1,
  is_current      boolean NOT NULL DEFAULT true,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT offers_unique_ver UNIQUE (conversation_id, version)
);
CREATE INDEX IF NOT EXISTS offers_idx_conv_current
  ON mas.offers (conversation_id) WHERE is_current;

-- funkcje i triggery
CREATE OR REPLACE FUNCTION mas.touch_updated_at() RETURNS trigger AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION mas.facts_bi() RETURNS trigger AS $$
DECLARE v integer;
BEGIN
  SELECT max(version) INTO v
  FROM mas.facts
  WHERE conversation_id = NEW.conversation_id AND fact_key = NEW.fact_key;
  IF v IS NOT NULL THEN NEW.version := v + 1; END IF;
  NEW.is_current := true;
  NEW.created_at := COALESCE(NEW.created_at, now());
  NEW.updated_at := now();
  RETURN NEW;
END$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION mas.facts_ai() RETURNS trigger AS $$
BEGIN
  UPDATE mas.facts
     SET is_current = false, updated_at = now()
   WHERE conversation_id = NEW.conversation_id
     AND fact_key       = NEW.fact_key
     AND id            <> NEW.id
     AND is_current     = true;
  RETURN NULL;
END$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION mas.offers_bi() RETURNS trigger AS $$
DECLARE v integer;
BEGIN
  SELECT max(version) INTO v
  FROM mas.offers
  WHERE conversation_id = NEW.conversation_id;
  IF v IS NOT NULL THEN NEW.version := v + 1; END IF;
  NEW.is_current := true;
  NEW.created_at := COALESCE(NEW.created_at, now());
  NEW.updated_at := now();
  RETURN NEW;
END$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION mas.offers_ai() RETURNS trigger AS $$
BEGIN
  UPDATE mas.offers
     SET is_current = false, updated_at = now()
   WHERE conversation_id = NEW.conversation_id
     AND id            <> NEW.id
     AND is_current     = true;
  RETURN NULL;
END$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS facts_bi ON mas.facts;
CREATE TRIGGER facts_bi  BEFORE INSERT ON mas.facts  FOR EACH ROW EXECUTE FUNCTION mas.facts_bi();
DROP TRIGGER IF EXISTS facts_ai ON mas.facts;
CREATE TRIGGER facts_ai  AFTER  INSERT ON mas.facts  FOR EACH ROW EXECUTE FUNCTION mas.facts_ai();
DROP TRIGGER IF EXISTS facts_bu ON mas.facts;
CREATE TRIGGER facts_bu  BEFORE UPDATE ON mas.facts FOR EACH ROW EXECUTE FUNCTION mas.touch_updated_at();

DROP TRIGGER IF EXISTS offers_bi ON mas.offers;
CREATE TRIGGER offers_bi BEFORE INSERT ON mas.offers FOR EACH ROW EXECUTE FUNCTION mas.offers_bi();
DROP TRIGGER IF EXISTS offers_ai ON mas.offers;
CREATE TRIGGER offers_ai AFTER  INSERT ON mas.offers FOR EACH ROW EXECUTE FUNCTION mas.offers_ai();
DROP TRIGGER IF EXISTS offers_bu ON mas.offers;
CREATE TRIGGER offers_bu BEFORE UPDATE ON mas.offers FOR EACH ROW EXECUTE FUNCTION mas.touch_updated_at();
