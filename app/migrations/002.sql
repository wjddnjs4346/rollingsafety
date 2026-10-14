ALTER TABLE users ADD COLUMN display_name TEXT NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE assignments ADD COLUMN version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE findings ADD COLUMN version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE findings ADD COLUMN resolved_year INTEGER;
ALTER TABLE findings ADD COLUMN resolved_quarter INTEGER;
ALTER TABLE finding_events ADD COLUMN year INTEGER;
ALTER TABLE finding_events ADD COLUMN quarter INTEGER;
CREATE TABLE requests(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), route TEXT NOT NULL, body_hash TEXT NOT NULL, response_json TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX findings_equipment ON findings(equipment_id);
CREATE INDEX audit_entity ON audit_logs(entity_type,entity_id);
CREATE UNIQUE INDEX finding_source_item ON findings(source_inspection_id,item_code);

CREATE TABLE assignments_new(id INTEGER PRIMARY KEY, equipment_id INTEGER NOT NULL REFERENCES equipment(id), year INTEGER NOT NULL, quarter INTEGER NOT NULL CHECK(quarter BETWEEN 1 AND 4), assignee_id INTEGER REFERENCES users(id), version INTEGER NOT NULL DEFAULT 1, UNIQUE(equipment_id,year,quarter));
INSERT INTO assignments_new SELECT id,equipment_id,year,quarter,assignee_id,version FROM assignments;
DROP TABLE assignments;
ALTER TABLE assignments_new RENAME TO assignments;
CREATE UNIQUE INDEX one_open_finding_per_item ON findings(equipment_id,item_code) WHERE status='open';
