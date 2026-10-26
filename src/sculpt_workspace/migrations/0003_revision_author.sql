-- Who produced each revision: 'upload' (revision 1), 'agent' (MCP) or 'user' (browser editor).
ALTER TABLE revisions ADD COLUMN author TEXT NOT NULL DEFAULT 'agent';
