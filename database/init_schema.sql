-- ============================================
-- PARKING MANAGEMENT SYSTEM - INITIALIZATION SCRIPT
-- Runs the full schema setup script.
-- ============================================

SET ECHO ON
SET FEEDBACK ON
SET SERVEROUTPUT ON
SET DEFINE OFF

PROMPT Starting database initialization...

@@schema.sql

PROMPT Database initialization completed.
