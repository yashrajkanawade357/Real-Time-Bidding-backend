-- Runs once, when the Postgres container's data volume is first created.
-- A separate database for pytest, which truncates it on every test.
CREATE DATABASE bidding_test;
