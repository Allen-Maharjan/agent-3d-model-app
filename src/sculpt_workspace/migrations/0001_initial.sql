CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    original_size INTEGER NOT NULL,
    original_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('importing', 'ready', 'import_failed')),
    current_revision_id TEXT REFERENCES revisions(id),
    view_frame TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE revisions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    parent_id TEXT REFERENCES revisions(id),
    summary TEXT NOT NULL,
    created_at TEXT NOT NULL,
    blender_version TEXT NOT NULL,
    protection_report TEXT NOT NULL,
    job_id TEXT,
    UNIQUE (project_id, number)
);

CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('import', 'edit', 'render', 'inspect', 'export')),
    state TEXT NOT NULL CHECK (state IN ('blocked', 'queued', 'running', 'succeeded', 'failed', 'cancelled')),
    params TEXT NOT NULL,
    result TEXT,
    error TEXT,
    base_revision_id TEXT REFERENCES revisions(id),
    revision_id TEXT REFERENCES revisions(id),
    parent_job_id TEXT REFERENCES jobs(id),
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE INDEX jobs_project_state ON jobs (project_id, state);
CREATE INDEX jobs_revision ON jobs (revision_id, kind, state);

CREATE TABLE artifacts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    revision_id TEXT REFERENCES revisions(id),
    job_id TEXT REFERENCES jobs(id),
    kind TEXT NOT NULL CHECK (kind IN ('render_image', 'export_stl', 'export_blend')),
    view TEXT,
    rel_path TEXT NOT NULL,
    mime TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    metadata TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX artifacts_job ON artifacts (job_id);
CREATE INDEX artifacts_revision ON artifacts (revision_id, kind);
