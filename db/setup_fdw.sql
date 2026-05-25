-- =====================================================================
-- FDW BOOTSTRAP — connects clema_landing DB to the existing ipeds DB
-- so that landing.* materialized views / views can still JOIN to
-- ipeds.* and score_card.* without copying any data.
--
-- Run this AS THE OWNER (rajailayaperumal) of the clema_landing DB,
-- AFTER `CREATE DATABASE clema_landing` and BEFORE applying the landing
-- schema in schema.sql.
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS postgres_fdw;
-- PostGIS needed because ipeds.institutions_2024 has a `public.geometry` column
-- that we have to declare even if we never query it.
CREATE EXTENSION IF NOT EXISTS postgis;

-- One foreign server pointing at the existing ipeds DB on the same host.
-- Adjust host/port if your local Postgres uses different values.
DROP SERVER IF EXISTS ipeds_db CASCADE;
CREATE SERVER ipeds_db
  FOREIGN DATA WRAPPER postgres_fdw
  OPTIONS (host 'localhost', port '5432', dbname 'ipeds',
           fetch_size '10000', use_remote_estimate 'true');

-- User mapping: connect to ipeds DB as the current user (no password needed
-- on a default macOS/Postgres local install — adjust if your setup uses auth).
DROP USER MAPPING IF EXISTS FOR CURRENT_USER SERVER ipeds_db;
CREATE USER MAPPING FOR CURRENT_USER SERVER ipeds_db;

-- Mirror the ipeds + score_card schemas locally as foreign tables.
-- We only import the tables actually referenced by landing.* — keeps the
-- foreign catalog small and the EXPLAIN plans clean.
CREATE SCHEMA IF NOT EXISTS ipeds;
CREATE SCHEMA IF NOT EXISTS score_card;

-- ipeds tables we need: directory, lookups, time-series we join on
IMPORT FOREIGN SCHEMA ipeds
  LIMIT TO (
    institutions_2024,
    carnegie_codes,
    control_codes,
    sector_codes,
    state_codes,
    enrollment_12month_2024,
    fall_enrollment_2024,
    fall_enrollment_retention,
    fall_enrollment_distance_2024,
    fall_enrollment_age_2024,
    fall_enrollment_residence_2024,
    fall_enrollment_major_2024,
    completions_by_program_2024,
    cost1,
    cost_tuition_fees,
    financial_aid,
    graduation_rates_2024,
    graduation_rates_200pct,
    graduation_rates_pell_ssl,
    outcome_measures_2024,
    admissions,
    staff_instructional_summary,
    staff_instructional_detail_2024,
    salaries_instructional,
    salaries_noninstructional,
    drv_admissions,
    drv_completions,
    drv_cost,
    drv_fall_enrollment,
    drv_graduation_rates,
    drv_outcome_measures,
    drv_human_resources,
    drv_finance
  )
  FROM SERVER ipeds_db INTO ipeds;

-- score_card tables we need: yearly fact, dimensions
IMPORT FOREIGN SCHEMA score_card
  LIMIT TO (
    fact_institution_yearly,
    dim_carnegie_classifications,
    dim_institutions,
    dim_states,
    dim_academic_years
  )
  FROM SERVER ipeds_db INTO score_card;

-- Smoke test
DO $$
DECLARE
  n_inst INT;
BEGIN
  SELECT COUNT(*) INTO n_inst FROM ipeds.institutions_2024;
  RAISE NOTICE 'FDW OK: ipeds.institutions_2024 has % rows via foreign server', n_inst;
END$$;
