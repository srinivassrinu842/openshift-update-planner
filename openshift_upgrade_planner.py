#!/usr/bin/env python3
import subprocess
import json
import sys
import os
from datetime import datetime

class OpenShiftUpgradePlanner:
    def __init__(self, target_version, output_dir="/tmp/UPGRADE"):
        self.target_version = target_version
        self.output_dir = output_dir
        self.report_data = {
            "timestamp": datetime.now().isoformat(),
            "target_version": target_version,
            "current_version": "Unknown",
            "etcd_health": "Unknown",
            "etcd_alarms": "None",
            "cluster_operators": [],
            "machine_config_pools": [],
            "nodes": [],
            "addon_operators": [],
            "warnings_and_events": [],
            "deprecated_apis_in_use": [],
            "expiring_certificates": [],
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
            fallback = self.run_cmd("oc version")
            if fallback["success"]:
                for line in fallback["output"].splitlines():
                    if "Server Version:" in line or "openshiftVersion:" in line:
                        self.report_data["current_version"] = line.split()[-1]

    def create_diagnostics_directory(self):
        print(f"Creating diagnostics directory at {self.output_dir}...")
        os.makedirs(self.output_dir, exist_ok=True)

    def collect_redhat_support_dumps(self):
        print("Collecting standard cluster diagnostic dumps...")
        
        # 1. Cluster Info Dump
        print("-> Dumping cluster info...")
        self.run_cmd(f"oc cluster-info dump > {self.output_dir}/cluster-info.out")
        
        # 2. Resource Dump
        print("-> Dumping all resources...")
        self.run_cmd(f"oc get all -A > {self.output_dir}/resource-all.out")
        
        # 3. Pods Dump
        print("-> Dumping all pods...")
        self.run_cmd(f"oc get pod -A > {self.output_dir}/pods-all.out")
        
        # 4. Subscriptions Dump
        print("-> Dumping operator subscriptions...")
        self.run_cmd(f"oc get subs -A > {self.output_dir}/subs-all.out")
        
        # 5. Events Dump
        print("-> Dumping cluster events...")
        self.run_cmd(f"oc get events -A > {self.output_dir}/events-all.out")
        
        # 6. Nodes Description Loop
        print("-> Describing all nodes...")
        nodes_res = self.run_cmd("oc get nodes -o jsonpath='{.items[*].metadata.name}'")
        if nodes_res["success"]:
            nodes = nodes_res["output"].split()
            for node in nodes:
                self.run_cmd(f"oc describe node {node} > {self.output_dir}/{node}.info")

    def run_must_gather_command_generation(self):
        print("Generating required Red Hat support must-gather commands...")
        # Get OCS/ODF operator CSV name
        odf_csv_res = self.run_cmd("oc -n openshift-storage get deployment.apps/ocs-operator -o jsonpath='{.metadata.ownerReferences[0].name}'")
        odf_image_str = ""
        if odf_csv_res["success"] and odf_csv_res["output"]:
            csv_name = odf_csv_res["output"].strip()
            image_res = self.run_cmd(f"oc -n openshift-storage get csv/{csv_name} -o json | jq '.spec.relatedImages[] | select (.name | contains (\"must-gather\")) | .image' | sed 's/\"//g'")
            if image_res["success"] and image_res["output"]:
                odf_image_str = f"--image={image_res['output'].strip()}"
        
        cnv_image = "--image=registry.redhat.io/container-native-virtualization/cnv-must-gather-rhel9:v4.18-1784294790"
        
        must_gather_cmd = f"oc adm must-gather {odf_image_str} --image-stream=openshift/must-gather {cnv_image}"
        
        # Write the generated command to a script file in the UPGRADE directory for support
        with open(f"{self.output_dir}/must-gather-trigger.sh", "w") as f:
            f.write("#!/bin/bash\n")
            f.write(f"{must_gather_cmd} --dest-dir={self.output_dir}/must-gather-data\n")
        os.chmod(f"{self.output_dir}/must-gather-trigger.sh", 0o755)
        self.report_data["must_gather_command"] = must_gather_cmd

    def check_etcd(self):
        print("Checking etcd health & alarms...")
        # Find etcd pod name
        pod_res = self.run_cmd("oc get pods -n openshift-etcd -l app=etcd --field-selector='status.phase==Running' -o jsonpath='{.items[0].metadata.name}'")
        if pod_res["success"] and pod_res["output"]:
            pod_name = pod_res["output"].strip()
            
            # Execute diagnostic command
            etcd_cmd = (
                f"oc exec -n openshift-etcd {pod_name} -c etcdctl -- bash -c "
                f"\"etcdctl member list -w table; "
                f"etcdctl endpoint health --cluster -w table; "
                f"etcdctl endpoint status --cluster -w table; "
                f"etcdctl alarm list\""
            )
            etcd_res = self.run_cmd(etcd_cmd)
            if etcd_res["success"]:
                with open(f"{self.output_dir}/etcd-status.out", "w") as f:
                    f.write(etcd_res["output"])
                
                # Analyze output
                output = etcd_res["output"]
                if "unhealthy" in output.lower():
                    self.report_data["etcd_health"] = "DEGRADED"
                    self.report_data["overall_status"] = "FAIL"
                    self.report_data["errors"].append("etcd reports unhealthy endpoints in etcdctl endpoint health.")
                else:
                    self.report_data["etcd_health"] = "HEALTHY"
                
                # Parse alarms
                alarm_lines = [line for line in output.splitlines() if "alarm" in line.lower() or "alarm list" in line.lower()]
                # If there are active alarms listed after the alarm list call
                alarm_idx = output.find("alarm list")
                if alarm_idx != -1:
                    alarm_section = output[alarm_idx:].strip()
                    alarm_lines_list = alarm_section.splitlines()[1:] # skip header
                    active_alarms = [l for l in alarm_lines_list if l.strip()]
                    if active_alarms:
                        self.report_data["etcd_alarms"] = ", ".join(active_alarms)
                        self.report_data["overall_status"] = "FAIL"
                        self.report_data["errors"].append(f"etcd has active alarms: {active_alarms}")
            else:
                self.report_data["etcd_health"] = "UNKNOWN"
                self.report_data["errors"].append(f"Failed to execute etcdctl diagnostics: {etcd_res['error']}")
        else:
            self.report_data["etcd_health"] = "UNAVAILABLE"
            self.report_data["errors"].append("Could not find any running etcd pod to execute diagnostics.")

    def check_deprecated_apis(self):
        print("Checking for deprecated or removed APIs in use...")
        self.run_cmd(f"oc get apirequestcounts > {self.output_dir}/apirequestcounts.out")
        
        # UserAgent count list
        ua_cmd = (
            "oc get apirequestcounts -o jsonpath='{range .items[?(@.status.removedInRelease!=\"\")]}{.metadata.name}{\"\\n\"}{end}' | "
            "xargs -I {} sh -c 'echo \"\\n==> $1\\n\" && oc get apirequestcount $1 -o yaml | grep -E \"username:|userAgent:\" | sort | uniq' sh {} "
            f"> {self.output_dir}/apirequest-userAgent.out"
        )
        self.run_cmd(ua_cmd)
        
        # RemovedInRelease count summary
        summary_cmd = (
            "oc get apirequestcounts -o jsonpath='{range .items[?(@.status.removedInRelease!=\"\")]}{.status.removedInRelease}{\"\\t\"}{.status.requestCount}{\"\\t\"}{.metadata.name}{\"\\n\"}{end}' "
            f"> {self.output_dir}/apirequest-removedInRelease_count.out"
        )
        self.run_cmd(summary_cmd)
        
        # Parse findings in python
        if os.path.exists(f"{self.output_dir}/apirequest-removedInRelease_count.out"):
            with open(f"{self.output_dir}/apirequest-removedInRelease_count.out", "r") as f:
                lines = f.readlines()
                for line in lines:
                    parts = line.strip().split("\t")
                    if len(parts) >= 3:
                        release, count, api_name = parts[0], parts[1], parts[2]
                        if int(count) > 0:
                            self.report_data["deprecated_apis_in_use"].append({
                                "api": api_name,
                                "removed_in": release,
                                "request_count": count
                            })
                            # If it's removed in or before target version, it is a blocker
                            try:
                                target_float = float(".".join(self.target_version.split(".")[:2]))
                                release_float = float(release)
                                if target_float >= release_float:
                                    self.report_data["overall_status"] = "FAIL"
                                    self.report_data["errors"].append(
                                        f"API '{api_name}' is deprecated and removed in version {release}, but has {count} active requests."
                                    )
                            except ValueError:
                                pass

    def check_certificates(self):
        print("Checking TLS Certificates validity & expiration dates...")
        
        # Dump detailed cert information
        certs_cmd = (
            "oc get secrets -A -o json | jq -r '.items | sort_by(.metadata.namespace,.metadata.name) |.[] |"
            "select((.type == \"kubernetes.io/tls\") or (.type == \"SecretTypeTLS\"))| \"\\(.metadata.namespace) \\(.metadata.name) \\(.data | to_entries[] | select(.key | test(\"key\") or test(\"Key\") | not)| .value)\"' | "
            "while read namespace name cert; do echo -e \"\\n${namespace} - ${name}\\n##################################################\"; "
            "echo $cert | base64 -d | openssl crl2pkcs7 -nocrl -certfile /dev/stdin | openssl pkcs7 -print_certs -text -noout | grep -A4 Issuer:; "
            f"done > {self.output_dir}/certs.out"
        )
        self.run_cmd(certs_cmd)
        
        # Parse expiration dates
        certs2_cmd = (
            "(echo -e \"NAMESPACE\\tNAME\\tEXPIRY\" && oc get secrets -A -o go-template='"
            "{{range .items}}{{if eq .type \"kubernetes.io/tls\"}}{{.metadata.namespace}}{\" \"}}{{.metadata.name}}{\" \"}}{{index .data \"tls.crt\"}}{\"\\n\"}}{{end}}{{end}}' | "
            "while read namespace name cert; do echo -en \"$namespace\\t$name\\t\"; echo $cert | base64 -d | openssl x509 -noout -enddate; "
            f"done ) | column -t > {self.output_dir}/certs2.out"
        )
        self.run_cmd(certs2_cmd)
        
        # Analyze certs2.out for quick-expiration
        if os.path.exists(f"{self.output_dir}/certs2.out"):
            with open(f"{self.output_dir}/certs2.out", "r") as f:
                lines = f.readlines()[1:] # skip header
                for line in lines:
                    parts = line.strip().split()
                    if len(parts) >= 3:
                        ns = parts[0]
                        name = parts[1]
                        expiry_str = " ".join(parts[2:]).replace("notAfter=", "")
                        try:
                            expiry_clean = expiry_str.split("GMT")[0].strip()
                            expiry_dt = datetime.strptime(expiry_clean, "%b %d %H:%M:%S %Y")
                            days_remaining = (expiry_dt - datetime.now()).days
                            
                            self.report_data["expiring_certificates"].append({
                                "namespace": ns,
                                "name": name,
                                "expiry": expiry_str,
                                "days_remaining": days_remaining
                            })
                            
                            if days_remaining < 30:
                                self.report_data["warnings_and_events"].append(
                                    f"Certificate '{name}' in namespace '{ns}' is expiring in {days_remaining} days (Expiry: {expiry_str})."
                                )
                                if days_remaining < 7:
                                    self.report_data["overall_status"] = "FAIL"
                                    self.report_data["errors"].append(
                                        f"Critical Certificate Expiration: '{name}' in namespace '{ns}' expires in {days_remaining} days!"
                                    )
                        except Exception as e:
                            self.report_data["expiring_certificates"].append({
                                "namespace": ns,
                                "name": name,
                                "expiry": expiry_str,
                                "days_remaining": "Unknown"
                            })

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
                for event in events[:15]:
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
        
        md = f"""# OpenShift Upgrade Readiness & Diagnostic Report
Generated on: `{self.report_data["timestamp"]}`

## Executive Summary
* **Current Version:** `{self.report_data["current_version"]}`
* **Target Version:** `{self.report_data["target_version"]}`
* **Readiness Status:** **{status_color}**
* **Diagnostic Dump Location:** `{self.output_dir}` (Contains all dumps requested by Red Hat Support)

---

## Pre-Upgrade Readiness Checks Detailed Stand

### 1. etcd Health & Alarms
* **etcd Health Status:** `{self.report_data["etcd_health"]}`
* **Active Alarms:** `{self.report_data["etcd_alarms"]}`
* *Diagnostic output written to `{self.output_dir}/etcd-status.out`*

### 2. Deprecated & Removed APIs in Use
Check this table for APIs that will be unavailable after the upgrade:
"""
        if self.report_data["deprecated_apis_in_use"]:
            md += "| API Name | Removed In Version | Active Request Count |\n|---|---|---|\n"
            for api in self.report_data["deprecated_apis_in_use"]:
                md += f"| `{api['api']}` | `{api['removed_in']}` | `{api['request_count']}` |\n"
            md += f"\n*Refer to `{self.output_dir}/apirequest-userAgent.out` to trace exact UserAgents and Usernames calling these APIs.*"
        else:
            md += "*No deprecated or removed APIs with active requests detected.*"

        md += """

### 3. Core Cluster Operators
| Operator Name | Available | Progressing | Degraded | Status |
|---|---|---|---|---|
"""
        for co in self.report_data["cluster_operators"]:
            co_status = "🟢 OK" if co["status_ok"] else "🔴 DEGRADED"
            md += f"| `{co['name']}` | `{co['available']}` | `{co['progressing']}` | `{co['degraded']}` | {co_status} |\n"
            
        md += """
### 4. Machine Config Pools (MCPs)
| MCP Name | Paused | Degraded | Updated | Updating | Status |
|---|---|---|---|---|---|
"""
        for mcp in self.report_data["machine_config_pools"]:
            mcp_status = "🔴 DEGRADED" if mcp["degraded"] else ("⚠️ PAUSED" if mcp["paused"] else "🟢 OK")
            md += f"| `{mcp['name']}` | `{mcp['paused']}` | `{mcp['degraded']}` | `{mcp['updated']}` | `{mcp['updating']}` | {mcp_status} |\n"

        md += """
### 5. Node Status
| Node Name | Role | Ready | Schedulable |
|---|---|---|---|
"""
        for node in self.report_data["nodes"]:
            node_status = "🟢 Yes" if (node["ready"] == "True" and not node["unschedulable"]) else "🔴 No"
            md += f"| `{node['name']}` | `{node['role']}` | `{node['ready']}` | `{not node['unschedulable']}` |\n"

        md += """
### 6. OLM Add-on Operators
| Operator CSV Name | Namespace | Phase | Status |
|---|---|---|---|
"""
        for csv in self.report_data["addon_operators"]:
            csv_status = "🟢 Succeeded" if csv["phase"] == "Succeeded" else "🔴 Unstable"
            md += f"| `{csv['name']}` | `{csv['namespace']}` | `{csv['phase']}` | {csv_status} |\n"

        md += """
### 7. Expiring Certificates (Next 30 Days)
"""
        expiring = [c for c in self.report_data["expiring_certificates"] if isinstance(c["days_remaining"], int) and c["days_remaining"] < 30]
        if expiring:
            md += "| Namespace | Secret Name | Expiration Date | Days Remaining |\n|---|---|---|---|\n"
            for cert in expiring:
                md += f"| `{cert['namespace']}` | `{cert['name']}` | `{cert['expiry']}` | `{cert['days_remaining']}` |\n"
        else:
            md += "*All parsed TLS secrets are valid for > 30 days. See full details in `certs2.out`.*"

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
1. **Red Hat Support must-gather script:**
   To gather diagnostics requested by support, run:
   ```bash
   {self.output_dir}/must-gather-trigger.sh
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
        self.create_diagnostics_directory()
        self.collect_redhat_support_dumps()
        self.run_must_gather_command_generation()
        self.check_etcd()
        self.check_cluster_operators()
        self.check_machine_config_pools()
        self.check_nodes()
        self.check_addon_operators()
        self.check_deprecated_apis()
        self.check_certificates()
        self.check_events_and_logs()
        self.generate_markdown_report()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 openshift_upgrade_planner.py <target_version> [output_dir]")
        sys.argv.append("4.13.0")
    
    target = sys.argv[1]
    out_dir = "/tmp/UPGRADE"
    if len(sys.argv) > 2:
        out_dir = sys.argv[2]
        
    planner = OpenShiftUpgradePlanner(target, out_dir)
    planner.run_all()
