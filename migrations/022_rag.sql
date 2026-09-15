-- Implementation note: DESIGN.md section 3 originally proposed content_sha256.
-- M6 v1 ships one chunk per reference, so UNIQUE(ref_kind, ref_id) provides the
-- same dedupe guarantee without requiring a per-connection sha256() UDF in core
-- table triggers. Reintroduce a content hash only if multi-chunk refs land.
CREATE TABLE rag_chunks (
  id INTEGER PRIMARY KEY,
  ref_kind TEXT NOT NULL,
  ref_id INTEGER,
  posted_on TEXT,
  category_id INTEGER,
  amount_cents INTEGER,
  content TEXT NOT NULL,
  UNIQUE (ref_kind, ref_id)
);

CREATE VIRTUAL TABLE rag_fts USING fts5(content, content='rag_chunks', content_rowid='id');

DROP VIEW IF EXISTS v_rag_transaction_chunks;
CREATE VIEW v_rag_transaction_chunks AS
WITH split_rollup AS (
  SELECT
    s.transaction_id,
    MIN(s.category_id) AS category_id,
    GROUP_CONCAT(DISTINCT c.name) AS category_names,
    GROUP_CONCAT(NULLIF(TRIM(s.memo), ''), ' · ') AS split_memos
  FROM transaction_splits s
  JOIN categories c ON c.id = s.category_id
  GROUP BY s.transaction_id
),
receipt_items AS (
  SELECT
    ie.transaction_id,
    GROUP_CONCAT(
      NULLIF(
        TRIM(
          COALESCE(json_extract(item.value, '$.description'), '')
          || CASE
               WHEN COALESCE(json_extract(item.value, '$.amount_cents'), 0) = 0
               THEN ''
               ELSE ' $' || printf('%.2f', ABS(CAST(json_extract(item.value, '$.amount_cents') AS INTEGER)) / 100.0)
             END
        ),
        ''
      ),
      ', '
    ) AS line_items
  FROM ingest_extractions ie
  JOIN json_each(json_extract(ie.extracted_json, '$.line_items')) AS item
  WHERE ie.transaction_id IS NOT NULL
  GROUP BY ie.transaction_id
),
base AS (
  SELECT
    t.id AS ref_id,
    t.posted_on,
    sr.category_id,
    t.amount_cents,
    TRIM(
      t.posted_on
      || ' · ' || CASE strftime('%m', t.posted_on)
           WHEN '01' THEN 'January'
           WHEN '02' THEN 'February'
           WHEN '03' THEN 'March'
           WHEN '04' THEN 'April'
           WHEN '05' THEN 'May'
           WHEN '06' THEN 'June'
           WHEN '07' THEN 'July'
           WHEN '08' THEN 'August'
           WHEN '09' THEN 'September'
           WHEN '10' THEN 'October'
           WHEN '11' THEN 'November'
           WHEN '12' THEN 'December'
           ELSE ''
         END
      || ' ' || strftime('%Y', t.posted_on)
      || ' · ' || TRIM(COALESCE(NULLIF(t.counterparty, ''), NULLIF(t.description, ''), 'transaction'))
      || ' · ' || COALESCE(NULLIF(sr.category_names, ''), 'Uncategorized')
      || ' · $' || printf('%.2f', ABS(t.amount_cents) / 100.0)
      || CASE
           WHEN NULLIF(TRIM(t.description), '') IS NOT NULL
            AND TRIM(t.description) != TRIM(COALESCE(NULLIF(t.counterparty, ''), ''))
           THEN ' · ' || TRIM(t.description)
           ELSE ''
         END
      || CASE WHEN NULLIF(TRIM(t.notes), '') IS NOT NULL THEN ' · ' || TRIM(t.notes) ELSE '' END
      || CASE WHEN NULLIF(TRIM(sr.split_memos), '') IS NOT NULL THEN ' · ' || TRIM(sr.split_memos) ELSE '' END
      || CASE WHEN NULLIF(TRIM(ri.line_items), '') IS NOT NULL THEN ' · items: ' || TRIM(ri.line_items) ELSE '' END
    ) AS content
  FROM transactions t
  LEFT JOIN split_rollup sr ON sr.transaction_id = t.id
  LEFT JOIN receipt_items ri ON ri.transaction_id = t.id
)
SELECT
  'transaction' AS ref_kind,
  ref_id,
  posted_on,
  category_id,
  amount_cents,
  content
FROM base;

CREATE TRIGGER rag_chunks_ai AFTER INSERT ON rag_chunks BEGIN
  INSERT INTO rag_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER rag_chunks_ad AFTER DELETE ON rag_chunks BEGIN
  INSERT INTO rag_fts(rag_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;

CREATE TRIGGER rag_chunks_au AFTER UPDATE ON rag_chunks BEGIN
  INSERT INTO rag_fts(rag_fts, rowid, content) VALUES ('delete', old.id, old.content);
  INSERT INTO rag_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER rag_transactions_ai AFTER INSERT ON transactions BEGIN
  DELETE FROM rag_chunks WHERE ref_kind = 'transaction' AND ref_id = new.id;
  INSERT INTO rag_chunks(ref_kind, ref_id, posted_on, category_id, amount_cents, content)
  SELECT ref_kind, ref_id, posted_on, category_id, amount_cents, content
  FROM v_rag_transaction_chunks
  WHERE ref_id = new.id;
END;

CREATE TRIGGER rag_transactions_au AFTER UPDATE OF account_id, posted_on, description, counterparty, amount_cents, source_document_id, notes ON transactions BEGIN
  DELETE FROM rag_chunks WHERE ref_kind = 'transaction' AND ref_id = new.id;
  INSERT INTO rag_chunks(ref_kind, ref_id, posted_on, category_id, amount_cents, content)
  SELECT ref_kind, ref_id, posted_on, category_id, amount_cents, content
  FROM v_rag_transaction_chunks
  WHERE ref_id = new.id;
END;

CREATE TRIGGER rag_transactions_ad AFTER DELETE ON transactions BEGIN
  DELETE FROM rag_chunks WHERE ref_kind = 'transaction' AND ref_id = old.id;
END;

CREATE TRIGGER rag_splits_ai AFTER INSERT ON transaction_splits BEGIN
  DELETE FROM rag_chunks WHERE ref_kind = 'transaction' AND ref_id = new.transaction_id;
  INSERT INTO rag_chunks(ref_kind, ref_id, posted_on, category_id, amount_cents, content)
  SELECT ref_kind, ref_id, posted_on, category_id, amount_cents, content
  FROM v_rag_transaction_chunks
  WHERE ref_id = new.transaction_id;
END;

CREATE TRIGGER rag_splits_au AFTER UPDATE OF category_id, amount_cents, memo ON transaction_splits BEGIN
  DELETE FROM rag_chunks WHERE ref_kind = 'transaction' AND ref_id = new.transaction_id;
  INSERT INTO rag_chunks(ref_kind, ref_id, posted_on, category_id, amount_cents, content)
  SELECT ref_kind, ref_id, posted_on, category_id, amount_cents, content
  FROM v_rag_transaction_chunks
  WHERE ref_id = new.transaction_id;
END;

CREATE TRIGGER rag_splits_ad AFTER DELETE ON transaction_splits BEGIN
  DELETE FROM rag_chunks WHERE ref_kind = 'transaction' AND ref_id = old.transaction_id;
  INSERT INTO rag_chunks(ref_kind, ref_id, posted_on, category_id, amount_cents, content)
  SELECT ref_kind, ref_id, posted_on, category_id, amount_cents, content
  FROM v_rag_transaction_chunks
  WHERE ref_id = old.transaction_id;
END;

CREATE TRIGGER rag_categories_name_au AFTER UPDATE OF name ON categories BEGIN
  DELETE FROM rag_chunks
  WHERE ref_kind = 'transaction'
    AND ref_id IN (
      SELECT transaction_id FROM transaction_splits WHERE category_id = new.id
    );
  INSERT INTO rag_chunks(ref_kind, ref_id, posted_on, category_id, amount_cents, content)
  SELECT ref_kind, ref_id, posted_on, category_id, amount_cents, content
  FROM v_rag_transaction_chunks
  WHERE ref_id IN (
    SELECT transaction_id FROM transaction_splits WHERE category_id = new.id
  );
END;

CREATE TRIGGER rag_ingest_extractions_ai AFTER INSERT ON ingest_extractions WHEN new.transaction_id IS NOT NULL BEGIN
  DELETE FROM rag_chunks WHERE ref_kind = 'transaction' AND ref_id = new.transaction_id;
  INSERT INTO rag_chunks(ref_kind, ref_id, posted_on, category_id, amount_cents, content)
  SELECT ref_kind, ref_id, posted_on, category_id, amount_cents, content
  FROM v_rag_transaction_chunks
  WHERE ref_id = new.transaction_id;
END;

CREATE TRIGGER rag_ingest_extractions_au AFTER UPDATE OF extracted_json, transaction_id ON ingest_extractions BEGIN
  DELETE FROM rag_chunks
  WHERE ref_kind = 'transaction'
    AND ref_id IN (old.transaction_id, new.transaction_id);
  INSERT INTO rag_chunks(ref_kind, ref_id, posted_on, category_id, amount_cents, content)
  SELECT ref_kind, ref_id, posted_on, category_id, amount_cents, content
  FROM v_rag_transaction_chunks
  WHERE ref_id IN (old.transaction_id, new.transaction_id);
END;

CREATE TRIGGER rag_ingest_extractions_ad AFTER DELETE ON ingest_extractions WHEN old.transaction_id IS NOT NULL BEGIN
  DELETE FROM rag_chunks WHERE ref_kind = 'transaction' AND ref_id = old.transaction_id;
  INSERT INTO rag_chunks(ref_kind, ref_id, posted_on, category_id, amount_cents, content)
  SELECT ref_kind, ref_id, posted_on, category_id, amount_cents, content
  FROM v_rag_transaction_chunks
  WHERE ref_id = old.transaction_id;
END;

INSERT INTO rag_chunks(ref_kind, ref_id, posted_on, category_id, amount_cents, content)
SELECT ref_kind, ref_id, posted_on, category_id, amount_cents, content
FROM v_rag_transaction_chunks;
