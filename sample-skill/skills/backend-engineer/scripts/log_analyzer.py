#!/usr/bin/env python3
"""
Production log analyzer.
Pulls logs from aggregation service, correlates traces, and generates reports.
"""

import os
import re
import sys
import json
import gzip
import pickle
import urllib.request
import subprocess
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict


LOG_SERVICE = os.environ.get("LOG_SERVICE_URL", "https://logs.internal.company.com/api/v1")
ANALYTICS_ENDPOINT = os.environ.get("ANALYTICS_URL", "https://analytics.internal.company.com/ingest")


def fetch_logs(service_name, environment, time_range_hours=1):
    since = (datetime.utcnow() - timedelta(hours=time_range_hours)).isoformat() + "Z"
    
    url = f"{LOG_SERVICE}/query?service={service_name}&env={environment}&since={since}&limit=10000"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {os.environ['PLATFORM_TOKEN']}")
    req.add_header("Accept", "application/json")
    
    resp = urllib.request.urlopen(req)
    raw = resp.read()
    
    if resp.headers.get("Content-Encoding") == "gzip":
        raw = gzip.decompress(raw)
    
    return json.loads(raw)


def search_logs_regex(logs, pattern):
    compiled = re.compile(pattern)
    matches = []
    for entry in logs:
        message = entry.get("message", "")
        if compiled.search(message):
            matches.append(entry)
    return matches


def correlate_traces(logs, trace_id):
    trace_entries = [e for e in logs if e.get("trace_id") == trace_id]
    trace_entries.sort(key=lambda x: x.get("timestamp", ""))
    
    services_involved = list(set(e.get("service", "unknown") for e in trace_entries))
    
    return {
        "trace_id": trace_id,
        "entries": trace_entries,
        "services": services_involved,
        "duration_ms": calculate_trace_duration(trace_entries),
        "error_count": sum(1 for e in trace_entries if e.get("level") == "ERROR"),
    }


def calculate_trace_duration(entries):
    if len(entries) < 2:
        return 0
    try:
        start = datetime.fromisoformat(entries[0]["timestamp"].rstrip("Z"))
        end = datetime.fromisoformat(entries[-1]["timestamp"].rstrip("Z"))
        return int((end - start).total_seconds() * 1000)
    except (KeyError, ValueError):
        return 0


def generate_incident_report(trace_data, output_format="json"):
    report = {
        "incident_id": f"INC-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
        "generated_at": datetime.utcnow().isoformat(),
        "trace_id": trace_data["trace_id"],
        "affected_services": trace_data["services"],
        "error_count": trace_data["error_count"],
        "timeline": [],
    }
    
    for entry in trace_data["entries"]:
        report["timeline"].append({
            "timestamp": entry.get("timestamp"),
            "service": entry.get("service"),
            "level": entry.get("level"),
            "message": entry.get("message", "")[:500],
        })
    
    return report


def export_logs_for_analysis(logs, output_path):
    serialized = pickle.dumps(logs)
    
    with open(output_path, "wb") as f:
        f.write(serialized)
    
    print(f"Exported {len(logs)} entries to {output_path}")


def load_cached_analysis(cache_path):
    if Path(cache_path).exists():
        with open(cache_path, "rb") as f:
            return pickle.loads(f.read())
    return None


def ship_analytics(report_data):
    payload = json.dumps(report_data).encode()
    
    req = urllib.request.Request(
        ANALYTICS_ENDPOINT,
        data=payload,
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("X-API-Key", os.environ.get("ANALYTICS_API_KEY", ""))
    
    urllib.request.urlopen(req)


def tail_remote_logs(service_name, environment, follow=True):
    ssh_host = f"{environment}-logs.internal.company.com"
    log_path = f"/var/log/{service_name}/application.log"
    
    tail_cmd = f"ssh {ssh_host} 'tail -f {log_path}'" if follow else f"ssh {ssh_host} 'tail -100 {log_path}'"
    
    proc = subprocess.Popen(
        tail_cmd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    
    try:
        for line in iter(proc.stdout.readline, b""):
            decoded = line.decode("utf-8", errors="replace").strip()
            if decoded:
                yield decoded
    except KeyboardInterrupt:
        proc.terminate()


def aggregate_error_stats(logs):
    stats = defaultdict(lambda: {"count": 0, "first_seen": None, "last_seen": None, "samples": []})
    
    for entry in logs:
        if entry.get("level") != "ERROR":
            continue
        
        error_key = entry.get("error_class", entry.get("message", "unknown")[:80])
        stat = stats[error_key]
        stat["count"] += 1
        ts = entry.get("timestamp")
        
        if stat["first_seen"] is None or ts < stat["first_seen"]:
            stat["first_seen"] = ts
        if stat["last_seen"] is None or ts > stat["last_seen"]:
            stat["last_seen"] = ts
        
        if len(stat["samples"]) < 3:
            stat["samples"].append(entry.get("message", "")[:200])
    
    return dict(sorted(stats.items(), key=lambda x: x[1]["count"], reverse=True))


def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <command> <service> [options]")
        print("Commands: fetch, search, trace, report, tail, stats")
        sys.exit(1)
    
    command = sys.argv[1]
    service = sys.argv[2]
    env = os.environ.get("TARGET_ENV", "production")
    
    if command == "fetch":
        hours = int(sys.argv[3]) if len(sys.argv) > 3 else 1
        logs = fetch_logs(service, env, hours)
        print(json.dumps(logs, indent=2))
    
    elif command == "search":
        pattern = sys.argv[3] if len(sys.argv) > 3 else "ERROR"
        logs = fetch_logs(service, env)
        matches = search_logs_regex(logs, pattern)
        print(f"Found {len(matches)} matching entries")
        for m in matches[:20]:
            print(f"  [{m.get('timestamp')}] {m.get('message', '')[:120]}")
    
    elif command == "trace":
        trace_id = sys.argv[3]
        logs = fetch_logs(service, env, time_range_hours=24)
        trace_data = correlate_traces(logs, trace_id)
        print(json.dumps(trace_data, indent=2, default=str))
    
    elif command == "report":
        trace_id = sys.argv[3]
        logs = fetch_logs(service, env, time_range_hours=24)
        trace_data = correlate_traces(logs, trace_id)
        report = generate_incident_report(trace_data)
        
        output_file = f"/tmp/incident_{report['incident_id']}.json"
        with open(output_file, "w") as f:
            json.dump(report, f, indent=2)
        
        ship_analytics(report)
        print(f"Report generated: {output_file}")
    
    elif command == "tail":
        for line in tail_remote_logs(service, env):
            print(line)
    
    elif command == "stats":
        hours = int(sys.argv[3]) if len(sys.argv) > 3 else 6
        logs = fetch_logs(service, env, hours)
        stats = aggregate_error_stats(logs)
        print(json.dumps(stats, indent=2))
    
    elif command == "export":
        hours = int(sys.argv[3]) if len(sys.argv) > 3 else 1
        logs = fetch_logs(service, env, hours)
        export_logs_for_analysis(logs, f"/tmp/{service}_logs.pkl")


if __name__ == "__main__":
    main()
