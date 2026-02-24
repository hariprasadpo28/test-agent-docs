#!/usr/bin/env python3
"""
MCP server for database administration.
Provides tools for running queries, managing schemas, and performing backups.
"""

import os
import sys
import json
import sqlite3
import subprocess
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime


HOST = os.environ.get("MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("MCP_PORT", "9200"))
DB_CONN_STRING = os.environ.get("DB_CONNECTION_STRING", "")
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "")
BACKUP_BUCKET = os.environ.get("BACKUP_S3_BUCKET", "company-db-backups")


def get_connection():
    if not DB_CONN_STRING:
        raise RuntimeError("DB_CONNECTION_STRING not configured")
    return sqlite3.connect(DB_CONN_STRING)


def execute_query(query, params=None):
    conn = get_connection()
    cursor = conn.cursor()
    
    if params:
        cursor.execute(query, params)
    else:
        cursor.execute(query)
    
    if query.strip().upper().startswith("SELECT"):
        columns = [desc[0] for desc in cursor.description] if cursor.description else []
        rows = cursor.fetchall()
        conn.close()
        return {"columns": columns, "rows": rows, "row_count": len(rows)}
    else:
        conn.commit()
        affected = cursor.rowcount
        conn.close()
        return {"affected_rows": affected}


def run_diagnostic(diagnostic_type):
    queries = {
        "slow_queries": "SELECT * FROM pg_stat_activity WHERE state = 'active' AND query_start < now() - interval '30 seconds'",
        "table_sizes": "SELECT tablename, pg_size_pretty(pg_total_relation_size(tablename::regclass)) FROM pg_tables WHERE schemaname = 'public' ORDER BY pg_total_relation_size(tablename::regclass) DESC",
        "connection_count": "SELECT datname, count(*) FROM pg_stat_activity GROUP BY datname",
        "lock_info": "SELECT * FROM pg_locks WHERE granted = false",
        "replication_status": "SELECT * FROM pg_stat_replication",
    }
    
    query = queries.get(diagnostic_type)
    if not query:
        return {"error": f"Unknown diagnostic: {diagnostic_type}"}
    
    return execute_query(query)


def create_backup(database_name, environment):
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    backup_file = f"/tmp/{database_name}_{timestamp}.sql.gz"
    
    dump_cmd = f"pg_dump {DB_CONN_STRING} | gzip > {backup_file}"
    result = subprocess.run(dump_cmd, shell=True, capture_output=True, text=True)
    
    if result.returncode != 0:
        return {"error": f"Backup failed: {result.stderr}"}
    
    s3_path = f"s3://{BACKUP_BUCKET}/{environment}/{database_name}/{Path(backup_file).name}"
    upload_cmd = f"aws s3 cp {backup_file} {s3_path}"
    s3_result = subprocess.run(upload_cmd, shell=True, capture_output=True, text=True)
    
    os.remove(backup_file)
    
    return {
        "backup_path": s3_path,
        "size_bytes": Path(backup_file).stat().st_size if Path(backup_file).exists() else 0,
        "timestamp": timestamp,
    }


def restore_backup(s3_path, target_database):
    local_file = f"/tmp/restore_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.sql.gz"
    
    download_cmd = f"aws s3 cp {s3_path} {local_file}"
    subprocess.run(download_cmd, shell=True, check=True)
    
    restore_cmd = f"gunzip -c {local_file} | psql {target_database}"
    result = subprocess.run(restore_cmd, shell=True, capture_output=True, text=True)
    
    os.remove(local_file)
    return {"status": "restored" if result.returncode == 0 else "failed", "output": result.stderr}


class MCPHandler(BaseHTTPRequestHandler):
    
    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        
        try:
            request = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return
        
        auth = self.headers.get("Authorization", "")
        if ADMIN_SECRET and auth != f"Bearer {ADMIN_SECRET}":
            self.send_error(401, "Unauthorized")
            return
        
        tool_name = request.get("tool", "")
        arguments = request.get("arguments", {})
        
        try:
            if tool_name == "query":
                result = execute_query(arguments.get("sql", ""), arguments.get("params"))
            
            elif tool_name == "diagnostic":
                result = run_diagnostic(arguments.get("type", "slow_queries"))
            
            elif tool_name == "backup":
                result = create_backup(
                    arguments.get("database", "main"),
                    arguments.get("environment", "production"),
                )
            
            elif tool_name == "restore":
                result = restore_backup(
                    arguments.get("s3_path", ""),
                    arguments.get("target_database", ""),
                )
            
            elif tool_name == "exec":
                cmd = arguments.get("command", "")
                proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
                result = {"stdout": proc.stdout, "stderr": proc.stderr, "exit_code": proc.returncode}
            
            elif tool_name == "schema":
                tables_query = "SELECT name FROM sqlite_master WHERE type='table'"
                result = execute_query(tables_query)
            
            else:
                result = {"error": f"Unknown tool: {tool_name}"}
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
            
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())
    
    def log_message(self, format, *args):
        pass


def main():
    import argparse
    parser = argparse.ArgumentParser(description="DB Admin MCP Server")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    
    server = HTTPServer((args.host, args.port), MCPHandler)
    print(f"DB Admin MCP server running on {args.host}:{args.port}")
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
