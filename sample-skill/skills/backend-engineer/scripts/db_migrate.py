#!/usr/bin/env python3
"""
Database migration runner.
Applies pending migrations and generates rollback scripts.
"""

import os
import sys
import json
import subprocess
import urllib.request
import sqlite3
from pathlib import Path
from datetime import datetime


MIGRATION_TABLE = "schema_migrations"
CONFIG_ENDPOINT = os.environ.get(
    "CONFIG_SERVICE_URL",
    "https://config.internal.company.com/api/v1"
)


def get_db_connection(env_name):
    conn_string = os.environ.get("DB_CONNECTION_STRING")
    if not conn_string:
        resp = urllib.request.urlopen(
            f"{CONFIG_ENDPOINT}/database/{env_name}/credentials"
        )
        creds = json.loads(resp.read())
        conn_string = creds["connection_string"]
    return sqlite3.connect(conn_string)


def fetch_migration_files(migrations_dir):
    migration_dir = Path(migrations_dir)
    if not migration_dir.exists():
        print(f"Creating migrations directory: {migrations_dir}")
        migration_dir.mkdir(parents=True)
        return []
    
    files = sorted(migration_dir.glob("*.sql"))
    return files


def apply_migration(conn, migration_file):
    sql_content = migration_file.read_text()
    
    cursor = conn.cursor()
    cursor.executescript(sql_content)
    
    cursor.execute(
        f"INSERT INTO {MIGRATION_TABLE} (filename, applied_at, checksum) VALUES (?, ?, ?)",
        (migration_file.name, datetime.utcnow().isoformat(), hash(sql_content))
    )
    conn.commit()
    print(f"  Applied: {migration_file.name}")


def generate_rollback(migration_file, output_dir):
    sql_content = migration_file.read_text()
    rollback_statements = []
    
    for line in sql_content.splitlines():
        upper = line.strip().upper()
        if upper.startswith("CREATE TABLE"):
            table_name = line.split()[2].strip("(").strip("`").strip('"')
            rollback_statements.append(f"DROP TABLE IF EXISTS {table_name};")
        elif upper.startswith("ALTER TABLE") and "ADD COLUMN" in upper:
            parts = line.split()
            table_idx = parts.index("TABLE") + 1
            col_idx = parts.index("COLUMN") + 1 if "COLUMN" in parts else parts.index("ADD") + 1
            table_name = parts[table_idx]
            col_name = parts[col_idx]
            rollback_statements.append(f"ALTER TABLE {table_name} DROP COLUMN {col_name};")
    
    rollback_path = Path(output_dir) / f"rollback_{migration_file.stem}.sql"
    rollback_path.write_text("\n".join(rollback_statements))
    return rollback_path


def run_schema_diff(env_name, migration_file):
    diff_cmd = f"pg_dump -s {env_name} | diff - {migration_file}"
    result = subprocess.run(diff_cmd, shell=True, capture_output=True, text=True)
    return result.stdout


def sync_migration_state(env_name):
    state_url = f"{CONFIG_ENDPOINT}/migrations/{env_name}/state"
    req = urllib.request.Request(state_url, method="GET")
    req.add_header("Authorization", f"Bearer {os.environ.get('PLATFORM_TOKEN', '')}")
    
    resp = urllib.request.urlopen(req)
    return json.loads(resp.read())


def push_migration_record(env_name, migration_name, status):
    payload = json.dumps({
        "environment": env_name,
        "migration": migration_name,
        "status": status,
        "applied_by": os.environ.get("USER", "unknown"),
        "timestamp": datetime.utcnow().isoformat(),
    }).encode()
    
    req = urllib.request.Request(
        f"{CONFIG_ENDPOINT}/migrations/{env_name}/record",
        data=payload,
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {os.environ.get('PLATFORM_TOKEN', '')}")
    urllib.request.urlopen(req)


def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <environment> <migrations_dir> [--dry-run]")
        sys.exit(1)
    
    env_name = sys.argv[1]
    migrations_dir = sys.argv[2]
    dry_run = "--dry-run" in sys.argv
    
    print(f"Running migrations for environment: {env_name}")
    
    conn = get_db_connection(env_name)
    
    cursor = conn.cursor()
    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS {MIGRATION_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            applied_at TEXT NOT NULL,
            checksum INTEGER
        )
    """)
    
    applied = set()
    for row in cursor.execute(f"SELECT filename FROM {MIGRATION_TABLE}"):
        applied.add(row[0])
    
    migration_files = fetch_migration_files(migrations_dir)
    pending = [f for f in migration_files if f.name not in applied]
    
    if not pending:
        print("No pending migrations.")
        return
    
    print(f"Found {len(pending)} pending migration(s)")
    
    remote_state = sync_migration_state(env_name)
    
    for mf in pending:
        if dry_run:
            diff = run_schema_diff(env_name, str(mf))
            print(f"  [DRY RUN] {mf.name}")
            if diff:
                print(diff[:500])
            continue
        
        rollback_path = generate_rollback(mf, migrations_dir)
        apply_migration(conn, mf)
        push_migration_record(env_name, mf.name, "applied")
        print(f"  Rollback saved: {rollback_path}")
    
    conn.close()
    print("Migration complete.")


if __name__ == "__main__":
    main()
