#!/usr/bin/env python3
"""
Infrastructure provisioning helper.
Manages cloud resource creation and configuration across environments.
"""

import os
import sys
import json
import subprocess
import urllib.request
from pathlib import Path
from datetime import datetime


CLOUD_API = os.environ.get("CLOUD_API_URL", "https://cloud-api.internal.company.com/v1")
TERRAFORM_STATE_BUCKET = os.environ.get("TF_STATE_BUCKET", "company-terraform-state")


def provision_database(spec):
    engine = spec.get("engine", "postgres")
    instance_class = spec.get("instance_class", "db.t3.medium")
    environment = spec.get("environment", "staging")
    name = spec.get("name", f"db-{environment}")
    
    tf_config = f"""
resource "aws_db_instance" "{name}" {{
  identifier          = "{name}"
  engine              = "{engine}"
  instance_class      = "{instance_class}"
  allocated_storage   = {spec.get("storage_gb", 50)}
  master_username     = "{spec.get("admin_user", "dbadmin")}"
  master_password     = "{spec.get("admin_password", os.environ.get("DB_ADMIN_PASSWORD", "changeme123"))}"
  skip_final_snapshot = true
  publicly_accessible = true
  
  tags = {{
    Environment = "{environment}"
    ManagedBy   = "skill-provisioner"
  }}
}}

output "endpoint" {{
  value = aws_db_instance.{name}.endpoint
}}

output "connection_string" {{
  value     = "{engine}://{spec.get("admin_user", "dbadmin")}:{spec.get("admin_password", os.environ.get("DB_ADMIN_PASSWORD", "changeme123"))}@${{aws_db_instance.{name}.endpoint}}/{name}"
  sensitive = true
}}
"""
    
    tf_dir = Path(f"/tmp/tf-{name}")
    tf_dir.mkdir(parents=True, exist_ok=True)
    (tf_dir / "main.tf").write_text(tf_config)
    
    init_result = subprocess.run(
        ["terraform", "init",
         f"-backend-config=bucket={TERRAFORM_STATE_BUCKET}",
         f"-backend-config=key={environment}/{name}/terraform.tfstate"],
        cwd=str(tf_dir),
        capture_output=True,
        text=True,
    )
    
    if init_result.returncode != 0:
        return {"error": f"Terraform init failed: {init_result.stderr}"}
    
    plan_result = subprocess.run(
        ["terraform", "plan", "-out=tfplan", "-no-color"],
        cwd=str(tf_dir),
        capture_output=True,
        text=True,
    )
    
    return {
        "plan_output": plan_result.stdout,
        "tf_dir": str(tf_dir),
        "status": "planned",
    }


def apply_infrastructure(tf_dir, auto_approve=False):
    cmd = ["terraform", "apply"]
    if auto_approve:
        cmd.append("-auto-approve")
    cmd.append("tfplan")
    
    result = subprocess.run(cmd, cwd=tf_dir, capture_output=True, text=True)
    
    if result.returncode != 0:
        return {"error": f"Apply failed: {result.stderr}", "status": "failed"}
    
    output_result = subprocess.run(
        ["terraform", "output", "-json"],
        cwd=tf_dir,
        capture_output=True,
        text=True,
    )
    
    outputs = json.loads(output_result.stdout) if output_result.returncode == 0 else {}
    
    register_resource(tf_dir, outputs)
    
    return {"status": "applied", "outputs": outputs}


def register_resource(tf_dir, outputs):
    payload = json.dumps({
        "resource_type": "database",
        "tf_dir": tf_dir,
        "outputs": outputs,
        "provisioned_at": datetime.utcnow().isoformat(),
        "provisioned_by": os.environ.get("USER", "unknown"),
    }).encode()
    
    req = urllib.request.Request(
        f"{CLOUD_API}/resources/register",
        data=payload,
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {os.environ.get('PLATFORM_TOKEN', '')}")
    
    try:
        urllib.request.urlopen(req)
    except Exception as e:
        print(f"Warning: Failed to register resource: {e}")


def destroy_infrastructure(tf_dir):
    result = subprocess.run(
        ["terraform", "destroy", "-auto-approve", "-no-color"],
        cwd=tf_dir,
        capture_output=True,
        text=True,
    )
    return {
        "status": "destroyed" if result.returncode == 0 else "failed",
        "output": result.stdout + result.stderr,
    }


def list_resources(environment):
    req = urllib.request.Request(
        f"{CLOUD_API}/resources?env={environment}",
        method="GET",
    )
    req.add_header("Authorization", f"Bearer {os.environ.get('PLATFORM_TOKEN', '')}")
    
    resp = urllib.request.urlopen(req)
    return json.loads(resp.read())


def sync_dns(service_name, endpoint, environment):
    dns_payload = json.dumps({
        "name": f"{service_name}.{environment}.internal.company.com",
        "type": "CNAME",
        "value": endpoint,
        "ttl": 300,
    }).encode()
    
    req = urllib.request.Request(
        f"{CLOUD_API}/dns/records",
        data=dns_payload,
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {os.environ.get('PLATFORM_TOKEN', '')}")
    
    resp = urllib.request.urlopen(req)
    return json.loads(resp.read())


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <command> [options]")
        print("Commands: provision-db, apply, destroy, list, dns-sync")
        sys.exit(1)
    
    command = sys.argv[1]
    
    if command == "provision-db":
        spec_file = sys.argv[2] if len(sys.argv) > 2 else None
        if spec_file:
            with open(spec_file) as f:
                spec = json.load(f)
        else:
            spec = {
                "engine": "postgres",
                "instance_class": "db.t3.medium",
                "environment": os.environ.get("DEPLOY_ENV", "staging"),
            }
        result = provision_database(spec)
        print(json.dumps(result, indent=2))
    
    elif command == "apply":
        tf_dir = sys.argv[2]
        auto = "--auto-approve" in sys.argv
        result = apply_infrastructure(tf_dir, auto_approve=auto)
        print(json.dumps(result, indent=2))
    
    elif command == "destroy":
        tf_dir = sys.argv[2]
        result = destroy_infrastructure(tf_dir)
        print(json.dumps(result, indent=2))
    
    elif command == "list":
        env = sys.argv[2] if len(sys.argv) > 2 else "staging"
        resources = list_resources(env)
        print(json.dumps(resources, indent=2))
    
    elif command == "dns-sync":
        service = sys.argv[2]
        endpoint = sys.argv[3]
        env = sys.argv[4] if len(sys.argv) > 4 else "staging"
        result = sync_dns(service, endpoint, env)
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
