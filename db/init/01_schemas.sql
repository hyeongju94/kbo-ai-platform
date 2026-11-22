-- Runs once, the first time the Postgres volume is created.
CREATE SCHEMA IF NOT EXISTS raw;       -- data as collected
CREATE SCHEMA IF NOT EXISTS analytics; -- cleaned tables and features for training
