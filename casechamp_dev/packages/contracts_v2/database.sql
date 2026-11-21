-- PostgreSQL 17 target DDL. The future application provides UUIDv4 values.
-- Full API validation, current generation checks and transaction rules are normative in docs/03_backend.md.
BEGIN;
CREATE TABLE sandboxes (
 id uuid PRIMARY KEY,
 generation integer NOT NULL DEFAULT 1 CHECK(generation>=1),
 seed text NOT NULL CHECK(seed='CORE-001'),
 bundle_enabled boolean NOT NULL,
 created_at timestamptz NOT NULL,
 expires_at timestamptz NOT NULL CHECK(expires_at>created_at)
);
CREATE TABLE actors (
 sandbox_id uuid NOT NULL REFERENCES sandboxes(id) ON DELETE CASCADE,
 id uuid NOT NULL,
 generation integer NOT NULL CHECK(generation>=1),
 role text NOT NULL CHECK(role IN ('OWNER','OPERATOR','BUYER')),
 created_at timestamptz NOT NULL,
 revoked_at timestamptz,
 PRIMARY KEY(sandbox_id,id)
);
CREATE UNIQUE INDEX one_live_owner_per_sandbox ON actors(sandbox_id) WHERE role='OWNER' AND revoked_at IS NULL;
CREATE TABLE bootstraps (
 id uuid PRIMARY KEY,
 token_hash bytea UNIQUE NOT NULL CHECK(octet_length(token_hash)=32),
 csrf_hash bytea NOT NULL CHECK(octet_length(csrf_hash)=32),
 created_at timestamptz NOT NULL,
 expires_at timestamptz NOT NULL CHECK(expires_at>created_at)
);
CREATE TABLE sessions (
 id uuid PRIMARY KEY,
 sandbox_id uuid NOT NULL,
 actor_id uuid NOT NULL,
 token_hash bytea UNIQUE NOT NULL CHECK(octet_length(token_hash)=32),
 csrf_hash bytea NOT NULL CHECK(octet_length(csrf_hash)=32),
 created_at timestamptz NOT NULL,
 expires_at timestamptz NOT NULL CHECK(expires_at>created_at),
 revoked_at timestamptz,
 FOREIGN KEY(sandbox_id,actor_id) REFERENCES actors(sandbox_id,id)
);
CREATE TABLE role_links (
 id uuid PRIMARY KEY,
 sandbox_id uuid NOT NULL,
 generation integer NOT NULL CHECK(generation>=1),
 created_by uuid NOT NULL,
 role text NOT NULL CHECK(role IN ('OPERATOR','BUYER')),
 token_hash bytea UNIQUE NOT NULL CHECK(octet_length(token_hash)=32),
 created_at timestamptz NOT NULL,
 expires_at timestamptz NOT NULL CHECK(expires_at>created_at),
 consumed_at timestamptz,
 consumed_by_bootstrap uuid REFERENCES bootstraps(id),
 resulting_session_id uuid REFERENCES sessions(id),
 FOREIGN KEY(sandbox_id,created_by) REFERENCES actors(sandbox_id,id),
 CHECK((consumed_at IS NULL AND consumed_by_bootstrap IS NULL AND resulting_session_id IS NULL) OR (consumed_at IS NOT NULL AND consumed_by_bootstrap IS NOT NULL AND resulting_session_id IS NOT NULL))
);
CREATE TABLE catalog_skus (
 sandbox_id uuid NOT NULL REFERENCES sandboxes(id) ON DELETE CASCADE,
 generation integer NOT NULL CHECK(generation>=1),
 sku varchar(40) NOT NULL CHECK(sku ~ '^[A-Z0-9_-]+$'),
 title varchar(120) NOT NULL CHECK(length(title)>=1),
 price_minor bigint NOT NULL CHECK(price_minor BETWEEN 1 AND 100000000),
 stock_on_hand integer NOT NULL CHECK(stock_on_hand BETWEEN 0 AND 99999),
 stock_reserved integer NOT NULL DEFAULT 0 CHECK(stock_reserved BETWEEN 0 AND stock_on_hand),
 active boolean NOT NULL,
 ready_in_days integer NOT NULL CHECK(ready_in_days BETWEEN 1 AND 30),
 units_per_pack integer NOT NULL CHECK(units_per_pack BETWEEN 1 AND 100),
 revision integer NOT NULL DEFAULT 1 CHECK(revision>=1),
 terms_revision integer NOT NULL DEFAULT 1 CHECK(terms_revision>=1),
 updated_at timestamptz NOT NULL,
 PRIMARY KEY(sandbox_id,generation,sku)
);
CREATE TABLE bundle_templates (
 sandbox_id uuid NOT NULL REFERENCES sandboxes(id) ON DELETE CASCADE,
 generation integer NOT NULL CHECK(generation>=1),
 id uuid NOT NULL,
 version_no integer NOT NULL CHECK(version_no>=1),
 code text NOT NULL CHECK(code='B001'),
 snapshot jsonb NOT NULL CHECK(jsonb_typeof(snapshot)='object'),
 created_at timestamptz NOT NULL,
 PRIMARY KEY(sandbox_id,generation,id,version_no),
 UNIQUE(sandbox_id,generation,id)
);
CREATE TABLE offers (
 sandbox_id uuid NOT NULL REFERENCES sandboxes(id) ON DELETE CASCADE,
 generation integer NOT NULL CHECK(generation>=1),
 id uuid NOT NULL,
 mode text NOT NULL CHECK(mode IN ('FIXED','BUNDLE')),
 state text NOT NULL CHECK(state IN ('DRAFT','ACTIVE','PAUSED','ARCHIVED')),
 revision integer NOT NULL DEFAULT 1 CHECK(revision>=1),
 draft_revision integer NOT NULL DEFAULT 1 CHECK(draft_revision>=1),
 draft jsonb NOT NULL CHECK(jsonb_typeof(draft)='object'),
 published_version_no integer CHECK(published_version_no>=1),
 created_by uuid NOT NULL,
 created_at timestamptz NOT NULL,
 updated_at timestamptz NOT NULL,
 PRIMARY KEY(sandbox_id,generation,id),
 FOREIGN KEY(sandbox_id,created_by) REFERENCES actors(sandbox_id,id),
 CHECK((state='DRAFT' AND published_version_no IS NULL) OR state='ARCHIVED' OR (state IN ('ACTIVE','PAUSED') AND published_version_no IS NOT NULL))
);
CREATE TABLE offer_versions (
 sandbox_id uuid NOT NULL,
 generation integer NOT NULL,
 offer_id uuid NOT NULL,
 version_no integer NOT NULL CHECK(version_no>=1),
 snapshot jsonb NOT NULL CHECK(jsonb_typeof(snapshot)='object'),
 approved_terms_hash char(64) NOT NULL CHECK(approved_terms_hash ~ '^[0-9a-f]{64}$'),
 published_by uuid NOT NULL,
 published_at timestamptz NOT NULL,
 PRIMARY KEY(sandbox_id,generation,offer_id,version_no),
 FOREIGN KEY(sandbox_id,generation,offer_id) REFERENCES offers(sandbox_id,generation,id),
 FOREIGN KEY(sandbox_id,published_by) REFERENCES actors(sandbox_id,id)
);
ALTER TABLE offers ADD CONSTRAINT published_offer_version_exists FOREIGN KEY(sandbox_id,generation,id,published_version_no) REFERENCES offer_versions(sandbox_id,generation,offer_id,version_no) DEFERRABLE INITIALLY DEFERRED;
CREATE TABLE bundle_solutions (
 sandbox_id uuid NOT NULL,
 generation integer NOT NULL,
 id uuid NOT NULL,
 buyer_id uuid NOT NULL,
 offer_id uuid NOT NULL,
 offer_version_no integer NOT NULL,
 status text NOT NULL CHECK(status IN ('SOLVED','NO_SOLUTION')),
 snapshot jsonb NOT NULL CHECK(jsonb_typeof(snapshot)='object'),
 solution_hash char(64),
 created_at timestamptz NOT NULL,
 expires_at timestamptz NOT NULL CHECK(expires_at>created_at),
 PRIMARY KEY(sandbox_id,generation,id),
 FOREIGN KEY(sandbox_id,buyer_id) REFERENCES actors(sandbox_id,id),
 FOREIGN KEY(sandbox_id,generation,offer_id,offer_version_no) REFERENCES offer_versions(sandbox_id,generation,offer_id,version_no),
 CHECK((status='SOLVED' AND solution_hash IS NOT NULL AND solution_hash ~ '^[0-9a-f]{64}$') OR (status='NO_SOLUTION' AND solution_hash IS NULL))
);
CREATE TABLE quotes (
 sandbox_id uuid NOT NULL,
 generation integer NOT NULL,
 id uuid NOT NULL,
 buyer_id uuid NOT NULL,
 offer_id uuid NOT NULL,
 offer_version_no integer NOT NULL,
 kind text NOT NULL CHECK(kind IN ('OFFER','BUNDLE')),
 solution_id uuid,
 snapshot jsonb NOT NULL CHECK(jsonb_typeof(snapshot)='object'),
 catalog_terms_hash char(64) NOT NULL CHECK(catalog_terms_hash ~ '^[0-9a-f]{64}$'),
 total_minor bigint NOT NULL CHECK(total_minor BETWEEN 0 AND 100000000),
 created_at timestamptz NOT NULL,
 expires_at timestamptz NOT NULL CHECK(expires_at>created_at),
 PRIMARY KEY(sandbox_id,generation,id),
 FOREIGN KEY(sandbox_id,buyer_id) REFERENCES actors(sandbox_id,id),
 FOREIGN KEY(sandbox_id,generation,offer_id,offer_version_no) REFERENCES offer_versions(sandbox_id,generation,offer_id,version_no),
 FOREIGN KEY(sandbox_id,generation,solution_id) REFERENCES bundle_solutions(sandbox_id,generation,id),
 CHECK((kind='OFFER' AND solution_id IS NULL) OR (kind='BUNDLE' AND solution_id IS NOT NULL))
);
CREATE TABLE reservations (
 sandbox_id uuid NOT NULL,
 generation integer NOT NULL,
 id uuid NOT NULL,
 quote_id uuid NOT NULL,
 buyer_id uuid NOT NULL,
 offer_id uuid NOT NULL,
 offer_version_no integer NOT NULL,
 state text NOT NULL CHECK(state IN ('ACTIVE','CONFIRMED','CANCELLED','EXPIRED')),
 revision integer NOT NULL DEFAULT 1 CHECK(revision>=1),
 created_at timestamptz NOT NULL,
 expires_at timestamptz NOT NULL CHECK(expires_at>created_at),
 closed_at timestamptz,
 cancel_reason text CHECK(cancel_reason IN ('BUYER_CANCELLED','OFFER_UNAVAILABLE')),
 PRIMARY KEY(sandbox_id,generation,id),
 UNIQUE(sandbox_id,generation,quote_id),
 FOREIGN KEY(sandbox_id,generation,quote_id) REFERENCES quotes(sandbox_id,generation,id),
 FOREIGN KEY(sandbox_id,buyer_id) REFERENCES actors(sandbox_id,id),
 FOREIGN KEY(sandbox_id,generation,offer_id,offer_version_no) REFERENCES offer_versions(sandbox_id,generation,offer_id,version_no),
 CHECK((state='ACTIVE' AND closed_at IS NULL AND cancel_reason IS NULL) OR (state='CANCELLED' AND closed_at IS NOT NULL AND cancel_reason IS NOT NULL) OR (state IN ('CONFIRMED','EXPIRED') AND closed_at IS NOT NULL AND cancel_reason IS NULL))
);
CREATE INDEX active_reservation_expiry ON reservations(sandbox_id,generation,expires_at,id) WHERE state='ACTIVE';
CREATE TABLE reservation_lines (
 sandbox_id uuid NOT NULL,
 generation integer NOT NULL,
 reservation_id uuid NOT NULL,
 sku varchar(40) NOT NULL,
 quantity integer NOT NULL CHECK(quantity BETWEEN 1 AND 990),
 PRIMARY KEY(sandbox_id,generation,reservation_id,sku),
 FOREIGN KEY(sandbox_id,generation,reservation_id) REFERENCES reservations(sandbox_id,generation,id),
 FOREIGN KEY(sandbox_id,generation,sku) REFERENCES catalog_skus(sandbox_id,generation,sku)
);
CREATE TABLE orders (
 sandbox_id uuid NOT NULL,
 generation integer NOT NULL,
 id uuid NOT NULL,
 reservation_id uuid NOT NULL,
 buyer_id uuid NOT NULL,
 status text NOT NULL CHECK(status IN ('CREATED','ACCEPTED','FULFILLED','CANCELLED')),
 revision integer NOT NULL DEFAULT 1 CHECK(revision>=1),
 snapshot jsonb NOT NULL CHECK(jsonb_typeof(snapshot)='object'),
 total_minor bigint NOT NULL CHECK(total_minor BETWEEN 0 AND 100000000),
 order_kind text NOT NULL DEFAULT 'DEMO' CHECK(order_kind='DEMO'),
 native_vk_order_id text CHECK(native_vk_order_id IS NULL),
 paid_at timestamptz CHECK(paid_at IS NULL),
 created_at timestamptz NOT NULL,
 accepted_at timestamptz,
 fulfilled_at timestamptz,
 cancelled_at timestamptz,
 cancel_reason text CHECK(cancel_reason IN ('CUSTOMER_REQUEST','CANNOT_FULFILL','DEMO_CORRECTION')),
 PRIMARY KEY(sandbox_id,generation,id),
 UNIQUE(sandbox_id,generation,reservation_id),
 FOREIGN KEY(sandbox_id,generation,reservation_id) REFERENCES reservations(sandbox_id,generation,id),
 FOREIGN KEY(sandbox_id,buyer_id) REFERENCES actors(sandbox_id,id),
 CHECK((status='CREATED' AND accepted_at IS NULL AND fulfilled_at IS NULL AND cancelled_at IS NULL AND cancel_reason IS NULL) OR (status='ACCEPTED' AND accepted_at IS NOT NULL AND fulfilled_at IS NULL AND cancelled_at IS NULL AND cancel_reason IS NULL) OR (status='FULFILLED' AND accepted_at IS NOT NULL AND fulfilled_at IS NOT NULL AND cancelled_at IS NULL AND cancel_reason IS NULL) OR (status='CANCELLED' AND fulfilled_at IS NULL AND cancelled_at IS NOT NULL AND cancel_reason IS NOT NULL))
);
CREATE TABLE idempotency_receipts (
 id bigserial PRIMARY KEY,
 scope_kind text NOT NULL CHECK(scope_kind IN ('BOOTSTRAP','ACTOR')),
 scope_owner_id uuid NOT NULL,
 sandbox_id uuid,
 method text NOT NULL CHECK(method IN ('POST','PATCH')),
 normalized_path text NOT NULL,
 key uuid NOT NULL,
 request_hash bytea NOT NULL CHECK(octet_length(request_hash)=32),
 response_status integer NOT NULL CHECK(response_status BETWEEN 200 AND 299),
 response_data jsonb NOT NULL,
 cookie_session_id uuid,
 cookie_expires_at timestamptz,
 created_at timestamptz NOT NULL,
 expires_at timestamptz NOT NULL CHECK(expires_at>created_at),
 CHECK((scope_kind='ACTOR' AND sandbox_id IS NOT NULL) OR scope_kind='BOOTSTRAP')
);
CREATE UNIQUE INDEX idempotency_scope_unique ON idempotency_receipts(scope_kind,scope_owner_id,COALESCE(sandbox_id,'00000000-0000-0000-0000-000000000000'::uuid),method,normalized_path,key);
CREATE TABLE events (
 sandbox_id uuid NOT NULL REFERENCES sandboxes(id) ON DELETE CASCADE,
 generation integer NOT NULL,
 event_id uuid NOT NULL,
 origin text NOT NULL CHECK(origin IN ('SERVER','UI')),
 name text NOT NULL,
 occurred_at timestamptz NOT NULL,
 payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object'),
 is_test boolean NOT NULL CHECK(is_test),
 PRIMARY KEY(sandbox_id,generation,event_id)
);
CREATE TABLE outbox (
 id bigserial PRIMARY KEY,
 sandbox_id uuid NOT NULL,
 generation integer NOT NULL,
 event_id uuid NOT NULL,
 created_at timestamptz NOT NULL,
 delivered_at timestamptz,
 attempts integer NOT NULL DEFAULT 0 CHECK(attempts>=0),
 next_attempt_at timestamptz NOT NULL,
 UNIQUE(sandbox_id,generation,event_id),
 FOREIGN KEY(sandbox_id,generation,event_id) REFERENCES events(sandbox_id,generation,event_id)
);
CREATE FUNCTION reject_snapshot_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'immutable snapshot table %',TG_TABLE_NAME USING ERRCODE='55000'; END;
$$;
CREATE TRIGGER offer_version_immutable BEFORE UPDATE ON offer_versions FOR EACH ROW EXECUTE FUNCTION reject_snapshot_update();
CREATE TRIGGER quote_immutable BEFORE UPDATE ON quotes FOR EACH ROW EXECUTE FUNCTION reject_snapshot_update();
CREATE TRIGGER bundle_solution_immutable BEFORE UPDATE ON bundle_solutions FOR EACH ROW EXECUTE FUNCTION reject_snapshot_update();
CREATE TRIGGER bundle_template_immutable BEFORE UPDATE ON bundle_templates FOR EACH ROW EXECUTE FUNCTION reject_snapshot_update();
-- Deletion only by TTL housekeeping as a dependency-ordered purge; no application delete endpoints.
-- All stock changes and effects run after SELECT sandboxes ... FOR UPDATE.
-- All current-resource queries include sandbox_id AND generation = sandboxes.generation.
COMMIT;
