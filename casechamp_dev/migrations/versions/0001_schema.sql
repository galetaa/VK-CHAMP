-- PostgreSQL 17 reference DDL, specification 1.0.0.
-- TARGET STRUCTURE FOR IMPLEMENTATION MIGRATIONS. NOT AN EXECUTED DATABASE.
-- This file does not create an application, run a migration runner, or seed data.
-- Migration owner and application login must be different roles. The application
-- must not be a superuser/table owner and must not receive DDL or DELETE rights.
-- All commerce writes first lock sandboxes by sandbox_id FOR UPDATE. ML/solve
-- runs outside that transaction. The application always filters sandbox+epoch.
-- Composite FKs protect tenant references; they do NOT provide read-level RLS.

BEGIN;
CREATE SCHEMA vk_demo;
REVOKE ALL ON SCHEMA vk_demo FROM PUBLIC;
SET LOCAL search_path = vk_demo, pg_catalog;

CREATE DOMAIN positive_version AS integer CHECK (VALUE BETWEEN 1 AND 2147483647);
CREATE DOMAIN sha256_hex AS text CHECK (VALUE ~ '^[a-f0-9]{64}$');
CREATE DOMAIN slot_id AS text CHECK (VALUE IN
 ('dinner_plate','dessert_plate','drinking_glass','cutlery_set','napkin','tablecloth'));
CREATE DOMAIN product_color AS text CHECK (VALUE IN
 ('WHITE','BEIGE','BLACK','BLUE','GREEN','RED','TRANSPARENT','SILVER','GOLD','MULTICOLOR','UNKNOWN'));

CREATE TABLE sandboxes (
 sandbox_id uuid PRIMARY KEY,
 session_token_hash bytea NOT NULL UNIQUE CHECK (octet_length(session_token_hash)=32),
 current_epoch positive_version NOT NULL,
 status text NOT NULL CHECK (status IN ('ACTIVE','EXPIRED')),
 created_at timestamptz NOT NULL,
 expires_at timestamptz NOT NULL,
 CHECK (expires_at = created_at + interval '24 hours')
);
COMMENT ON TABLE sandboxes IS 'Mutex: every short commerce transaction SELECTs this row FOR UPDATE before reading clock_timestamp or changing child rows.';
CREATE INDEX sandboxes_expiry_idx ON sandboxes(expires_at) WHERE status='ACTIVE';

CREATE TABLE sandbox_epochs (
 sandbox_id uuid NOT NULL REFERENCES sandboxes(sandbox_id) ON DELETE CASCADE,
 epoch positive_version NOT NULL,
 catalog_version positive_version NOT NULL DEFAULT 1,
 fulfillment_version positive_version NOT NULL DEFAULT 1,
 created_at timestamptz NOT NULL,
 closed_at timestamptz,
 PRIMARY KEY(sandbox_id,epoch),
 CHECK (closed_at IS NULL OR closed_at>=created_at)
);
ALTER TABLE sandboxes ADD CONSTRAINT sandboxes_current_epoch_fk
 FOREIGN KEY(sandbox_id,current_epoch) REFERENCES sandbox_epochs(sandbox_id,epoch)
 DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE assets (
 sandbox_id uuid NOT NULL,
 epoch positive_version NOT NULL,
 asset_id uuid NOT NULL,
 kind text NOT NULL CHECK (kind IN ('IMAGE','VIDEO','PRODUCT_IMAGE')),
 content_type text NOT NULL CHECK (content_type IN ('image/jpeg','image/png','image/webp','video/mp4')),
 sha256 sha256_hex NOT NULL,
 storage_key text NOT NULL CHECK (length(storage_key) BETWEEN 1 AND 500),
 size_bytes bigint NOT NULL CHECK (size_bytes BETWEEN 1 AND 52428800),
 width integer NOT NULL CHECK (width BETWEEN 1 AND 8192),
 height integer NOT NULL CHECK (height BETWEEN 1 AND 8192),
 duration_ms integer,
 created_at timestamptz NOT NULL,
 PRIMARY KEY(sandbox_id,epoch,asset_id),
 FOREIGN KEY(sandbox_id,epoch) REFERENCES sandbox_epochs(sandbox_id,epoch),
 CHECK ((kind='VIDEO' AND content_type='video/mp4' AND duration_ms IS NOT NULL AND duration_ms BETWEEN 1 AND 60000)
     OR (kind<>'VIDEO' AND content_type<>'video/mp4' AND duration_ms IS NULL))
);
CREATE INDEX assets_digest_idx ON assets(sandbox_id,epoch,sha256);

CREATE TABLE products (
 sandbox_id uuid NOT NULL,
 epoch positive_version NOT NULL,
 sku_id text COLLATE "C" NOT NULL CHECK (sku_id ~ '^[A-Z0-9][A-Z0-9_-]{0,63}$'),
 slot_id slot_id NOT NULL,
 title text NOT NULL CHECK (length(title) BETWEEN 1 AND 120),
 color product_color NOT NULL,
 pack_units integer NOT NULL CHECK (pack_units BETWEEN 1 AND 24),
 price_minor bigint NOT NULL CHECK (price_minor BETWEEN 1 AND 10000000),
 currency text NOT NULL DEFAULT 'RUB' CHECK (currency='RUB'),
 active boolean NOT NULL,
 handling_days integer NOT NULL CHECK (handling_days BETWEEN 0 AND 14),
 length_cm integer,
 width_cm integer,
 image_asset_id uuid,
 product_version positive_version NOT NULL,
 created_at timestamptz NOT NULL,
 updated_at timestamptz NOT NULL,
 PRIMARY KEY(sandbox_id,epoch,sku_id),
 UNIQUE(sandbox_id,epoch,sku_id,slot_id),
 FOREIGN KEY(sandbox_id,epoch) REFERENCES sandbox_epochs(sandbox_id,epoch),
 FOREIGN KEY(sandbox_id,epoch,image_asset_id) REFERENCES assets(sandbox_id,epoch,asset_id),
 CHECK ((slot_id='tablecloth' AND length_cm IS NOT NULL AND width_cm IS NOT NULL
          AND length_cm BETWEEN 1 AND 400 AND width_cm BETWEEN 1 AND 400)
     OR (slot_id<>'tablecloth' AND length_cm IS NULL AND width_cm IS NULL)),
 CHECK (updated_at>=created_at)
);
CREATE INDEX products_slot_idx ON products(sandbox_id,epoch,slot_id,sku_id);

CREATE TABLE product_versions (
 sandbox_id uuid NOT NULL,
 epoch positive_version NOT NULL,
 sku_id text COLLATE "C" NOT NULL,
 product_version positive_version NOT NULL,
 product_snapshot jsonb NOT NULL CHECK (jsonb_typeof(product_snapshot)='object'),
 snapshot_hash sha256_hex NOT NULL,
 created_at timestamptz NOT NULL,
 PRIMARY KEY(sandbox_id,epoch,sku_id,product_version),
 FOREIGN KEY(sandbox_id,epoch,sku_id) REFERENCES products(sandbox_id,epoch,sku_id)
);
COMMENT ON COLUMN product_versions.product_snapshot IS 'Immutable commercial product fields only; live stock/reservation counters are excluded.';
ALTER TABLE products ADD CONSTRAINT products_current_version_fk
 FOREIGN KEY(sandbox_id,epoch,sku_id,product_version)
 REFERENCES product_versions(sandbox_id,epoch,sku_id,product_version)
 DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE inventory (
 sandbox_id uuid NOT NULL,
 epoch positive_version NOT NULL,
 sku_id text COLLATE "C" NOT NULL,
 stock_on_hand_packs integer NOT NULL CHECK (stock_on_hand_packs BETWEEN 0 AND 10000),
 reserved_packs integer NOT NULL CHECK (reserved_packs BETWEEN 0 AND 10000),
 returnable_packs integer NOT NULL CHECK (returnable_packs BETWEEN 0 AND 10000),
 inventory_version positive_version NOT NULL,
 updated_at timestamptz NOT NULL,
 PRIMARY KEY(sandbox_id,epoch,sku_id),
 FOREIGN KEY(sandbox_id,epoch,sku_id) REFERENCES products(sandbox_id,epoch,sku_id),
 CHECK (reserved_packs<=stock_on_hand_packs),
 CHECK (stock_on_hand_packs+returnable_packs<=10000)
);
COMMENT ON COLUMN inventory.returnable_packs IS 'Internal liability: sum of line pack_count in TEST_CONFIRMED, uncanceled orders. Confirm increments; order cancel decrements.';
COMMENT ON COLUMN inventory.reserved_packs IS 'Materialized ACTIVE reservations. Expiry sweep releases once. Solve availability excludes logically expired reservations before worker materialization.';

CREATE TABLE fulfillment_terms (
 sandbox_id uuid NOT NULL,
 epoch positive_version NOT NULL,
 fulfillment_version positive_version NOT NULL,
 pickup_fee_minor bigint NOT NULL CHECK (pickup_fee_minor=0),
 local_delivery_fee_minor bigint NOT NULL CHECK (local_delivery_fee_minor=30000),
 pickup_transit_days integer NOT NULL CHECK (pickup_transit_days=0),
 local_delivery_transit_days integer NOT NULL CHECK (local_delivery_transit_days=1),
 timezone text NOT NULL CHECK (timezone='Europe/Moscow'),
 terms_snapshot jsonb NOT NULL CHECK (jsonb_typeof(terms_snapshot)='object'),
 created_at timestamptz NOT NULL,
 PRIMARY KEY(sandbox_id,epoch,fulfillment_version),
 FOREIGN KEY(sandbox_id,epoch) REFERENCES sandbox_epochs(sandbox_id,epoch)
);
ALTER TABLE sandbox_epochs ADD CONSTRAINT sandbox_epoch_fulfillment_fk
 FOREIGN KEY(sandbox_id,epoch,fulfillment_version)
 REFERENCES fulfillment_terms(sandbox_id,epoch,fulfillment_version)
 DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE catalog_imports (
 sandbox_id uuid NOT NULL,
 epoch positive_version NOT NULL,
 import_id uuid NOT NULL,
 status text NOT NULL CHECK (status IN ('VALID','INVALID','COMMITTED','EXPIRED')),
 source_sha256 sha256_hex NOT NULL,
 base_catalog_version positive_version NOT NULL,
 normalized_rows jsonb NOT NULL CHECK (jsonb_typeof(normalized_rows)='array'),
 validation_report jsonb NOT NULL CHECK (jsonb_typeof(validation_report)='object'),
 row_count integer NOT NULL CHECK (row_count BETWEEN 0 AND 500),
 created_at timestamptz NOT NULL,
 expires_at timestamptz NOT NULL,
 committed_at timestamptz,
 PRIMARY KEY(sandbox_id,epoch,import_id),
 FOREIGN KEY(sandbox_id,epoch) REFERENCES sandbox_epochs(sandbox_id,epoch),
 CHECK (expires_at=created_at+interval '900 seconds'),
 CHECK ((status='COMMITTED')=(committed_at IS NOT NULL)),
 CHECK (jsonb_array_length(normalized_rows)<=500)
);
CREATE INDEX imports_expiry_idx ON catalog_imports(sandbox_id,epoch,expires_at) WHERE status='VALID';

CREATE TABLE scenarios (
 sandbox_id uuid NOT NULL,
 epoch positive_version NOT NULL,
 scenario_id uuid NOT NULL,
 draft_version positive_version NOT NULL,
 title text NOT NULL CHECK (length(title) BETWEEN 1 AND 80),
 caption text NOT NULL CHECK (length(caption)<=5000),
 asset_id uuid,
 slots jsonb NOT NULL CHECK (jsonb_typeof(slots)='array' AND jsonb_array_length(slots)=6),
 created_at timestamptz NOT NULL,
 updated_at timestamptz NOT NULL,
 PRIMARY KEY(sandbox_id,epoch,scenario_id),
 FOREIGN KEY(sandbox_id,epoch) REFERENCES sandbox_epochs(sandbox_id,epoch),
 FOREIGN KEY(sandbox_id,epoch,asset_id) REFERENCES assets(sandbox_id,epoch,asset_id),
 CHECK (updated_at>=created_at)
);

CREATE TABLE scenario_revisions (
 sandbox_id uuid NOT NULL,
 epoch positive_version NOT NULL,
 scenario_revision_id uuid NOT NULL,
 scenario_id uuid NOT NULL,
 revision_number positive_version NOT NULL,
 status text NOT NULL CHECK (status IN ('PUBLISHED','WITHDRAWN')),
 immutable_snapshot jsonb NOT NULL CHECK (jsonb_typeof(immutable_snapshot)='object'),
 template_hash sha256_hex NOT NULL,
 published_at timestamptz NOT NULL,
 withdrawn_at timestamptz,
 PRIMARY KEY(sandbox_id,epoch,scenario_revision_id),
 UNIQUE(sandbox_id,epoch,scenario_id,revision_number),
 FOREIGN KEY(sandbox_id,epoch,scenario_id) REFERENCES scenarios(sandbox_id,epoch,scenario_id),
 CHECK ((status='WITHDRAWN')=(withdrawn_at IS NOT NULL)),
 CHECK (withdrawn_at IS NULL OR withdrawn_at>=published_at)
);
CREATE INDEX revisions_latest_idx ON scenario_revisions(sandbox_id,epoch,scenario_id,revision_number DESC);

CREATE TABLE scenario_revision_items (
 sandbox_id uuid NOT NULL,
 epoch positive_version NOT NULL,
 scenario_revision_id uuid NOT NULL,
 slot_id slot_id NOT NULL,
 sku_id text COLLATE "C" NOT NULL,
 role text NOT NULL CHECK (role IN ('PRIMARY','ALTERNATIVE')),
 PRIMARY KEY(sandbox_id,epoch,scenario_revision_id,slot_id,sku_id),
 FOREIGN KEY(sandbox_id,epoch,scenario_revision_id)
   REFERENCES scenario_revisions(sandbox_id,epoch,scenario_revision_id),
 FOREIGN KEY(sandbox_id,epoch,sku_id,slot_id) REFERENCES products(sandbox_id,epoch,sku_id,slot_id)
);
CREATE UNIQUE INDEX one_primary_per_slot_idx
 ON scenario_revision_items(sandbox_id,epoch,scenario_revision_id,slot_id) WHERE role='PRIMARY';
COMMENT ON TABLE scenario_revision_items IS 'Relational approved-candidate projection. Publish service validates all six primary slots and exact equality with immutable_snapshot in one transaction.';

CREATE TABLE ml_jobs (
 sandbox_id uuid NOT NULL,
 epoch positive_version NOT NULL,
 job_id uuid NOT NULL,
 kind text NOT NULL CHECK (kind IN ('ANALYZE_SCENARIO','PARSE_INTENT')),
 status text NOT NULL CHECK (status IN ('QUEUED','RUNNING','SUCCEEDED','FAILED','TIMED_OUT','CANCELED')),
 scenario_id uuid,
 source_draft_version positive_version,
 input_snapshot jsonb NOT NULL CHECK (jsonb_typeof(input_snapshot)='object'),
 result jsonb,
 failure_code text,
 created_at timestamptz NOT NULL,
 started_at timestamptz,
 deadline_at timestamptz NOT NULL,
 finished_at timestamptz,
 PRIMARY KEY(sandbox_id,epoch,job_id),
 FOREIGN KEY(sandbox_id,epoch) REFERENCES sandbox_epochs(sandbox_id,epoch),
 FOREIGN KEY(sandbox_id,epoch,scenario_id) REFERENCES scenarios(sandbox_id,epoch,scenario_id),
 CHECK (result IS NULL OR jsonb_typeof(result)='object'),
 CHECK ((status IN ('QUEUED','RUNNING'))=(finished_at IS NULL)),
 CHECK (status<>'SUCCEEDED' OR result IS NOT NULL),
 CHECK (finished_at IS NULL OR finished_at>=created_at),
 CHECK (started_at IS NULL OR (started_at>=created_at AND started_at<created_at+interval '60 seconds')),
 CHECK (status<>'QUEUED' OR started_at IS NULL),
 CHECK (status NOT IN ('RUNNING','SUCCEEDED') OR started_at IS NOT NULL),
 CHECK (finished_at IS NULL OR started_at IS NULL OR finished_at>=started_at),
 CHECK (deadline_at>created_at),
 CHECK (deadline_at=CASE WHEN started_at IS NULL THEN
     created_at+CASE WHEN kind='ANALYZE_SCENARIO' THEN interval '180 seconds' ELSE interval '90 seconds' END
   ELSE least(
     created_at+CASE WHEN kind='ANALYZE_SCENARIO' THEN interval '180 seconds' ELSE interval '90 seconds' END,
     started_at+CASE WHEN kind='ANALYZE_SCENARIO' THEN interval '120 seconds' ELSE interval '30 seconds' END)
   END),
 CHECK (status<>'SUCCEEDED' OR finished_at<deadline_at)
);
CREATE INDEX jobs_queue_idx ON ml_jobs(created_at,job_id) WHERE status='QUEUED';
CREATE INDEX jobs_running_deadline_idx ON ml_jobs(deadline_at,job_id) WHERE status='RUNNING';
COMMENT ON TABLE ml_jobs IS 'Queue wait is strictly less than 60 seconds. Initial deadline: analysis180s/intent90s; after start min(initial,started_at+analysis120s/intent30s). At most two unfinished jobs per sandbox is checked by service under sandbox mutex.';

CREATE TABLE solve_runs (
 sandbox_id uuid NOT NULL,
 epoch positive_version NOT NULL,
 solve_run_id uuid NOT NULL,
 scenario_revision_id uuid NOT NULL,
 client_run_id uuid NOT NULL,
 algorithm_version text NOT NULL CHECK (length(algorithm_version) BETWEEN 1 AND 80),
 request_hash sha256_hex NOT NULL,
 snapshot_hash sha256_hex NOT NULL,
 business_date date NOT NULL,
 immutable_input jsonb NOT NULL CHECK (jsonb_typeof(immutable_input)='object'),
 result_code text NOT NULL CHECK (result_code IN
   ('QUOTED','INFEASIBLE','NO_PURCHASE_REQUIRED','COMPUTATION_LIMIT','COMPUTATION_ABORTED','INTERNAL_INVARIANT_FAILED')),
 diagnostics jsonb NOT NULL CHECK (jsonb_typeof(diagnostics)='object'),
 created_at timestamptz NOT NULL,
 finished_at timestamptz NOT NULL,
 PRIMARY KEY(sandbox_id,epoch,solve_run_id),
 FOREIGN KEY(sandbox_id,epoch,scenario_revision_id)
   REFERENCES scenario_revisions(sandbox_id,epoch,scenario_revision_id),
 CHECK (finished_at>=created_at)
);
CREATE INDEX solve_runs_metrics_idx ON solve_runs(sandbox_id,epoch,result_code,created_at);

CREATE TABLE quotes (
 sandbox_id uuid NOT NULL,
 epoch positive_version NOT NULL,
 quote_id uuid NOT NULL,
 solve_run_id uuid NOT NULL,
 scenario_revision_id uuid NOT NULL,
 status text NOT NULL CHECK (status IN ('ACTIVE','RESERVED','CONFIRMED','EXPIRED','STALE','CANCELED')),
 aggregate_version positive_version NOT NULL,
 request_hash sha256_hex NOT NULL,
 quote_hash sha256_hex NOT NULL,
 solution_hash sha256_hex NOT NULL,
 snapshot_hash sha256_hex NOT NULL,
 algorithm_version text NOT NULL CHECK (length(algorithm_version) BETWEEN 1 AND 80),
 immutable_terms jsonb NOT NULL CHECK (jsonb_typeof(immutable_terms)='object'),
 business_date date NOT NULL,
 earliest_date date NOT NULL,
 needed_by date,
 fulfillment_version positive_version NOT NULL,
 currency text NOT NULL CHECK (currency='RUB'),
 subtotal_minor bigint NOT NULL CHECK (subtotal_minor BETWEEN 1 AND 5000000),
 shipping_minor bigint NOT NULL CHECK (shipping_minor IN (0,30000)),
 total_minor bigint NOT NULL CHECK (total_minor BETWEEN 1 AND 5000000),
 created_at timestamptz NOT NULL,
 expires_at timestamptz NOT NULL,
 updated_at timestamptz NOT NULL,
 PRIMARY KEY(sandbox_id,epoch,quote_id),
 UNIQUE(sandbox_id,epoch,solve_run_id),
 UNIQUE(sandbox_id,epoch,quote_id,quote_hash),
 FOREIGN KEY(sandbox_id,epoch,solve_run_id) REFERENCES solve_runs(sandbox_id,epoch,solve_run_id),
 FOREIGN KEY(sandbox_id,epoch,scenario_revision_id)
   REFERENCES scenario_revisions(sandbox_id,epoch,scenario_revision_id),
 FOREIGN KEY(sandbox_id,epoch,fulfillment_version)
   REFERENCES fulfillment_terms(sandbox_id,epoch,fulfillment_version),
 CHECK (subtotal_minor+shipping_minor=total_minor),
 CHECK (earliest_date>=business_date),
 CHECK (needed_by IS NULL OR earliest_date<=needed_by),
 CHECK (expires_at>created_at AND expires_at<=created_at+interval '900 seconds'),
 CHECK (updated_at>=created_at)
);
CREATE INDEX quotes_expiry_idx ON quotes(sandbox_id,epoch,expires_at) WHERE status='ACTIVE';
CREATE INDEX quotes_revision_idx ON quotes(sandbox_id,epoch,scenario_revision_id,status);
COMMENT ON COLUMN quotes.immutable_terms IS 'Exact consent-bearing normalized request + QuoteLine snapshots + totals + dates + source versions. Hash is computed by canonical JSON in application, not by PostgreSQL jsonb text rendering.';

CREATE TABLE quote_lines (
 sandbox_id uuid NOT NULL,
 epoch positive_version NOT NULL,
 quote_id uuid NOT NULL,
 slot_id slot_id NOT NULL,
 sku_id text COLLATE "C" NOT NULL,
 product_version positive_version NOT NULL,
 pack_units integer NOT NULL CHECK (pack_units BETWEEN 1 AND 24),
 pack_count integer NOT NULL CHECK (pack_count BETWEEN 1 AND 12),
 required_units integer NOT NULL CHECK (required_units BETWEEN 1 AND 12),
 effective_owned_units integer NOT NULL CHECK (effective_owned_units BETWEEN 0 AND 12),
 delivered_units integer NOT NULL CHECK (delivered_units BETWEEN 1 AND 288),
 excess_units integer NOT NULL CHECK (excess_units BETWEEN 0 AND 23),
 unit_pack_price_minor bigint NOT NULL CHECK (unit_pack_price_minor BETWEEN 1 AND 10000000),
 line_total_minor bigint NOT NULL CHECK (line_total_minor BETWEEN 1 AND 120000000),
 line_snapshot jsonb NOT NULL CHECK (jsonb_typeof(line_snapshot)='object'),
 PRIMARY KEY(sandbox_id,epoch,quote_id,slot_id),
 UNIQUE(sandbox_id,epoch,quote_id,sku_id),
 UNIQUE(sandbox_id,epoch,quote_id,sku_id,pack_count),
 FOREIGN KEY(sandbox_id,epoch,quote_id) REFERENCES quotes(sandbox_id,epoch,quote_id),
 FOREIGN KEY(sandbox_id,epoch,sku_id,slot_id) REFERENCES products(sandbox_id,epoch,sku_id,slot_id),
 FOREIGN KEY(sandbox_id,epoch,sku_id,product_version)
   REFERENCES product_versions(sandbox_id,epoch,sku_id,product_version),
 CHECK (effective_owned_units<required_units),
 CHECK (delivered_units=pack_units*pack_count),
 CHECK (effective_owned_units+delivered_units=required_units+excess_units),
 CHECK (pack_count=(required_units-effective_owned_units+pack_units-1)/pack_units),
 CHECK (line_total_minor=unit_pack_price_minor*pack_count),
 CHECK (slot_id<>'tablecloth' OR required_units=1)
);
CREATE INDEX quote_lines_sku_idx ON quote_lines(sandbox_id,epoch,sku_id,quote_id);

CREATE TABLE reservations (
 sandbox_id uuid NOT NULL,
 epoch positive_version NOT NULL,
 reservation_id uuid NOT NULL,
 quote_id uuid NOT NULL,
 accepted_quote_hash sha256_hex NOT NULL,
 status text NOT NULL CHECK (status IN ('ACTIVE','CONFIRMED','CANCELED','EXPIRED','INVALIDATED')),
 aggregate_version positive_version NOT NULL,
 immutable_terms jsonb NOT NULL CHECK (jsonb_typeof(immutable_terms)='object'),
 created_at timestamptz NOT NULL,
 expires_at timestamptz NOT NULL,
 completed_at timestamptz,
 reason_code text,
 PRIMARY KEY(sandbox_id,epoch,reservation_id),
 UNIQUE(sandbox_id,epoch,quote_id),
 UNIQUE(sandbox_id,epoch,reservation_id,quote_id),
 FOREIGN KEY(sandbox_id,epoch,quote_id,accepted_quote_hash)
   REFERENCES quotes(sandbox_id,epoch,quote_id,quote_hash),
 CHECK (expires_at>created_at AND expires_at<=created_at+interval '600 seconds'),
 CHECK ((status='ACTIVE')=(completed_at IS NULL)),
 CHECK (completed_at IS NULL OR completed_at>=created_at),
 CHECK (reason_code IS NULL OR length(reason_code) BETWEEN 1 AND 80)
);
CREATE INDEX reservations_due_idx ON reservations(expires_at,sandbox_id,epoch,reservation_id) WHERE status='ACTIVE';

CREATE TABLE reservation_lines (
 sandbox_id uuid NOT NULL,
 epoch positive_version NOT NULL,
 reservation_id uuid NOT NULL,
 quote_id uuid NOT NULL,
 sku_id text COLLATE "C" NOT NULL,
 pack_count integer NOT NULL CHECK (pack_count BETWEEN 1 AND 12),
 PRIMARY KEY(sandbox_id,epoch,reservation_id,sku_id),
 FOREIGN KEY(sandbox_id,epoch,reservation_id,quote_id)
   REFERENCES reservations(sandbox_id,epoch,reservation_id,quote_id),
 FOREIGN KEY(sandbox_id,epoch,quote_id,sku_id,pack_count)
   REFERENCES quote_lines(sandbox_id,epoch,quote_id,sku_id,pack_count)
);
CREATE INDEX reservation_lines_inventory_idx ON reservation_lines(sandbox_id,epoch,sku_id,reservation_id);

CREATE TABLE orders (
 sandbox_id uuid NOT NULL,
 epoch positive_version NOT NULL,
 order_id uuid NOT NULL,
 order_number text NOT NULL CHECK (length(order_number) BETWEEN 1 AND 40),
 reservation_id uuid NOT NULL,
 quote_id uuid NOT NULL,
 status text NOT NULL CHECK (status IN ('TEST_CONFIRMED','CANCELED')),
 aggregate_version positive_version NOT NULL,
 is_test boolean NOT NULL DEFAULT true CHECK (is_test),
 payment_status text NOT NULL DEFAULT 'NOT_APPLICABLE' CHECK (payment_status='NOT_APPLICABLE'),
 native_vk_order_id text CHECK (native_vk_order_id IS NULL),
 immutable_terms jsonb NOT NULL CHECK (jsonb_typeof(immutable_terms)='object'),
 currency text NOT NULL CHECK (currency='RUB'),
 subtotal_minor bigint NOT NULL CHECK (subtotal_minor BETWEEN 1 AND 5000000),
 shipping_minor bigint NOT NULL CHECK (shipping_minor IN (0,30000)),
 total_minor bigint NOT NULL CHECK (total_minor BETWEEN 1 AND 5000000),
 created_at timestamptz NOT NULL,
 canceled_at timestamptz,
 PRIMARY KEY(sandbox_id,epoch,order_id),
 UNIQUE(sandbox_id,epoch,reservation_id),
 UNIQUE(sandbox_id,epoch,order_number),
 UNIQUE(sandbox_id,epoch,order_id,quote_id),
 FOREIGN KEY(sandbox_id,epoch,reservation_id,quote_id)
   REFERENCES reservations(sandbox_id,epoch,reservation_id,quote_id),
 CHECK (subtotal_minor+shipping_minor=total_minor),
 CHECK ((status='CANCELED')=(canceled_at IS NOT NULL)),
 CHECK (canceled_at IS NULL OR canceled_at>=created_at)
);
CREATE INDEX orders_metrics_idx ON orders(sandbox_id,epoch,status,created_at);

CREATE TABLE order_lines (
 sandbox_id uuid NOT NULL,
 epoch positive_version NOT NULL,
 order_id uuid NOT NULL,
 quote_id uuid NOT NULL,
 sku_id text COLLATE "C" NOT NULL,
 pack_count integer NOT NULL CHECK (pack_count BETWEEN 1 AND 12),
 PRIMARY KEY(sandbox_id,epoch,order_id,sku_id),
 FOREIGN KEY(sandbox_id,epoch,order_id,quote_id) REFERENCES orders(sandbox_id,epoch,order_id,quote_id),
 FOREIGN KEY(sandbox_id,epoch,quote_id,sku_id,pack_count)
   REFERENCES quote_lines(sandbox_id,epoch,quote_id,sku_id,pack_count)
);
CREATE INDEX order_lines_returns_idx ON order_lines(sandbox_id,epoch,sku_id,order_id);

CREATE TABLE idempotency_records (
 sandbox_id uuid NOT NULL,
 epoch positive_version NOT NULL,
 operation_id text NOT NULL CHECK (operation_id IN
  ('resetSandbox','commitImport','patchSku','withdrawSku','createScenario','patchScenario',
   'publishScenario','withdrawScenarioRevision','analyzeScenario','parseIntent',
   'createQuote','reserveQuote','cancelReservation','confirmTestOrder','cancelTestOrder','runSimulation')),
 idempotency_key uuid NOT NULL,
 request_hash sha256_hex NOT NULL,
 response_status integer NOT NULL CHECK (response_status BETWEEN 200 AND 499),
 response_body jsonb NOT NULL CHECK (jsonb_typeof(response_body)='object'),
 resource_id uuid,
 created_at timestamptz NOT NULL,
 PRIMARY KEY(sandbox_id,epoch,operation_id,idempotency_key),
 FOREIGN KEY(sandbox_id,epoch) REFERENCES sandbox_epochs(sandbox_id,epoch)
);
COMMENT ON TABLE idempotency_records IS 'Completed receipts only, inserted atomically with effects. 503/unexpected rollback are not cached. operation_id is exact OpenAPI operationId; aliases are forbidden. resetSandbox receipt may be read from old epoch only for replay of that same reset command.';

CREATE TABLE outbox_events (
 sandbox_id uuid NOT NULL,
 epoch positive_version NOT NULL,
 event_id uuid NOT NULL,
 aggregate_type text NOT NULL CHECK (length(aggregate_type) BETWEEN 1 AND 40),
 aggregate_id text NOT NULL CHECK (length(aggregate_id) BETWEEN 1 AND 100),
 aggregate_version positive_version NOT NULL,
 event_name text NOT NULL CHECK (length(event_name) BETWEEN 1 AND 100),
 payload jsonb NOT NULL CHECK (jsonb_typeof(payload)='object'),
 occurred_at timestamptz NOT NULL,
 publish_status text NOT NULL CHECK (publish_status IN ('PENDING','CLAIMED','PUBLISHED')),
 attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count>=0),
 next_attempt_at timestamptz NOT NULL,
 lease_token uuid,
 lease_until timestamptz,
 published_at timestamptz,
 PRIMARY KEY(sandbox_id,epoch,event_id),
 UNIQUE(sandbox_id,epoch,aggregate_type,aggregate_id,aggregate_version,event_name),
 FOREIGN KEY(sandbox_id,epoch) REFERENCES sandbox_epochs(sandbox_id,epoch),
 CHECK ((publish_status='PUBLISHED')=(published_at IS NOT NULL)),
 CHECK ((publish_status='CLAIMED' AND lease_token IS NOT NULL AND lease_until IS NOT NULL)
     OR (publish_status<>'CLAIMED' AND lease_token IS NULL AND lease_until IS NULL))
);
CREATE INDEX outbox_ready_idx ON outbox_events(next_attempt_at,event_id) WHERE publish_status='PENDING';
CREATE INDEX outbox_lease_idx ON outbox_events(lease_until,event_id) WHERE publish_status='CLAIMED';

CREATE TABLE event_projection (
 sandbox_id uuid NOT NULL,
 epoch positive_version NOT NULL,
 event_id uuid NOT NULL,
 event_name text NOT NULL,
 occurred_at timestamptz NOT NULL,
 projected_at timestamptz NOT NULL,
 payload jsonb NOT NULL CHECK (jsonb_typeof(payload)='object'),
 PRIMARY KEY(sandbox_id,epoch,event_id),
 FOREIGN KEY(sandbox_id,epoch,event_id) REFERENCES outbox_events(sandbox_id,epoch,event_id)
);
CREATE INDEX event_projection_cursor_idx ON event_projection(sandbox_id,epoch,occurred_at,event_id);

CREATE TABLE client_events (
 sandbox_id uuid NOT NULL,
 epoch positive_version NOT NULL,
 event_id uuid NOT NULL,
 event_name text NOT NULL CHECK (event_name IN
  ('scenario_viewed','request_form_started','proposal_viewed','checkout_viewed')),
 occurred_at timestamptz NOT NULL,
 received_at timestamptz NOT NULL,
 client_run_id uuid NOT NULL,
 resource_id uuid,
 PRIMARY KEY(sandbox_id,epoch,event_id),
 FOREIGN KEY(sandbox_id,epoch) REFERENCES sandbox_epochs(sandbox_id,epoch)
);
COMMENT ON TABLE client_events IS 'Untrusted UI observations. Never authoritative for stock, orders, paid revenue, or server confirmed metrics.';

CREATE TABLE audit_log (
 audit_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 sandbox_id uuid NOT NULL,
 epoch positive_version NOT NULL,
 request_id uuid NOT NULL,
 client_run_id uuid,
 operation_id text NOT NULL CHECK (length(operation_id) BETWEEN 1 AND 100),
 demo_role text NOT NULL CHECK (demo_role IN ('BUYER','SELLER','WORKER','SYSTEM')),
 outcome_code text NOT NULL CHECK (length(outcome_code) BETWEEN 1 AND 100),
 resource_id uuid,
 occurred_at timestamptz NOT NULL,
 facts jsonb NOT NULL CHECK (jsonb_typeof(facts)='object'),
 FOREIGN KEY(sandbox_id,epoch) REFERENCES sandbox_epochs(sandbox_id,epoch)
);
CREATE INDEX audit_trace_idx ON audit_log(sandbox_id,epoch,request_id,audit_id);

CREATE TABLE verification_runs (
 sandbox_id uuid NOT NULL,
 epoch positive_version NOT NULL,
 verification_id uuid NOT NULL,
 quote_id uuid NOT NULL,
 snapshot_hash sha256_hex NOT NULL,
 verifier_version text NOT NULL CHECK (length(verifier_version) BETWEEN 1 AND 80),
 passed boolean NOT NULL,
 checks jsonb NOT NULL CHECK (jsonb_typeof(checks)='array'),
 checked_at timestamptz NOT NULL,
 PRIMARY KEY(sandbox_id,epoch,verification_id),
 FOREIGN KEY(sandbox_id,epoch,quote_id) REFERENCES quotes(sandbox_id,epoch,quote_id)
);

-- Generic immutability guard: only explicitly listed columns may change.
-- DELETE is not granted to application roles; expiry cleanup is a separate,
-- privileged maintenance operation after sandbox retention has elapsed.
CREATE FUNCTION guard_immutable_columns() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
 IF (to_jsonb(NEW) - coalesce(TG_ARGV,ARRAY[]::text[]))
    IS DISTINCT FROM (to_jsonb(OLD) - coalesce(TG_ARGV,ARRAY[]::text[])) THEN
   RAISE EXCEPTION 'immutable fields changed in %', TG_TABLE_NAME USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END;
$$;

CREATE FUNCTION guard_product_update() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE changed boolean;
BEGIN
 IF ROW(NEW.sandbox_id,NEW.epoch,NEW.sku_id,NEW.slot_id,NEW.pack_units,NEW.color,
        NEW.length_cm,NEW.width_cm,NEW.currency,NEW.created_at)
    IS DISTINCT FROM
    ROW(OLD.sandbox_id,OLD.epoch,OLD.sku_id,OLD.slot_id,OLD.pack_units,OLD.color,
        OLD.length_cm,OLD.width_cm,OLD.currency,OLD.created_at) THEN
   RAISE EXCEPTION 'physical SKU identity is immutable' USING ERRCODE='23514';
 END IF;
 changed := ROW(NEW.title,NEW.price_minor,NEW.handling_days,NEW.active,NEW.image_asset_id)
              IS DISTINCT FROM
            ROW(OLD.title,OLD.price_minor,OLD.handling_days,OLD.active,OLD.image_asset_id);
 IF OLD.active=false AND NEW.active=true THEN
   RAISE EXCEPTION 'withdrawn SKU cannot be reactivated in v1' USING ERRCODE='23514';
 END IF;
 IF NEW.product_version <> OLD.product_version + (CASE WHEN changed THEN 1 ELSE 0 END) THEN
   RAISE EXCEPTION 'incorrect product_version increment' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END;
$$;
CREATE TRIGGER products_guard BEFORE UPDATE ON products
 FOR EACH ROW EXECUTE FUNCTION guard_product_update();

CREATE FUNCTION guard_inventory_update() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE changed boolean;
BEGIN
 IF ROW(NEW.sandbox_id,NEW.epoch,NEW.sku_id) IS DISTINCT FROM ROW(OLD.sandbox_id,OLD.epoch,OLD.sku_id) THEN
   RAISE EXCEPTION 'inventory key is immutable' USING ERRCODE='23514';
 END IF;
 changed := ROW(NEW.stock_on_hand_packs,NEW.reserved_packs,NEW.returnable_packs)
              IS DISTINCT FROM
            ROW(OLD.stock_on_hand_packs,OLD.reserved_packs,OLD.returnable_packs);
 IF NEW.inventory_version <> OLD.inventory_version + (CASE WHEN changed THEN 1 ELSE 0 END) THEN
   RAISE EXCEPTION 'incorrect inventory_version increment' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END;
$$;
CREATE TRIGGER inventory_guard BEFORE UPDATE ON inventory
 FOR EACH ROW EXECUTE FUNCTION guard_inventory_update();

CREATE FUNCTION guard_commerce_state() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE allowed boolean := false;
BEGIN
 IF NEW.status=OLD.status THEN
   IF NEW.aggregate_version<>OLD.aggregate_version THEN
     RAISE EXCEPTION 'no-op status must not increment aggregate_version' USING ERRCODE='23514';
   END IF;
   RETURN NEW;
 END IF;
 IF TG_TABLE_NAME='quotes' THEN
   allowed := (OLD.status='ACTIVE' AND NEW.status IN ('RESERVED','EXPIRED','STALE','CANCELED'))
           OR (OLD.status='RESERVED' AND NEW.status IN ('CONFIRMED','EXPIRED','CANCELED'))
           OR (OLD.status='CONFIRMED' AND NEW.status='CANCELED');
 ELSIF TG_TABLE_NAME='reservations' THEN
   allowed := OLD.status='ACTIVE' AND NEW.status IN ('CONFIRMED','CANCELED','EXPIRED','INVALIDATED');
 ELSIF TG_TABLE_NAME='orders' THEN
   allowed := OLD.status='TEST_CONFIRMED' AND NEW.status='CANCELED';
 END IF;
 IF NOT allowed OR NEW.aggregate_version<>OLD.aggregate_version+1 THEN
   RAISE EXCEPTION 'invalid commerce state transition in %',TG_TABLE_NAME USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END;
$$;

CREATE FUNCTION guard_revision_state() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.status IS DISTINCT FROM OLD.status AND NOT (OLD.status='PUBLISHED' AND NEW.status='WITHDRAWN') THEN
   RAISE EXCEPTION 'withdrawn revision cannot be republished' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END;
$$;

CREATE FUNCTION guard_ml_job_update() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE allowed boolean;
BEGIN
 IF OLD.status IN ('SUCCEEDED','FAILED','TIMED_OUT','CANCELED') THEN
   IF to_jsonb(NEW) IS DISTINCT FROM to_jsonb(OLD) THEN
     RAISE EXCEPTION 'terminal ML job is immutable' USING ERRCODE='23514';
   END IF;
   RETURN NEW;
 END IF;
 allowed := (OLD.status='QUEUED' AND NEW.status IN ('QUEUED','RUNNING','FAILED','TIMED_OUT','CANCELED'))
         OR (OLD.status='RUNNING' AND NEW.status IN ('RUNNING','SUCCEEDED','FAILED','TIMED_OUT','CANCELED'));
 IF NOT allowed THEN
   RAISE EXCEPTION 'invalid ML job transition' USING ERRCODE='23514';
 END IF;
 IF OLD.started_at IS NOT NULL AND NEW.started_at IS DISTINCT FROM OLD.started_at THEN
   RAISE EXCEPTION 'ML started_at cannot be replaced' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END;
$$;

CREATE TRIGGER product_versions_immutable BEFORE UPDATE ON product_versions
 FOR EACH ROW EXECUTE FUNCTION guard_immutable_columns();
CREATE TRIGGER sandbox_identity_immutable BEFORE UPDATE ON sandboxes
 FOR EACH ROW EXECUTE FUNCTION guard_immutable_columns('current_epoch','status');
CREATE TRIGGER assets_immutable BEFORE UPDATE ON assets
 FOR EACH ROW EXECUTE FUNCTION guard_immutable_columns();
CREATE TRIGGER import_preview_immutable BEFORE UPDATE ON catalog_imports
 FOR EACH ROW EXECUTE FUNCTION guard_immutable_columns('status','committed_at');
CREATE TRIGGER fulfillment_terms_immutable BEFORE UPDATE ON fulfillment_terms
 FOR EACH ROW EXECUTE FUNCTION guard_immutable_columns();
CREATE TRIGGER scenario_revision_terms_immutable BEFORE UPDATE ON scenario_revisions
 FOR EACH ROW EXECUTE FUNCTION guard_immutable_columns('status','withdrawn_at');
CREATE TRIGGER scenario_revision_state_guard BEFORE UPDATE ON scenario_revisions
 FOR EACH ROW EXECUTE FUNCTION guard_revision_state();
CREATE TRIGGER scenario_revision_items_immutable BEFORE UPDATE ON scenario_revision_items
 FOR EACH ROW EXECUTE FUNCTION guard_immutable_columns();
CREATE TRIGGER solve_runs_immutable BEFORE UPDATE ON solve_runs
 FOR EACH ROW EXECUTE FUNCTION guard_immutable_columns();
CREATE TRIGGER ml_job_input_immutable BEFORE UPDATE ON ml_jobs
 FOR EACH ROW EXECUTE FUNCTION guard_immutable_columns('status','started_at','deadline_at','finished_at','result','failure_code');
CREATE TRIGGER ml_job_state_guard BEFORE UPDATE ON ml_jobs
 FOR EACH ROW EXECUTE FUNCTION guard_ml_job_update();
CREATE TRIGGER quote_terms_immutable BEFORE UPDATE ON quotes
 FOR EACH ROW EXECUTE FUNCTION guard_immutable_columns('status','aggregate_version','updated_at');
CREATE TRIGGER quote_state_guard BEFORE UPDATE ON quotes
 FOR EACH ROW EXECUTE FUNCTION guard_commerce_state();
CREATE TRIGGER quote_lines_immutable BEFORE UPDATE ON quote_lines
 FOR EACH ROW EXECUTE FUNCTION guard_immutable_columns();
CREATE TRIGGER reservation_terms_immutable BEFORE UPDATE ON reservations
 FOR EACH ROW EXECUTE FUNCTION guard_immutable_columns('status','aggregate_version','completed_at','reason_code');
CREATE TRIGGER reservation_state_guard BEFORE UPDATE ON reservations
 FOR EACH ROW EXECUTE FUNCTION guard_commerce_state();
CREATE TRIGGER reservation_lines_immutable BEFORE UPDATE ON reservation_lines
 FOR EACH ROW EXECUTE FUNCTION guard_immutable_columns();
CREATE TRIGGER order_terms_immutable BEFORE UPDATE ON orders
 FOR EACH ROW EXECUTE FUNCTION guard_immutable_columns('status','aggregate_version','canceled_at');
CREATE TRIGGER order_state_guard BEFORE UPDATE ON orders
 FOR EACH ROW EXECUTE FUNCTION guard_commerce_state();
CREATE TRIGGER order_lines_immutable BEFORE UPDATE ON order_lines
 FOR EACH ROW EXECUTE FUNCTION guard_immutable_columns();
CREATE TRIGGER idempotency_receipts_immutable BEFORE UPDATE ON idempotency_records
 FOR EACH ROW EXECUTE FUNCTION guard_immutable_columns();
CREATE TRIGGER outbox_payload_immutable BEFORE UPDATE ON outbox_events
 FOR EACH ROW EXECUTE FUNCTION guard_immutable_columns
 ('publish_status','attempt_count','next_attempt_at','lease_token','lease_until','published_at');
CREATE TRIGGER event_projection_immutable BEFORE UPDATE ON event_projection
 FOR EACH ROW EXECUTE FUNCTION guard_immutable_columns();
CREATE TRIGGER client_events_immutable BEFORE UPDATE ON client_events
 FOR EACH ROW EXECUTE FUNCTION guard_immutable_columns();
CREATE TRIGGER audit_log_immutable BEFORE UPDATE ON audit_log
 FOR EACH ROW EXECUTE FUNCTION guard_immutable_columns();
CREATE TRIGGER verification_runs_immutable BEFORE UPDATE ON verification_runs
 FOR EACH ROW EXECUTE FUNCTION guard_immutable_columns();

COMMENT ON SCHEMA vk_demo IS
 'Reference DDL only. SQL protects local invariants and referential/immutable structure. Application transaction services must additionally validate six published primary slots, max 500 SKU, exact snapshot/line equality, all-or-nothing reservations, deadline cap, active epoch, idempotency command hash, inventory ledger sums and atomic outbox.';

-- Required transaction-level equalities after expiry sweep (not SQL CHECKs):
-- inventory.reserved_packs = SUM(reservation_lines.pack_count WHERE reservation.status='ACTIVE');
-- inventory.returnable_packs = SUM(order_lines.pack_count WHERE order.status='TEST_CONFIRMED');
-- quote snapshot lines == quote_lines; reservation lines == its quote lines;
-- order lines == reservation/quote lines; sum(line_total_minor)==quote.subtotal_minor.
-- Do not grant direct physical DELETE or UPDATE of immutable event/history rows.
-- UUIDs, timestamps and hashes are supplied by the service; canonical hashes
-- must never be computed from jsonb::text (different serialization contract).
COMMIT;
