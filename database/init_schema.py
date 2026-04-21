
import os
from pathlib import Path

try:
    import oracledb as cx_Oracle
except ImportError:
    import cx_Oracle

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

DB_CONFIG = {
    "user": os.getenv("ORACLE_USER", "SYSTEM"),
    "password": os.getenv("ORACLE_PWD", "oracle_password123"),
    "dsn": os.getenv("ORACLE_DSN", "localhost:1521/FREEPDB1"),
}


def _iter_statements(sql_text: str):
    """Yield executable SQL/PLSQL statements from a SQL*Plus-style file."""
    buffer = []
    plsql_mode = False

    for raw_line in sql_text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if not stripped or stripped.startswith("--"):
            continue

        if stripped.upper().startswith(("CREATE OR REPLACE TRIGGER", "CREATE OR REPLACE FUNCTION", "CREATE OR REPLACE PROCEDURE", "BEGIN", "DECLARE")):
            plsql_mode = True

        if stripped == "/":
            if buffer:
                yield "\n".join(buffer).strip()
                buffer = []
            plsql_mode = False
            continue

        buffer.append(line)

        if not plsql_mode and stripped.endswith(";"):
            statement = "\n".join(buffer).strip()
            if statement.endswith(";"):
                statement = statement[:-1].rstrip()
            if statement:
                yield statement
            buffer = []

    if buffer:
        statement = "\n".join(buffer).strip()
        if statement.endswith(";"):
            statement = statement[:-1].rstrip()
        if statement:
            yield statement


def main():
    if not SCHEMA_PATH.exists():
        raise FileNotFoundError(f"Schema file not found: {SCHEMA_PATH}")

    sql_text = SCHEMA_PATH.read_text(encoding="utf-8")
    statements = list(_iter_statements(sql_text))

    conn = cx_Oracle.connect(**DB_CONFIG)
    try:
        cursor = conn.cursor()
        for stmt in statements:
            cursor.execute(stmt)
        conn.commit()
        cursor.close()
        print(f"Schema initialized successfully. Executed {len(statements)} statements.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
