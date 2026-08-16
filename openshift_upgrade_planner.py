#!/usr/bin/env python3
import subprocess
import json
import sys
import os
import re
from datetime import datetime
import urllib.request
import urllib.parse

# Fallback for YAML parsing if PyYAML is missing
try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

class OpenShiftUpgradePlanner:
    COMPATIBILITY_MATRICES = {
        "ocs-operator": { # Red Hat OpenShift Data Foundation (ODF)
            "4.10": ["4.10"], "4.11": ["4.11"], "4.12": ["4.12"],
            "4.13": ["4.13"], "4.14": ["4.14"], "4.15": ["4.15"],
            "4.16": ["4.16"], "4.17": ["4.17"], "4.18": ["4.18"]
        },
        "kubevirt-hyperconverged": { # OpenShift Virtualization (CNV)
            "4.10": ["4.10"], "4.11": ["4.11"], "4.12": ["4.12"],
            "4.13": ["4.13"], "4.14": ["4.14"], "4.15": ["4.15"],
            "4.16": ["4.16"], "4.17": ["4.17"], "4.18": ["4.18"]
        },
        "advanced-cluster-management": { # Advanced Cluster Management (ACM)
            "2.6": ["4.10", "4.11"],
            "2.7": ["4.10", "4.11", "4.12"],
            "2.8": ["4.11", "4.12", "4.13"],
            "2.9": ["4.12", "4.13", "4.14"],
            "2.10": ["4.13", "4.14", "4.15"],
            "2.11": ["4.14", "4.15", "4.16"],
            "2.12": ["4.15", "4.16", "4.17"]
        },
        "openshift-gitops-operator": { # OpenShift GitOps (ArgoCD)
            "1.7": ["4.10", "4.11"],
            "1.8": ["4.11", "4.12"],
            "1.9": ["4.12", "4.13"],
            "1.10": ["4.13", "4.14"],
            "1.11": ["4.14", "4.15"],
            "1.12": ["4.15", "4.16"],
            "1.13": ["4.16", "4.17"]
        }
    }

    def __init__(self, target_version, output_dir="/tmp/UPGRADE", mode="live", proxy=None):
        self.target_version = target_version
        self.output_dir = output_dir
        self.mode = mode.lower()
        self.proxy = proxy
        self.is_must_gather = False
        self.cluster_channel = "stable-4.13"  # Default fallback
        self.cluster_arch = "amd64"          # Default fallback
        self.report_data = {
            "timestamp": datetime.now().isoformat(),
            "mode": self.mode,
            "target_version": target_version,
            "current_version": "Unknown",
            "upgrade_path": [],
            "etcd_health": "Unknown",
            "etcd_alarms": "None",
            "cluster_operators": [],
            "machine_config_pools": [],
            "nodes": [],
            "addon_operators": [],
            "operator_compatibility_issues": [],
            "unhealthy_pods": [],
            "failed_subscriptions": [],
            "deprecated_apis_in_use": [],
            "expiring_certificates": [],
            "warnings_and_events": [],
            "overall_status": "PASS",
            "errors": []
        }

    def load_yaml(self, path):
        if not os.path.exists(path):
            return None
        try:
            if HAS_YAML:
                with open(path, 'r') as f:
                    return yaml.safe_load(f)
            else:
                with open(path, 'r') as f:
                    content = f.read()
                return self._fallback_yaml_parse(content)
        except Exception:
            return None

    def _fallback_yaml_parse(self, content):
        result = {}
        metadata = {}
        status = {}
        name_match = re.search(r"name:\s*([\w\-\.]+)", content)
        if name_match:
            metadata["name"] = name_match.group(1)
        phase_match = re.search(r"phase:\s*(\w+)", content)
        if phase_match:
            status["phase"] = phase_match.group(1)
        
        annotations = {}
        ann_matches = re.findall(r"olm\.maxOpenShiftVersion:\s*\"?([\w\-\.]+)\"?", content)
        if ann_matches:
            annotations["olm.maxOpenShiftVersion"] = ann_matches[0]
            
        metadata["annotations"] = annotations
        result["metadata"] = metadata
        result["status"] = status
        return result

    def detect_must_gather(self):
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

    def ask_credentials_and_proxy(self):
        """Asks user if proxy or specific credentials are required for connection."""
        if self.mode == "offline":
            return

        print("\n--- Network & Registry Credentials Check ---")
        use_proxy = input("Does your environment require a proxy to access public APIs (e.g. Red Hat Upgrade Graph)? (y/N): ").strip().lower()
        if use_proxy == 'y':
            self.proxy = input("Enter proxy URL (e.g. http://username:password@proxy.example.com:8080): ").strip()
            print(f"Proxy set to: {self.proxy}")

        use_pull_secret = input("Do you want to extract and validate the cluster's global registry pull secret? (y/N): ").strip().lower()
        if use_pull_secret == 'y':
            print("-> Extracting global pull-secret from openshift-config...")
            res = self.run_cmd("oc get secret/pull-secret -n openshift-config -o jsonpath='{.data.\\.dockerconfigjson}' | base64 -d")
            if res["success"]:
                secret_path = os.path.join(self.output_dir, "extracted-pull-secret.json")
                with open(secret_path, "w") as f:
                    f.write(res["output"])
                print(f"Successfully exported pull-secret to {secret_path}")
            else:
                print(f"Failed to extract pull-secret: {res['error']}")

    def query_upgrade_graph(self):
        """Queries the Red Hat OpenShift Upgrade Graph API dynamically."""
        print(f"Querying Red Hat Upgrade Graph for path validation (Channel: {self.cluster_channel}, Arch: {self.cluster_arch})...")
        url = f"https://api.openshift.com/api/upgrades_info/v1/graph?channel={self.cluster_channel}&arch={self.cluster_arch}"
        
        try:
            # Set up proxy handler if proxy is defined
            if self.proxy:
                proxy_support = urllib.request.ProxyHandler({'http': self.proxy, 'https': self.proxy})
                opener = urllib.request.build_opener(proxy_support)
                urllib.request.install_opener(opener)
            
            req = urllib.request.Request(url, headers={'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=15) as response:
                graph_data = json.loads(response.read().decode('utf-8'))
                self.calculate_upgrade_path(graph_data)
        except Exception as e:
            msg = f"Failed to fetch upgrade graph from Red Hat: {str(e)}"
            print(f"Warning: {msg}")
            self.report_data["warnings_and_events"].append(msg)

    def calculate_upgrade_path(self, graph):
        """Calculates the upgrade path using Breadth-First Search (BFS) on the DAG."""
        nodes = graph.get("nodes", [])
        edges = graph.get("edges", [])
        
        # Build adjacency list
        adj = {i: [] for i in range(len(nodes))}
        for edge in edges:
            src, dest = edge[0], edge[1]
            adj[src].append(dest)

        curr_ver = self.report_data["current_version"]
        target_ver = self.target_version

        # Find node indexes
        curr_idx = -1
        target_idx = -1
        for idx, node in enumerate(nodes):
            if node == curr_ver:
                curr_idx = idx
            if node == target_ver:
                target_idx = idx

        if curr_idx == -1:
            print(f"Current version '{curr_ver}' not found in target upgrade channel.")
            return
        if target_idx == -1:
            print(f"Target version '{target_ver}' not found in target upgrade channel.")
            return

        # Run BFS
        queue = [[curr_idx]]
        visited = {curr_idx}
        path_found = None

        while queue:
            path = queue.pop(0)
            node = path[-1]
            if node == target_idx:
                path_found = path
                break
            for neighbor in adj[node]:
                if neighbor not in visited:
                    visited.add(neighbor)
                    new_path = list(path)
                    new_path.append(neighbor)
                    queue.append(new_path)

        if path_found:
            version_path = [nodes[i] for i in path_found]
            self.report_data["upgrade_path"] = version_path
            print(f"Valid upgrade path found: {' -> '.join(version_path)}")
        else:
            self.report_data["warnings_and_events"].append(
                f"No direct upgrade path found in channel '{self.cluster_channel}' from {curr_ver} to {target_ver}. Sequential channel hops or intermediate minor version updates may be required."
            )

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
        
        version_res = self.run_cmd("oc version -o json")
        if version_res["success"]:
            try:
                version_data = json.loads(version_res["output"])
                self.report_data["current_version"] = version_data.get("openshiftVersion", "Unknown")
            except:
                pass
        
        # Read Channel and Arch
        channel_res = self.run_cmd("oc get clusterversion version -o jsonpath='{.spec.channel}'")
        if channel_res["success"] and channel_res["output"]:
            self.cluster_channel = channel_res["output"].strip()

        arch_res = self.run_cmd("oc get nodes -o jsonpath='{.items[0].metadata.labels.kubernetes\\.io/arch}'")
        if arch_res["success"] and arch_res["output"]:
            self.cluster_arch = arch_res["output"].strip()
            
        self.run_cmd(f"oc get co -o json > {self.output_dir}/co.json")
        self.run_cmd(f"oc get mcp -o json > {self.output_dir}/mcp.json")
        self.run_cmd(f"oc get nodes -o json > {self.output_dir}/nodes.json")
        self.run_cmd(f"oc get csv -A -o json > {self.output_dir}/csv.json")
        
        # etcd status
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
            co_dir = os.path.join(self.output_dir, "cluster-scoped-resources", "config.openshift.io", "clusteroperators")
            if not os.path.exists(co_dir):
                co_file = os.path.join(self.output_dir, "cluster-scoped-resources", "clusteroperators.yaml")
                if os.path.exists(co_file):
                    self._parse_co_yaml(co_file)
                return
            for filename in os.listdir(co_dir):
                if filename.endswith(".yaml"):
                    self._parse_co_yaml(os.path.join(co_dir, filename))
            return

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

    def analyze_addon_operators(self):
        print("Analyzing Addon CSV Operators & Compatibility matrices...")
        csvs = []
        if self.is_must_gather:
            ns_dir = os.path.join(self.output_dir, "namespaces")
            if os.path.exists(ns_dir):
                for ns in os.listdir(ns_dir):
                    csv_path = os.path.join(ns_dir, ns, "operators.coreos.com", "clusterserviceversions")
                    if os.path.exists(csv_path):
                        for filename in os.listdir(csv_path):
                            if filename.endswith(".yaml"):
                                data = self.load_yaml(os.path.join(csv_path, filename))
                                if data:
                                    csvs.append({
                                        "name": data.get("metadata", {}).get("name", "Unknown"),
                                        "namespace": ns,
                                        "phase": data.get("status", {}).get("phase", "Unknown"),
                                        "raw_data": data
                                    })
        else:
            path = f"{self.output_dir}/csv.json"
            if os.path.exists(path):
                with open(path, "r") as f:
                    try:
                        data = json.load(f)
                        items = data.get("items", []) if isinstance(data, dict) else data
                        for item in items:
                            csvs.append({
                                "name": item["metadata"]["name"],
                                "namespace": item["metadata"]["namespace"],
                                "phase": item.get("status", {}).get("phase", "Unknown"),
                                "raw_data": item
                            })
                    except:
                        pass

        self.report_data["addon_operators"] = csvs
        target_ocp_minor = ".".join(self.target_version.split(".")[:2])
        target_ocp_float = float(target_ocp_minor)

        for csv in csvs:
            csv_name = csv["name"]
            raw_csv = csv["raw_data"]
            
            # --- DYNAMIC CHECK: OLM MaxOpenShiftVersion Annotation / Properties ---
            max_ver = None
            annotations = raw_csv.get("metadata", {}).get("annotations", {})
            if annotations:
                max_ver = annotations.get("olm.maxOpenShiftVersion")
                properties_str = annotations.get("olm.properties")
                if properties_str:
                    try:
                        properties = json.loads(properties_str)
                        for prop in properties:
                            if prop.get("type") == "olm.maxOpenShiftVersion":
                                max_ver = prop.get("value")
                                break
                    except:
                        pass

            if max_ver:
                try:
                    max_float = float(max_ver)
                    if target_ocp_float > max_float:
                        issue = {
                            "operator": csv_name,
                            "installed_version": "OLM Dynamic Check",
                            "target_ocp_version": target_ocp_minor,
                            "compatible": False,
                            "recommended_operator_version": f"A version declaring compatibility higher than {max_ver}"
                        }
                        self.report_data["operator_compatibility_issues"].append(issue)
                        self.report_data["overall_status"] = "FAIL"
                        self.report_data["errors"].append(
                            f"Dynamic Blocker: Operator '{csv_name}' restricts OCP upgrades past version {max_ver} (olm.maxOpenShiftVersion). "
                            f"Recommendation: Upgrade this operator before cluster upgrade."
                        )
                        continue
                except ValueError:
                    pass

            # --- STATIC CHECK FALLBACK ---
            package_name = None
            version_str = None
            for key in self.COMPATIBILITY_MATRICES.keys():
                if key in csv_name:
                    package_name = key
                    ver_match = re.search(r"v?(\d+\.\d+)", csv_name)
                    if ver_match:
                        version_str = ver_match.group(1)
                    break
            
            if package_name and version_str:
                matrix = self.COMPATIBILITY_MATRICES[package_name]
                compatible_ocp_versions = matrix.get(version_str, [])
                if target_ocp_minor not in compatible_ocp_versions:
                    recommended_version = "Unknown"
                    for op_ver, ocp_vers in matrix.items():
                        if target_ocp_minor in ocp_vers:
                            recommended_version = op_ver
                            break
                            
                    issue = {
                        "operator": package_name,
                        "installed_csv": csv_name,
                        "installed_version": version_str,
                        "target_ocp_version": target_ocp_minor,
                        "compatible": False,
                        "recommended_operator_version": recommended_version
                    }
                    if not any(iss["operator"] == package_name for iss in self.report_data["operator_compatibility_issues"]):
                        self.report_data["operator_compatibility_issues"].append(issue)
                        self.report_data["overall_status"] = "FAIL"
                        self.report_data["errors"].append(
                            f"Matrix Blocker: {package_name} v{version_str} is incompatible with target OCP {target_ocp_minor}. "
                            f"Recommendation: Upgrade {package_name} to version {recommended_version} prior to OCP upgrade."
                        )

    def analyze_deprecated_apis(self):
        if self.is_must_gather:
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
        try:
            v_path = os.path.join(self.output_dir, "cluster-scoped-resources", "config.openshift.io", "clusterversions", "cluster.yaml")
            if not os.path.exists(v_path):
                v_path = os.path.join(self.output_dir, "cluster-scoped-resources", "clusterversion.yaml")
            data = self.load_yaml(v_path)
            if data:
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

## Upgrade Graph Path Verification
"""
        if self.report_data["upgrade_path"]:
            path_str = " $\\rightarrow$ ".join([f"`{v}`" for v in self.report_data["upgrade_path"]])
            md += f"✅ **Valid Upgrade Path Found:**\n\n{path_str}\n"
        else:
            md += "❌ **No Direct/Valid path found in upstream update graph.** Check updates configuration or channel hops.\n"

        md += """

## Must-Gather Analysis Report

### 1. Add-on Operator Compatibility Matrix Checks
The planner cross-referenced your OLM operators against OpenShift target version compatibility matrices (including dynamic checks for OLM `olm.maxOpenShiftVersion` boundaries):
"""
        if self.report_data["operator_compatibility_issues"]:
            md += "| Operator / CSV | Installed Version | Target OCP | Compatibility | Recommendation |\n|---|---|---|---|---|\n"
            for issue in self.report_data["operator_compatibility_issues"]:
                md += f"| `{issue['operator']}` | `{issue['installed_version']}` | `{issue['target_ocp_version']}` | 🔴 Incompatible | {issue['recommended_operator_version']} before cluster upgrade |\n"
        else:
            md += "*All detected addon operators are compatible with the target version.*"

        md += """

### 2. Core Cluster Operators Status
| Operator Name | Available | Progressing | Degraded | Status |
|---|---|---|---|---|
"""
        for co in self.report_data["cluster_operators"]:
            co_status = "🟢 OK" if co["status_ok"] else "🔴 DEGRADED"
            md += f"| `{co['name']}` | `{co['available']}` | `{co['progressing']}` | `{co['degraded']}` | {co_status} |\n"
            
        md += """
### 3. Machine Config Pools (MCPs)
| MCP Name | Paused | Degraded | Status |
|---|---|---|---|
"""
        for mcp in self.report_data["machine_config_pools"]:
            mcp_status = "🔴 DEGRADED" if mcp["degraded"] else ("⚠️ PAUSED" if mcp["paused"] else "🟢 OK")
            md += f"| `{mcp['name']}` | `{mcp['paused']}` | `{mcp['degraded']}` | {mcp_status} |\n"

        md += """
### 4. Node Health
| Node Name | Role | Ready | Schedulable |
|---|---|---|---|
"""
        for node in self.report_data["nodes"]:
            md += f"| `{node['name']}` | `{node['role']}` | `{node['ready']}` | `{not node['unschedulable']}` |\n"

        md += """
### 5. Pod Failures & Evictions
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
            self.ask_credentials_and_proxy()
            self.collect_redhat_support_dumps()
            self.run_must_gather_command_generation()
            self.execute_live_checks()
            self.query_upgrade_graph()
            
        self.analyze_etcd()
        self.analyze_cluster_operators()
        self.analyze_machine_config_pools()
        self.analyze_nodes()
        self.analyze_pods()
        self.analyze_addon_operators()
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
    parser.add_argument("--proxy", default=None, help="Proxy URL for Upgrade Graph HTTP request")
    
    args = parser.parse_args()
    planner = OpenShiftUpgradePlanner(target_version=args.target_version, output_dir=args.dir, mode=args.mode, proxy=args.proxy)
    planner.run_all()
