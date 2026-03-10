-- Run once to create the database
-- psql -U postgres -f scripts/init_db.sql

CREATE DATABASE ai_test_platform;

\c ai_test_platform;

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Tables are created automatically by SQLAlchemy on startup (init_db()),
-- but this script is here for manual inspection / migration reference.

-- Optional: seed a quick test
-- INSERT INTO test_cases (test_script_num, module, test_case_name, description, expected_results, parsed_json)
-- VALUES ('RB001', 'RB_Pets_Landing_Page', 'Verify Pet landing page tabs', 'Verify 3 tabs', 'User sees 3 tabs', '{}');
