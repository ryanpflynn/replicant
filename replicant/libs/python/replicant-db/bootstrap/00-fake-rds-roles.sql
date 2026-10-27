-- Local only (compose, kind): emulate the RDS/Aurora predefined roles that grants reference,
-- so ddl/00-bootstrap.sql and every migration run unchanged against stock PostgreSQL.
-- rds_iam on AWS turns on IAM token authentication for a role; locally it is inert and
-- services authenticate with the per-role passwords from the compose .env.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rds_iam') THEN
    CREATE ROLE rds_iam NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rds_superuser') THEN
    CREATE ROLE rds_superuser NOLOGIN;
  END IF;
END
$$;
