"""Per-schema model modules. Each module is imported for WRITES only by that schema's writer
service (C-PG rule 7); import-linter contracts in .importlinter enforce it."""
