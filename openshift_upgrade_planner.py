#!/usr/bin/env python3
import subprocess
import json
import sys
import os
import re
from datetime import datetime

class OpenShiftUpgradePlanner:
    def __init__(self, target_version, output_dir="/tmp/UPGRADE", mode="live"):
        self.target_version = target_version
        self.output_dir = output_dir
        self.mode = mode.lower()  # 'live' or 'offline'
        self.report_data = {
            "timestamp": datetime.now().isoformat(),
            "mode": self.mode,
            "target_version": target_version,
            "current_version": "Unknown",
            "etcd_health": "Unknown",
            "etcd_alarms": "None",
            "cluster_operators": [],
            "machine_config_pools": [],
            "nodes": [],
            "addon_operators": [],
            "unhealthy_pods": [],
            "failed_subscriptions": [],
            "deprecated_apis_in_use": [],
            "expiring_certificates": [],
            "warnings_and_events": [],
            "overall_status": "PASS",
            "errors": []
        }

    def run_cmd(self, cmd):
        if self.mode == "offline":
            return {"success": False, "error": "Running in offline mode; commands skipped."}
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
        if self.mode == "live":
            print(f"Creating diagnostics directory at {self.output_dir}...")
            os.makedirs(self.output_dir, exist_ok=True)

    def collect_redhat_support_dumps(self):
        if self.mode == "offline":
            print("Skipping diagnostic dump collection (Offline mode).")
            return

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
        if self.mode == "offline":
            self.report_data["must_gather_command"] = "Skipped in offline mode"
            return
            
        print("Generating required Red Hat support must-gather commands...")
        odf_csv_res = self.run_cmd("oc -n openshift-storage get deployment.apps/ocs-operator -o jsonpath='{.metadata.ownerReferences[0].name}'")
        odf_image_str = ""
        if odf_csv_res["success"] and odf_csv_res["output"]:
            csv_name = odf_csv_res["output"].strip()
            image_res = self.run_cmd(f"oc -n openshift-storage get csv/{csv_name} -o json | jq '.spec.relatedImages[] | select (.name | contains (\"must-gather\")) | .image' | sed 's/\"//g'")
            if image_res["success"] and image_res["output"]:
                odf_image_str = f"--image={image_res['output'].strip()}"
        
        cnv_image = "--image=registry.redhat.io/container-native-virtualization/cnv-must-gather-rhel9:v4.18-1784294790"
        must_gather_cmd = f"oc adm must-gather {odf_image_str} --image-stream=openshift/must-gather {cnv_image}"
        
        with open(f"{self.output_dir}/must-gather-trigger.sh", "w") as f:
            f.write("#!/bin/bash\n")
            f.write(f"{must_gather_cmd} --dest-dir={self.output_dir}/must-gather-data\n")
        os.chmod(f"{self.output_dir}/must-gather-trigger.sh", 0o755)
        self.report_data["must_gather_command"] = must_gather_cmd

    def execute_live_checks(self):
        if self.mode == "offline":
            return
            
        print("Running live cluster validation commands...")
        # Get OCP Version
        version_res = self.run_cmd("oc version -o json")
        if version_res["success"]:
            try:
                version_data = json.loads(version_res["output"])
                self.report_data["current_version"] = version_data.get("openshiftVersion", "Unknown")
            except:
                pass
        
        # OCP Core Operators
        self.run_cmd("oc get co -o json > " + f"{self.output_dir}/co.json")
        
        # Machine Config Pools
        self.run_cmd("oc get mcp -o json > " + f"{self.output_dir}/mcp.json")
        
        # Nodes
        self.run_cmd("oc get nodes -o json > " + f"{self.output_dir}/nodes.json")
        
        # CSVs
        self.run_cmd("oc get csv -A -o json > " + f"{self.output_dir}/csv.json")
        
        # etcd deep status
        pod_res = self.run_cmd("oc get pods -n openshift-etcd -l app=etcd --field-selector='status.phase==Running' -o jsonpath='{.items[0].metadata.name}'")
        if pod_res["success"] and pod_res["output"]:
            pod_name = pod_res["output"].strip()
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

        # API requests
        self.run_cmd(f"oc get apirequestcounts > {self.output_dir}/apirequestcounts.out")
        
        ua_cmd = (
            "oc get apirequestcounts -o jsonpath='{range .items[?(@.status.removedInRelease!=\"\")]}{.metadata.name}{\"\\n\"}{end}' | "
            "xargs -I {} sh -c 'echo \"\\n==> $1\\n\" && oc get apirequestcount $1 -o yaml | grep -E \"username:|userAgent:\" | sort | uniq' sh {} "
            f"> {self.output_dir}/apirequest-userAgent.out"
        )
        self.run_cmd(ua_cmd)
        
        summary_cmd = (
            "oc get apirequestcounts -o jsonpath='{range .items[?(@.status.removedInRelease!=\"\")]}{.status.removedInRelease}{\"\\t\"}{.status.requestCount}{\"\\t\"}{.metadata.name}{\"\\n\"}{end}' "
            f"> {self.output_dir}/apirequest-removedInRelease_count.out"
        )
        self.run_cmd(summary_cmd)
        
        # Certificates
        certs_cmd = (
            "oc get secrets -A -o json | jq -r '.items | sort_by(.metadata.namespace,.metadata.name) |.[] |"
            "select((.type == \"kubernetes.io/tls\") or (.type == \"SecretTypeTLS\"))| \"\\(.metadata.namespace) \\(.metadata.name) \\(.data | to_entries[] | select(.key | test(\"key\") or test(\"Key\") | not)| .value)\"' | "
            "while read namespace name cert; do echo -e \"\\n${namespace} - ${name}\\n##################################################\"; "
            "echo $cert | base64 -d | openssl crl2pkcs7 -nocrl -certfile /dev/stdin | openssl pkcs7 -print_certs -text -noout | grep -A4 Issuer:; "
            f"done > {self.output_dir}/certs.out"
        )
        self.run_cmd(certs_cmd)
        
        certs2_cmd = (
            "(echo -e \"NAMESPACE\\tNAME\\tEXPIRY\" && oc get secrets -A -o go-template='"
            "{{range .items}}{{if eq .type \"kubernetes.io/tls\"}}{{.metadata.namespace}}{\" \"}}{{.metadata.name}}{\" \"}}{{index .data \"tls.crt\"}}{\"\\n\"}}{{end}}{{end}}' | "
            "while read namespace name cert; do echo -en \"$namespace\\t$name\\t\"; echo $cert | base64 -d | openssl x509 -noout -enddate; "
            f"done ) | column -t > {self.output_dir}/certs2.out"
        )
        self.run_cmd(certs2_cmd)

    def analyze_etcd(self):
        print("Analyzing etcd dumps...")
        path = f"{self.output_dir}/etcd-status.out"
        if not os.path.exists(path):
            self.report_data["etcd_health"] = "UNKNOWN"
            self.report_data["warnings_and_events"].append("etcd diagnostics file etcd-status.out not found.")
            return

        with open(path, "r") as f:
            content = f.read()
            if "unhealthy" in content.lower():
                self.report_data["etcd_health"] = "DEGRADED"
                self.report_data["overall_status"] = "FAIL"
                self.report_data["errors"].append("etcd shows unhealthy endpoints in endpoint health table.")
            else:
                self.report_data["etcd_health"] = "HEALTHY"

            # Parse alarms
            alarm_idx = content.find("alarm list")
            if alarm_idx != -1:
                alarm_section = content[alarm_idx:].strip()
                lines = alarm_section.splitlines()[1:]
                active_alarms = [l.strip() for l in lines if l.strip()]
                if active_alarms:
                    self.report_data["etcd_alarms"] = ", ".join(active_alarms)
                    self.report_data["overall_status"] = "FAIL"
                    self.report_data["errors"].append(f"etcd cluster has active alarms: {active_alarms}")

    def analyze_cluster_operators(self):
        print("Analyzing Cluster Operators...")
        path = f"{self.output_dir}/co.json"
        if not os.path.exists(path):
            self.report_data["warnings_and_events"].append("co.json not found, skipping core operator analysis.")
            return
            
        with open(path, "r") as f:
            try:
                data = json.load(f)
                items = data.get("items", []) if isinstance(data, dict) else data
                for item in items:
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
                        self.report_data["errors"].append(f"Cluster Operator '{name}' is unstable (Available={available}, Degraded={degraded}, Progressing={progressing}).")
            except Exception as e:
                self.report_data["errors"].append(f"Error parsing co.json: {str(e)}")

    def analyze_machine_config_pools(self):
        print("Analyzing Machine Config Pools...")
        path = f"{self.output_dir}/mcp.json"
        if not os.path.exists(path):
            return
            
        with open(path, "r") as f:
            try:
                data = json.load(f)
                items = data.get("items", []) if isinstance(data, dict) else data
                for item in items:
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
                        self.report_data["warnings_and_events"].append(f"MachineConfigPool '{name}' is paused. Node OS/Config upgrades are suspended.")
            except Exception as e:
                self.report_data["errors"].append(f"Error parsing mcp.json: {str(e)}")

    def analyze_nodes(self):
        print("Analyzing nodes status...")
        path = f"{self.output_dir}/nodes.json"
        if not os.path.exists(path):
            return
            
        with open(path, "r") as f:
            try:
                data = json.load(f)
                items = data.get("items", []) if isinstance(data, dict) else data
                for item in items:
                    name = item["metadata"]["name"]
                    roles = [k.replace("node-role.kubernetes.io/", "") for k in item["metadata"].get("labels", {}).keys() if "node-role.kubernetes.io/" in k]
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
                        self.report_data["errors"].append(f"Node '{name}' is not Ready (Ready={ready}).")
                    if unschedulable:
                        self.report_data["warnings_and_events"].append(f"Node '{name}' is cordoned (SchedulingDisabled).")
            except Exception as e:
                self.report_data["errors"].append(f"Error parsing nodes.json: {str(e)}")

    def analyze_pods(self):
        print("Analyzing pod health across all namespaces...")
        path = f"{self.output_dir}/pods-all.out"
        if not os.path.exists(path):
            self.report_data["warnings_and_events"].append("pods-all.out not found, skipping pod diagnostics.")
            return

        with open(path, "r") as f:
            lines = f.readlines()[1:] # skip header
            for line in lines:
                parts = line.strip().split()
                if len(parts) >= 4:
                    namespace = parts[0]
                    name = parts[1]
                    status = parts[2]
                    if status not in ["Running", "Completed", "Succeeded", "Terminating"]:
                        self.report_data["unhealthy_pods"].append({
                            "namespace": namespace,
                            "name": name,
                            "status": status
                        })
                        self.report_data["warnings_and_events"].append(f"Unhealthy Pod: [{namespace}] {name} is in status '{status}'")

    def analyze_subscriptions(self):
        print("Analyzing OLM subscriptions...")
        path = f"{self.output_dir}/subs-all.out"
        if not os.path.exists(path):
            return
            
        with open(path, "r") as f:
            lines = f.readlines()[1:]
            for line in lines:
                parts = line.strip().split()
                if len(parts) >= 2:
                    ns = parts[0]
                    name = parts[1]
                    pass

    def analyze_deprecated_apis(self):
        print("Analyzing deprecated APIs count...")
        path = f"{self.output_dir}/apirequest-removedInRelease_count.out"
        if not os.path.exists(path):
            return
            
        with open(path, "r") as f:
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
                        try:
                            target_float = float(".".join(self.target_version.split(".")[:2]))
                            release_float = float(release)
                            if target_float >= release_float:
                                self.report_data["overall_status"] = "FAIL"
                                self.report_data["errors"].append(
                                    f"Critical: API '{api_name}' is removed in release {release}, but has {count} active calls. Upgrade is blocked."
                                )
                        except ValueError:
                            pass

    def analyze_certificates(self):
        print("Analyzing certificates expiry...")
        path = f"{self.output_dir}/certs2.out"
        if not os.path.exists(path):
            return
            
        with open(path, "r") as f:
            lines = f.readlines()[1:]
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
                                f"TLS Secret '{name}' in [{ns}] expires in {days_remaining} days (Expiry: {expiry_str})."
                            )
                            if days_remaining < 7:
                                self.report_data["overall_status"] = "FAIL"
                                self.report_data["errors"].append(
                                    f"Critical Certificate Expiry: '{name}' in namespace '{ns}' expires in {days_remaining} days."
                                )
                    except:
                        pass

    def run_known_issues_analysis(self):
        print("Checking for known upgrade issues & version-specific blockers...")
        try:
            curr_match = re.search(r"4\.(\d+)", self.report_data["current_version"])
            target_match = re.search(r"4\.(\d+)", self.target_version)
            if curr_match and target_match:
                curr_minor = int(curr_match.group(1))
                target_minor = int(target_match.group(1))
                
                if target_minor - curr_minor > 1:
                    self.report_data["overall_status"] = "FAIL"
                    self.report_data["errors"].append(
                        f"Multi-minor version hops are unsupported (Current: 4.{curr_minor} -> Target: 4.{target_minor}). You must upgrade sequentially."
                    )
                
                if curr_minor == 11 and target_minor == 12:
                    self.report_data["warnings_and_events"].append(
                        "Note: Upgrading 4.11 -> 4.12 removes `v1beta1` ingress/flowcontrol APIs. Ensure no legacy ingress resources remain."
                    )
                elif curr_minor == 12 and target_minor == 13:
                    self.report_data["warnings_and_events"].append(
                        "Note: Upgrading 4.12 -> 4.13 removes `v1beta1` PodDisruptionBudget. Workloads must use `policy/v1`."
                    )
        except Exception as e:
            pass

        events_path = f"{self.output_dir}/events-all.out"
        if os.path.exists(events_path):
            with open(events_path, "r") as f:
                content = f.read()
                if "catalogsource" in content.lower() and "fail" in content.lower():
                    self.report_data["warnings_and_events"].append(
                        "Warning: Detected CatalogSource or Operator registry errors in cluster events. Check OLM health."
                    )

    def generate_markdown_report(self):
        report_path = "upgrade_readiness_report.md"
        print(f"Generating markdown report at {report_path}...")
        
        status_color = "🔴 FAIL" if self.report_data["overall_status"] == "FAIL" else "🟢 PASS"
        
        md = f"""# OpenShift Upgrade Readiness & Diagnostic Report
Generated on: `{self.report_data["timestamp"]}`
Execution Mode: `{self.report_data["mode"].upper()}`

## Executive Summary
* **Current Version:** `{self.report_data["current_version"]}`
* **Target Version:** `{self.report_data["target_version"]}`
* **Readiness Status:** **{status_color}**
* **Diagnostic Dump Location:** `{self.output_dir}`

---

## Pre-Upgrade Readiness Checks Detailed Stand

### 1. etcd Health & Alarms
* **etcd Health Status:** `{self.report_data["etcd_health"]}`
* **Active Alarms:** `{self.report_data["etcd_alarms"]}`

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
### 6. Unhealthy Pods (Warning State)
"""
        if self.report_data["unhealthy_pods"]:
            md += "| Namespace | Pod Name | Status |\n|---|---|---|\n"
            for pod in self.report_data["unhealthy_pods"][:15]:
                md += f"| `{pod['namespace']}` | `{pod['name']}` | `{pod['status']}` |\n"
            if len(self.report_data["unhealthy_pods"]) > 15:
                md += f"\n*And {len(self.report_data['unhealthy_pods']) - 15} more unhealthy pods. See `{self.output_dir}/pods-all.out`.*"
        else:
            md += "*All pods are healthy (Running/Succeeded).* "

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
        if self.mode == "live":
            self.check_oc_connection()
            self.create_diagnostics_directory()
            self.collect_redhat_support_dumps()
            self.run_must_gather_command_generation()
            self.execute_live_checks()
            
        self.analyze_etcd()
        self.analyze_cluster_operators()
        self.analyze_machine_config_pools()
        self.analyze_nodes()
        self.analyze_pods()
        self.analyze_subscriptions()
        self.analyze_deprecated_apis()
        self.analyze_certificates()
        self.run_known_issues_analysis()
        self.generate_markdown_report()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="OpenShift pre-upgrade planner.")
    parser.add_argument("target_version", help="Target OpenShift version (e.g. 4.13.10)")
    parser.add_argument("--mode", choices=["live", "offline"], default="live", help="Execution mode (live cluster query or offline dump analysis)")
    parser.add_argument("--dir", default="/tmp/UPGRADE", help="Diagnostics output/input directory")
    
    args = parser.parse_args()
    
    planner = OpenShiftUpgradePlanner(target_version=args.target_version, output_dir=args.dir, mode=args.mode)
    planner.run_all()
