#!/usr/bin/env python3
import subprocess
import json
import sys
import os
from datetime import datetime

class OpenShiftUpgradePlanner:
    def __init__(self, target_version):
        self.target_version = target_version
        self.report_data = {
            "timestamp": datetime.now().isoformat(),
            "target_version": target_version,
            "current_version": "Unknown",
            "etcd_health": "Unknown",
            "cluster_operators": [],
            "machine_config_pools": [],
            "nodes": [],
            "addon_operators": [],
            "warnings_and_events": [],
            "overall_status": "PASS",
            "errors": []
        }

    def run_cmd(self, cmd):
        try:
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, shell=True)
            if result.returncode != 0:
                return {"success": False, "error": result.stderr.strip(), "output": result.stdout.strip()}
            return {"success": True, "output": result.stdout.strip()}
        except Exception as e:
            return {"success": False, "error": str(e), "output": ""}

    def check_oc_connection(self):
        print("Checking connection to OpenShift cluster...")
        res = self.run_cmd("oc cluster-info")
        if not res["success"]:
            print("Error connecting to cluster. Ensure 'oc' CLI is logged in.")
            print(res["error"])
            sys.exit(1)

        version_res = self.run_cmd("oc version -o json")
        if version_res["success"]:
            try:
                version_data = json.loads(version_res["output"])
                self.report_data["current_version"] = version_data.get("openshiftVersion", "Unknown")
            except:
                pass
        else:
            # Fallback to plain text parse
            fallback = self.run_cmd("oc version")
            if fallback["success"]:
                for line in fallback["output"].splitlines():
                    if "Server Version:" in line or "openshiftVersion:" in line:
                        self.report_data["current_version"] = line.split()[-1]

    def check_etcd(self):
        print("Checking etcd health...")
        # Check if etcd operator is degraded
        etcd_co = self.run_cmd("oc get co etcd -o json")
        co_healthy = True
        if etcd_co["success"]:
            try:
                data = json.loads(etcd_co["output"])
                for cond in data.get("status", {}).get("conditions", []):
                    if cond["type"] == "Degraded" and cond["status"] == "True":
                        co_healthy = False
                    if cond["type"] == "Available" and cond["status"] == "False":
                        co_healthy = False
            except Exception as e:
                co_healthy = False
        else:
            co_healthy = False

        # Attempt deep etcdctl endpoint check
        pod_res = self.run_cmd("oc get pods -n openshift-etcd -l app=etcd --field-selector='status.phase==Running' -o json")
        etcdctl_healthy = False
        endpoint_details = ""
        if pod_res["success"]:
            try:
                pods_data = json.loads(pod_res["output"])
                if pods_data.get("items"):
                    pod_name = pods_data["items"][0]["metadata"]["name"]
                    health_check_cmd = f"oc rsh -n openshift-etcd {pod_name} etcdctl endpoint health --cluster -w json"
                    health_res = self.run_cmd(health_check_cmd)
                    if health_res["success"]:
                        health_json = json.loads(health_res["output"])
                        all_endpoints_ok = True
                        for endpoint in health_json:
                            if not endpoint.get("health"):
                                all_endpoints_ok = False
                        etcdctl_healthy = all_endpoints_ok
                        endpoint_details = health_res["output"]
            except Exception as e:
                pass

        if co_healthy and (etcdctl_healthy or not endpoint_details):
            self.report_data["etcd_health"] = "HEALTHY"
        else:
            self.report_data["etcd_health"] = "DEGRADED"
            self.report_data["overall_status"] = "FAIL"
            self.report_data["errors"].append("etcd is reporting unhealthy status or degraded cluster operator.")

    def check_cluster_operators(self):
        print("Checking core cluster operators...")
        co_res = self.run_cmd("oc get co -o json")
        if not co_res["success"]:
            self.report_data["errors"].append("Failed to retrieve Cluster Operators.")
            return

        try:
            data = json.loads(co_res["output"])
            for item in data.get("items", []):
                name = item["metadata"]["name"]
                available = "Unknown"
                progressing = "Unknown"
                degraded = "Unknown"
                for cond in item.get("status", {}).get("conditions", []):
                    if cond["type"] == "Available":
                        available = cond["status"]
                    elif cond["type"] == "Progressing":
                        progressing = cond["status"]
                    elif cond["type"] == "Degraded":
                        degraded = cond["status"]
                
                status_ok = (available == "True" and degraded == "False" and progressing == "False")
                self.report_data["cluster_operators"].append({
                    "name": name,
                    "available": available,
                    "progressing": progressing,
                    "degraded": degraded,
                    "status_ok": status_ok
                })
                if not status_ok:
                    self.report_data["overall_status"] = "FAIL"
                    self.report_data["errors"].append(f"Cluster Operator '{name}' is in an unstable state (Available={available}, Degraded={degraded}, Progressing={progressing}).")
        except Exception as e:
            self.report_data["errors"].append(f"Error parsing Cluster Operators: {str(e)}")

    def check_machine_config_pools(self):
        print("Checking Machine Config Pools (MCPs)...")
        mcp_res = self.run_cmd("oc get mcp -o json")
        if not mcp_res["success"]:
            self.report_data["errors"].append("Failed to retrieve Machine Config Pools.")
            return

        try:
            data = json.loads(mcp_res["output"])
            for item in data.get("items", []):
                name = item["metadata"]["name"]
                paused = item.get("spec", {}).get("paused", False)
                degraded = False
                updated = False
                updating = False
                
                for cond in item.get("status", {}).get("conditions", []):
                    if cond["type"] == "Degraded" and cond["status"] == "True":
                        degraded = True
                    elif cond["type"] == "Updated" and cond["status"] == "True":
                        updated = True
                    elif cond["type"] == "Updating" and cond["status"] == "True":
                        updating = True

                self.report_data["machine_config_pools"].append({
                    "name": name,
                    "paused": paused,
                    "degraded": degraded,
                    "updated": updated,
                    "updating": updating
                })
                
                if degraded:
                    self.report_data["overall_status"] = "FAIL"
                    self.report_data["errors"].append(f"MachineConfigPool '{name}' is degraded.")
                if paused:
                    # Paused MCP is a warning, doesn't fail unless strict
                    self.report_data["warnings_and_events"].append(f"MachineConfigPool '{name}' is paused. Nodes will not automatically update until unpaused.")
        except Exception as e:
            self.report_data["errors"].append(f"Error parsing Machine Config Pools: {str(e)}")

    def check_nodes(self):
        print("Checking nodes status...")
        nodes_res = self.run_cmd("oc get nodes -o json")
        if not nodes_res["success"]:
            self.report_data["errors"].append("Failed to retrieve nodes information.")
            return

        try:
            data = json.loads(nodes_res["output"])
            for item in data.get("items", []):
                name = item["metadata"]["name"]
                roles = [key.replace("node-role.kubernetes.io/", "") for key in item["metadata"].get("labels", {}).keys() if "node-role.kubernetes.io/" in key]
                role_str = ",".join(roles) if roles else "worker"
                
                ready = "Unknown"
                for cond in item.get("status", {}).get("conditions", []):
                    if cond["type"] == "Ready":
                        ready = cond["status"]
                        break
                
                unschedulable = item.get("spec", {}).get("unschedulable", False)
                
                self.report_data["nodes"].append({
                    "name": name,
                    "role": role_str,
                    "ready": ready,
                    "unschedulable": unschedulable
                })
                
                if ready != "True":
                    self.report_data["overall_status"] = "FAIL"
                    self.report_data["errors"].append(f"Node '{name}' is not Ready (Status: {ready}).")
                if unschedulable:
                    self.report_data["warnings_and_events"].append(f"Node '{name}' is unschedulable (Cordoned).")
        except Exception as e:
            self.report_data["errors"].append(f"Error parsing nodes: {str(e)}")

    def check_addon_operators(self):
        print("Checking OLM Addon Operators...")
        csv_res = self.run_cmd("oc get csv -A -o json")
        if not csv_res["success"]:
            # CSV check might fail if OLM is not used, record warning
            self.report_data["warnings_and_events"].append("Could not retrieve ClusterServiceVersions (CSVs) for Add-on Operators.")
            return

        try:
            data = json.loads(csv_res["output"])
            for item in data.get("items", []):
                name = item["metadata"]["name"]
                namespace = item["metadata"]["namespace"]
                phase = item.get("status", {}).get("phase", "Unknown")
                
                self.report_data["addon_operators"].append({
                    "name": name,
                    "namespace": namespace,
                    "phase": phase
                })
                
                if phase != "Succeeded":
                    self.report_data["overall_status"] = "FAIL"
                    self.report_data["errors"].append(f"OLM Operator '{name}' in namespace '{namespace}' is in phase '{phase}' (Expected: Succeeded).")
        except Exception as e:
            self.report_data["warnings_and_events"].append(f"Error parsing Addon Operators: {str(e)}")

    def check_events_and_logs(self):
        print("Checking recent cluster Warning events...")
        events_res = self.run_cmd("oc get events -A --field-selector type=Warning -o json")
        if events_res["success"]:
            try:
                data = json.loads(events_res["output"])
                events = data.get("items", [])
                self.report_data["warning_events_count"] = len(events)
                for event in events[:10]: # Log first 10 warnings
                    msg = event.get("message", "")
                    reason = event.get("reason", "")
                    obj = event.get("involvedObject", {}).get("name", "")
                    ns = event.get("involvedObject", {}).get("namespace", "")
                    self.report_data["warnings_and_events"].append(f"Warning Event in [{ns}] on {obj} ({reason}): {msg}")
            except Exception as e:
                pass
        else:
            self.report_data["warnings_and_events"].append("Failed to fetch cluster events.")

    def generate_markdown_report(self):
        report_path = "upgrade_readiness_report.md"
        print(f"Generating markdown report at {report_path}...")
        
        status_color = "🔴 FAIL" if self.report_data["overall_status"] == "FAIL" else "🟢 PASS"
        
        md = f"""# OpenShift Upgrade Readiness Report
Generated on: `{self.report_data["timestamp"]}`

## Executive Summary
* **Current Version:** `{self.report_data["current_version"]}`
* **Target Version:** `{self.report_data["target_version"]}`
* **Readiness Status:** **{status_color}**

---

## Pre-Upgrade Readiness Checks Detailed Stand

### 1. etcd Cluster Health
* **Status:** `{self.report_data["etcd_health"]}`
* *Note: etcd must be completely healthy with low latency before proceeding. Make sure to run a backup using `cluster-backup.sh` immediately before the upgrade.*

### 2. Core Cluster Operators
| Operator Name | Available | Progressing | Degraded | Status |
|---|---|---|---|---|
"""
        for co in self.report_data["cluster_operators"]:
            co_status = "🟢 OK" if co["status_ok"] else "🔴 DEGRADED"
            md += f"| `{co['name']}` | `{co['available']}` | `{co['progressing']}` | `{co['degraded']}` | {co_status} |\n"
            
        md += """
### 3. Machine Config Pools (MCPs)
| MCP Name | Paused | Degraded | Updated | Updating | Status |
|---|---|---|---|---|---|
"""
        for mcp in self.report_data["machine_config_pools"]:
            mcp_status = "🔴 DEGRADED" if mcp["degraded"] else ("⚠️ PAUSED" if mcp["paused"] else "🟢 OK")
            md += f"| `{mcp['name']}` | `{mcp['paused']}` | `{mcp['degraded']}` | `{mcp['updated']}` | `{mcp['updating']}` | {mcp_status} |\n"

        md += """
### 4. Node Status
| Node Name | Role | Ready | Schedulable |
|---|---|---|---|
"""
        for node in self.report_data["nodes"]:
            node_status = "🟢 Yes" if (node["ready"] == "True" and not node["unschedulable"]) else "🔴 No"
            md += f"| `{node['name']}` | `{node['role']}` | `{node['ready']}` | `{not node['unschedulable']}` |\n"

        md += """
### 5. OLM Add-on Operators
| Operator CSV Name | Namespace | Phase | Status |
|---|---|---|---|
"""
        for csv in self.report_data["addon_operators"]:
            csv_status = "🟢 Succeeded" if csv["phase"] == "Succeeded" else "🔴 Unstable"
            md += f"| `{csv['name']}` | `{csv['namespace']}` | `{csv['phase']}` | {csv_status} |\n"

        md += "\n--- \n\n## Warnings, Blockers, & Log Summaries\n"
        
        if self.report_data["errors"]:
            md += "### ❌ Critical Blockers\n"
            for err in self.report_data["errors"]:
                md += f"- **BLOCKER:** {err}\n"
        else:
            md += "### ✅ No Critical Blockers Found\n"
            
        if self.report_data["warnings_and_events"]:
            md += "\n### ⚠️ Warnings / Key Events to Review\n"
            for warn in self.report_data["warnings_and_events"]:
                md += f"- {warn}\n"
                
        md += f"""
---
## Action Plan & Commands for Version {self.report_data["target_version"]}
1. **Take etcd backup:**
   ```bash
   oc debug node/$(oc get nodes -l node-role.kubernetes.io/master= -o jsonpath='{{.items[0].metadata.name}}') -- chroot /host /usr/local/bin/cluster-backup.sh /home/core/assets/backup/
   ```
2. **Perform Upgrade:**
   ```bash
   oc adm upgrade --to={self.report_data["target_version"]}
   ```
3. **Monitor Progress:**
   ```bash
   export OC_ENABLE_CMD_UPGRADE_STATUS=true
   oc adm upgrade status
   ```
"""
        with open(report_path, "w") as f:
            f.write(md)
        print(f"Report written successfully to {report_path}")

    def run_all(self):
        self.check_oc_connection()
        self.check_etcd()
        self.check_cluster_operators()
        self.check_machine_config_pools()
        self.check_nodes()
        self.check_addon_operators()
        self.check_events_and_logs()
        self.generate_markdown_report()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 openshift_upgrade_planner.py <target_version>")
        sys.argv.append("4.13.0") # default value for demonstration
    target = sys.argv[1]
    planner = OpenShiftUpgradePlanner(target)
    planner.run_all()
