#!/usr/bin/env python3
import subprocess
import json
import sys
import os
import re
from datetime import datetime
import yaml  # In standard environments, we can fallback to regex parser if PyYAML is missing

class OpenShiftUpgradePlanner:
    def __init__(self, target_version, output_dir="/tmp/UPGRADE", mode="live"):
        self.target_version = target_version
        self.output_dir = output_dir
        self.mode = mode.lower()  # 'live' or 'offline'
        self.is_must_gather = False
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

    def load_yaml(self, path):
        """Loads a YAML file using PyYAML or basic regex-based fallback if PyYAML is missing."""
        if not os.path.exists(path):
            return None
        try:
            with open(path, 'r') as f:
                return yaml.safe_load(f)
        except ImportError:
            # Simple fallback parser for basic metadata reading
            with open(path, 'r') as f:
                content = f.read()
            return self._fallback_yaml_parse(content)
        except Exception as e:
            return None

    def _fallback_yaml_parse(self, content):
        """Basic regex parser to extract common fields like name, status, phase from YAML."""
        result = {}
        metadata = {}
        status = {}
        
        # Simple extraction using regex
        name_match = re.search(r"name:\s*([\w\-\.]+)", content)
        if name_match:
            metadata["name"] = name_match.group(1)
            
        phase_match = re.search(r"phase:\s*(\w+)", content)
        if phase_match:
            status["phase"] = phase_match.group(1)
            
        result["metadata"] = metadata
        result["status"] = status
        return result

    def detect_must_gather(self):
        """Checks if the output_dir is structured as a standard must-gather directory."""
        if self.mode == "offline":
            check_paths = [
                os.path.join(self.output_dir, "cluster-scoped-resources"),
                os.path.join(self.output_dir, "namespaces")
            ]
            if any(os.path.exists(p) for p in check_paths):
                self.is_must_gather = True
                print("-> Must-gather structure detected. Switching parser strategy...")

    def run_cmd(self, cmd):
        if self.mode == "offline":
            return {"success": False, "error": "Offline mode; commands skipped."}
        try:
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, shell=True)
            if result.returncode != 0:
                return {"success": False, "error": result.stderr.strip(), "output": result.stdout.strip()}
            return {"success": True, "output": result.stdout.strip()}
        except Exception as e:
            return {"success": False, "error": str(e), "output": ""}

    def create_diagnostics_directory(self):
        if self.mode == "live":
            os.makedirs(self.output_dir, exist_ok=True)

    def collect_redhat_support_dumps(self):
        if self.mode == "offline":
            return
        print("Collecting standard cluster diagnostic dumps...")
        self.run_cmd(f"oc cluster-info dump > {self.output_dir}/cluster-info.out")
        self.run_cmd(f"oc get all -A > {self.output_dir}/resource-all.out")
        self.run_cmd(f"oc get pod -A > {self.output_dir}/pods-all.out")
        self.run_cmd(f"oc get subs -A > {self.output_dir}/subs-all.out")
        self.run_cmd(f"oc get events -A > {self.output_dir}/events-all.out")
        
        nodes_res = self.run_cmd("oc get nodes -o jsonpath='{.items[*].metadata.name}'")
        if nodes_res["success"]:
            for node in nodes_res["output"].split():
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
        
        # OCP version & core info
        version_res = self.run_cmd("oc version -o json")
        if version_res["success"]:
            try:
                version_data = json.loads(version_res["output"])
                self.report_data["current_version"] = version_data.get("openshiftVersion", "Unknown")
            except:
                pass
        
        self.run_cmd(f"oc get co -o json > {self.output_dir}/co.json")
        self.run_cmd(f"oc get mcp -o json > {self.output_dir}/mcp.json")
        self.run_cmd(f"oc get nodes -o json > {self.output_dir}/nodes.json")
        self.run_cmd(f"oc get csv -A -o json > {self.output_dir}/csv.json")
        
        # etcd deep status
        pod_res = self.run_cmd("oc get pods -n openshift-etcd -l app=etcd --field-selector='status.phase==Running' -o jsonpath='{.items[0].metadata.name}'")
        if pod_res["success"] and pod_res["output"]:
            pod_name = pod_res["output"].strip()
            etcd_cmd = f"oc exec -n openshift-etcd {pod_name} -c etcdctl -- bash -c \"etcdctl member list -w table; etcdctl endpoint health --cluster -w table; etcdctl endpoint status --cluster -w table; etcdctl alarm list\""
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
        print("Analyzing etcd status...")
        path = f"{self.output_dir}/etcd-status.out"
        if self.is_must_gather:
            # In must-gather, we can inspect openshift-etcd namespace logs or core yaml
            # E.g. finding etcd pod statuses
            self.report_data["etcd_health"] = "Refer to etcd operators in must-gather"
            return
            
        if not os.path.exists(path):
            self.report_data["etcd_health"] = "UNKNOWN"
            return

        with open(path, "r") as f:
            content = f.read()
            if "unhealthy" in content.lower():
                self.report_data["etcd_health"] = "DEGRADED"
                self.report_data["overall_status"] = "FAIL"
                self.report_data["errors"].append("etcd reports unhealthy endpoints in etcdctl health check.")
            else:
                self.report_data["etcd_health"] = "HEALTHY"

            alarm_idx = content.find("alarm list")
            if alarm_idx != -1:
                active_alarms = [l.strip() for l in content[alarm_idx:].splitlines()[1:] if l.strip()]
                if active_alarms:
                    self.report_data["etcd_alarms"] = ", ".join(active_alarms)
                    self.report_data["overall_status"] = "FAIL"
                    self.report_data["errors"].append(f"etcd has active alarms: {active_alarms}")

    def analyze_cluster_operators(self):
        print("Analyzing Cluster Operators...")
        
        if self.is_must_gather:
            # Parse from must-gather clusterversion or clusteroperators folder
            co_dir = os.path.join(self.output_dir, "cluster-scoped-resources", "config.openshift.io", "clusteroperators")
            if not os.path.exists(co_dir):
                # Check root cluster-scoped-resources file
                co_file = os.path.join(self.output_dir, "cluster-scoped-resources", "clusteroperators.yaml")
                if os.path.exists(co_file):
                    self._parse_co_yaml(co_file)
                return
                
            for filename in os.listdir(co_dir):
                if filename.endswith(".yaml"):
                    self._parse_co_yaml(os.path.join(co_dir, filename))
            return

        # Default parse co.json
        path = f"{self.output_dir}/co.json"
        if not os.path.exists(path):
            return
        with open(path, "r") as f:
            try:
                data = json.load(f)
                items = data.get("items", []) if isinstance(data, dict) else data
                for item in items:
                    name = item["metadata"]["name"]
                    available, progressing, degraded = "Unknown", "Unknown", "Unknown"
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
                        self.report_data["errors"].append(f"Cluster Operator '{name}' is degraded/unstable.")
            except Exception as e:
                self.report_data["errors"].append(f"Error parsing co.json: {str(e)}")

    def _parse_co_yaml(self, path):
        data = self.load_yaml(path)
        if not data:
            return
        # Handle lists or single objects
        items = data.get("items", [data]) if isinstance(data, dict) else [data]
        for item in items:
            name = item.get("metadata", {}).get("name", "Unknown")
            available, progressing, degraded = "Unknown", "Unknown", "Unknown"
            for cond in item.get("status", {}).get("conditions", []):
                if cond.get("type") == "Available":
                    available = cond.get("status")
                elif cond.get("type") == "Progressing":
                    progressing = cond.get("status")
                elif cond.get("type") == "Degraded":
                    degraded = cond.get("status")
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
                self.report_data["errors"].append(f"Must-gather: Operator '{name}' is degraded or unstable.")

    def analyze_machine_config_pools(self):
        print("Analyzing Machine Config Pools...")
        if self.is_must_gather:
            mcp_dir = os.path.join(self.output_dir, "cluster-scoped-resources", "machineconfiguration.openshift.io", "machineconfigpools")
            if os.path.exists(mcp_dir):
                for filename in os.listdir(mcp_dir):
                    if filename.endswith(".yaml"):
                        self._parse_mcp_yaml(os.path.join(mcp_dir, filename))
            return

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
                    degraded = any(c["status"] == "True" for c in item.get("status", {}).get("conditions", []) if c["type"] == "Degraded")
                    self.report_data["machine_config_pools"].append({
                        "name": name,
                        "paused": paused,
                        "degraded": degraded,
                        "updated": "Unknown",
                        "updating": "Unknown"
                    })
                    if degraded:
                        self.report_data["overall_status"] = "FAIL"
                        self.report_data["errors"].append(f"MachineConfigPool '{name}' is degraded.")
            except:
                pass

    def _parse_mcp_yaml(self, path):
        data = self.load_yaml(path)
        if not data:
            return
        name = data.get("metadata", {}).get("name", "Unknown")
        paused = data.get("spec", {}).get("paused", False)
        degraded = False
        for cond in data.get("status", {}).get("conditions", []):
            if cond.get("type") == "Degraded" and cond.get("status") == "True":
                degraded = True
        self.report_data["machine_config_pools"].append({
            "name": name,
            "paused": paused,
            "degraded": degraded,
            "updated": "Unknown",
            "updating": "Unknown"
        })
        if degraded:
            self.report_data["overall_status"] = "FAIL"
            self.report_data["errors"].append(f"Must-gather: MCP '{name}' is degraded.")

    def analyze_nodes(self):
        print("Analyzing nodes...")
        if self.is_must_gather:
            nodes_dir = os.path.join(self.output_dir, "cluster-scoped-resources", "core", "nodes")
            if os.path.exists(nodes_dir):
                for filename in os.listdir(nodes_dir):
                    if filename.endswith(".yaml"):
                        data = self.load_yaml(os.path.join(nodes_dir, filename))
                        if data:
                            name = data.get("metadata", {}).get("name", "Unknown")
                            ready = "Unknown"
                            for cond in data.get("status", {}).get("conditions", []):
                                if cond.get("type") == "Ready":
                                    ready = cond.get("status")
                            self.report_data["nodes"].append({
                                "name": name,
                                "role": "node",
                                "ready": ready,
                                "unschedulable": data.get("spec", {}).get("unschedulable", False)
                            })
                            if ready != "True":
                                self.report_data["overall_status"] = "FAIL"
                                self.report_data["errors"].append(f"Node '{name}' in must-gather is not Ready.")
            return

        # Default parse nodes.json
        path = f"{self.output_dir}/nodes.json"
        if not os.path.exists(path):
            return
        with open(path, "r") as f:
            try:
                data = json.load(f)
                for item in data.get("items", []):
                    name = item["metadata"]["name"]
                    ready = "Unknown"
                    for cond in item.get("status", {}).get("conditions", []):
                        if cond["type"] == "Ready":
                            ready = cond["status"]
                    self.report_data["nodes"].append({
                        "name": name,
                        "role": "node",
                        "ready": ready,
                        "unschedulable": item.get("spec", {}).get("unschedulable", False)
                    })
            except:
                pass

    def analyze_pods(self):
        print("Analyzing pods...")
        if self.is_must_gather:
            # Search namespaces for unhealthy pods
            ns_dir = os.path.join(self.output_dir, "namespaces")
            if os.path.exists(ns_dir):
                for ns in os.listdir(ns_dir):
                    pods_file = os.path.join(ns_dir, ns, "core", "pods.yaml")
                    if os.path.exists(pods_file):
                        data = self.load_yaml(pods_file)
                        if data:
                            items = data.get("items", [data]) if isinstance(data, dict) else [data]
                            for item in items:
                                name = item.get("metadata", {}).get("name")
                                status = item.get("status", {}).get("phase")
                                if status not in ["Running", "Succeeded"]:
                                    self.report_data["unhealthy_pods"].append({
                                        "namespace": ns,
                                        "name": name,
                                        "status": status
                                    })
            return

        path = f"{self.output_dir}/pods-all.out"
        if os.path.exists(path):
            with open(path, "r") as f:
                for line in f.readlines()[1:]:
                    parts = line.strip().split()
                    if len(parts) >= 3:
                        ns, name, status = parts[0], parts[1], parts[2]
                        if status not in ["Running", "Completed", "Succeeded", "Terminating"]:
                            self.report_data["unhealthy_pods"].append({
                                "namespace": ns,
                                "name": name,
                                "status": status
                            })

    def analyze_deprecated_apis(self):
        if self.is_must_gather:
            # Search inside must-gather clusterversion or apirequestcounts yaml
            return
        path = f"{self.output_dir}/apirequest-removedInRelease_count.out"
        if os.path.exists(path):
            with open(path, "r") as f:
                for line in f.readlines():
                    parts = line.strip().split("\t")
                    if len(parts) >= 3:
                        release, count, api_name = parts[0], parts[1], parts[2]
                        if int(count) > 0:
                            self.report_data["deprecated_apis_in_use"].append({
                                "api": api_name,
                                "removed_in": release,
                                "request_count": count
                            })

    def analyze_certificates(self):
        path = f"{self.output_dir}/certs2.out"
        if os.path.exists(path):
            with open(path, "r") as f:
                for line in f.readlines()[1:]:
                    parts = line.strip().split()
                    if len(parts) >= 3:
                        ns, name = parts[0], parts[1]
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
                        except:
                            pass

    def run_known_issues_analysis(self):
        # Look for target version path jumps
        try:
            v_path = os.path.join(self.output_dir, "cluster-scoped-resources", "config.openshift.io", "clusterversions", "cluster.yaml")
            if not os.path.exists(v_path):
                v_path = os.path.join(self.output_dir, "cluster-scoped-resources", "clusterversion.yaml")
            data = self.load_yaml(v_path)
            if data:
                # Find current version
                history = data.get("status", {}).get("history", [])
                if history:
                    self.report_data["current_version"] = history[0].get("version", "Unknown")
        except:
            pass

    def generate_markdown_report(self):
        report_path = "upgrade_readiness_report.md"
        status_color = "🔴 FAIL" if self.report_data["overall_status"] == "FAIL" else "🟢 PASS"
        
        md = f"""# OpenShift Upgrade Readiness & must-gather Diagnostic Report
Generated on: `{self.report_data["timestamp"]}`
Execution Mode: `{self.report_data["mode"].upper()}` (Must-gather structure: `{self.is_must_gather}`)

## Executive Summary
* **Current Version:** `{self.report_data["current_version"]}`
* **Target Version:** `{self.report_data["target_version"]}`
* **Readiness Status:** **{status_color}**

---

## Must-Gather Analysis Report

### 1. Core Cluster Operators Status
| Operator Name | Available | Progressing | Degraded | Status |
|---|---|---|---|---|
"""
        for co in self.report_data["cluster_operators"]:
            co_status = "🟢 OK" if co["status_ok"] else "🔴 DEGRADED"
            md += f"| `{co['name']}` | `{co['available']}` | `{co['progressing']}` | `{co['degraded']}` | {co_status} |\n"
            
        md += """
### 2. Machine Config Pools (MCPs)
| MCP Name | Paused | Degraded | Status |
|---|---|---|---|
"""
        for mcp in self.report_data["machine_config_pools"]:
            mcp_status = "🔴 DEGRADED" if mcp["degraded"] else ("⚠️ PAUSED" if mcp["paused"] else "🟢 OK")
            md += f"| `{mcp['name']}` | `{mcp['paused']}` | `{mcp['degraded']}` | {mcp_status} |\n"

        md += """
### 3. Node Health
| Node Name | Role | Ready | Schedulable |
|---|---|---|---|
"""
        for node in self.report_data["nodes"]:
            md += f"| `{node['name']}` | `{node['role']}` | `{node['ready']}` | `{not node['unschedulable']}` |\n"

        md += """
### 4. Pod Failures & Evictions
"""
        if self.report_data["unhealthy_pods"]:
            md += "| Namespace | Pod Name | Status |\n|---|---|---|\n"
            for pod in self.report_data["unhealthy_pods"][:15]:
                md += f"| `{pod['namespace']}` | `{pod['name']}` | `{pod['status']}` |\n"
        else:
            md += "*No unhealthy pods parsed from diagnostic dumps.*"

        md += "\n--- \n\n## Warnings & Critical Blockers\n"
        if self.report_data["errors"]:
            for err in self.report_data["errors"]:
                md += f"- **BLOCKER:** {err}\n"
        else:
            md += "- **No blockers detected.**\n"
            
        with open(report_path, "w") as f:
            f.write(md)
        print(f"Report written successfully to {report_path}")

    def run_all(self):
        self.detect_must_gather()
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
        self.analyze_deprecated_apis()
        self.analyze_certificates()
        self.run_known_issues_analysis()
        self.generate_markdown_report()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="OpenShift pre-upgrade planner.")
    parser.add_argument("target_version", help="Target OpenShift version")
    parser.add_argument("--mode", choices=["live", "offline"], default="live")
    parser.add_argument("--dir", default="/tmp/UPGRADE")
    
    args = parser.parse_args()
    planner = OpenShiftUpgradePlanner(target_version=args.target_version, output_dir=args.dir, mode=args.mode)
    planner.run_all()
