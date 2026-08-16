# Red Hat OpenShift Container Platform (OCP) 4.x - Production-Grade Pre-Upgrade Checklist & Planner

This document provides a comprehensive, production-grade checklist and planning guide for upgrading Red Hat OpenShift Container Platform (OCP) 4.x clusters. These checks are compiled directly from Red Hat official documentation and best practices to ensure zero downtime and minimal risk during cluster upgrades.

---

## Table of Contents
1. [Upgrade Strategy & Parameters](#1-upgrade-strategy--parameters)
2. [Check 1: etcd Cluster Health & Pre-Upgrade Backups](#check-1-etcd-cluster-health--pre-upgrade-backups)
3. [Check 2: Cluster Operators Health & API Compatibility](#check-2-cluster-operators-health--api-compatibility)
4. [Check 3: MachineConfigPools (MCPs) Verification](#check-3-machineconfigpools-mcps-verification)
5. [Check 4: Node Health, Capacity & Schedulability](#check-4-node-health-capacity--schedulability)
6. [Check 5: Add-on & Installed Operators Compatibility](#check-5-add-on--installed-operators-compatibility)
7. [Check 6: Alerts, Events, and Diagnostic Logs](#check-6-alerts-events-and-diagnostic-logs)
8. [Post-Verification & Monitoring Playbook](#post-verification--monitoring-playbook)

---

## 1. Upgrade Strategy & Parameters

Before running any CLI commands, define the parameters of the upgrade. These parameters are crucial to verify path compatibility and minimize workload impact.

### Key Upgrade Parameters
* **Current Version:** `oc version` (e.g., `4.12.15`)
* **Target Version:** The destination version (e.g., `4.13.10`)
* **Upgrade Path Compatibility:** Red Hat OpenShift Update Graph must validate the path. Check compatibility via the [Red Hat OpenShift Upgrade Graph Visualizer](https://access.redhat.com/labs/ocpup/).
* **Update Channel:** E.g., `stable-4.13`, `fast-4.13`, or `candidate-4.13`.
* **Signature Verification Policy:** Ensure the cluster can fetch the update release signatures (requires internet access or mirrors configured for disconnected environments).
* **Maintenance Window Duration:** Allow at least 2 to 4 hours depending on the cluster size and node count.

---

## Check 1: etcd Cluster Health & Pre-Upgrade Backups

`etcd` is the source of truth for the entire OpenShift cluster. An unhealthy etcd state before an upgrade can cause catastrophic cluster failure.

### 1.1 Run etcd Member List and Endpoint Health
Verify that all etcd members are online, healthy, and communicating with low raft latency.

```bash
# 1. Get the list of running etcd pods
oc get pods -n openshift-etcd -l app=etcd

# 2. Remote shell into one of the etcd pods
ETCD_POD=$(oc get pods -n openshift-etcd -l app=etcd --field-selector="status.phase==Running" -o jsonpath='{.items[0].metadata.name}')

# 3. Check etcd member health
oc rsh -n openshift-etcd $ETCD_POD etcdctl endpoint health --cluster -w table

# 4. Check etcd cluster membership status
oc rsh -n openshift-etcd $ETCD_POD etcdctl member list -w table

# 5. Check etcd database size and fragmentation
oc rsh -n openshift-etcd $ETCD_POD etcdctl endpoint status -w table --cluster
```

* **Expected Output:** All endpoints must report `healthy: true` and `dbSize` should be within acceptable limits (usually < 8 GiB).
* **Action on Failure:** Do not proceed with the upgrade if any member reports `unhealthy`. If database size is close to the limit, perform defragmentation (`etcdctl defrag`).

### 1.2 Perform a Fresh etcd Backup
Immediately before upgrading, take an etcd backup and copy the archive off-cluster.

```bash
# 1. Start a debug session on one of the control-plane (master) nodes
oc debug node/$(oc get nodes -l node-role.kubernetes.io/master= -o jsonpath='{.items[0].metadata.name}') --as-root

# 2. Inside the debug container, run the cluster-backup.sh script
chroot /host /usr/local/bin/cluster-backup.sh /home/core/assets/backup/

# 3. Exit the debug shell and copy the backup out of the node to a secure external location
oc cp -n openshift-etcd <debug-pod-name>:/host/home/core/assets/backup/ <local-backup-dir>/
```
> [!IMPORTANT]
> A backup is your only safety net. Without an off-cluster copy of this backup, restoring a failed control-plane upgrade is extremely difficult.

---

## Check 2: Cluster Operators Health & API Compatibility

Core cluster operators manage all control plane components. Every operator must be stable and fully functional before starting.

### 2.1 Verify Cluster Operator Status
Ensure that no operators are degraded, unavailable, or currently progressing.

```bash
oc get co
```
* **Expected Output:**
  * `AVAILABLE` = `True`
  * `PROGRESSING` = `False`
  * `DEGRADED` = `False`
* **Command to pinpoint degraded operators:**
  ```bash
  oc get co | grep -v "True.*False.*False"
  ```

### 2.2 Verify API Request Counts (Deprecated APIs)
Ensure that workloads are not utilizing APIs that are removed or deprecated in the target version.

```bash
# List API versions that have active requests
oc get apirequestcounts
```
Review the list for any API that is scheduled for removal in your target version (refer to Red Hat documentation for removed APIs for your target release, e.g., removal of `v1beta1` APIs).

---

## Check 3: MachineConfigPools (MCPs) Verification

The Machine Config Operator (MCO) manages node operating system updates and machine configuration changes.

### 3.1 Check Machine Config Pool Health
```bash
oc get mcp
```
* **Expected Output:**
  * `UPDATED` = `True`
  * `UPDATING` = `False`
  * `DEGRADED` = `False`
  * `DEGRADEDMACHINECOUNT` = `0`
  * `MACHINECOUNT` must equal `READYMACHINECOUNT`

```bash
# To check if any MachineConfigPool is paused
oc get machineconfigpool -o custom-columns=NAME:.metadata.name,PAUSED:.spec.paused
```
> [!WARNING]
> **Paused Pools:** If a MachineConfigPool is paused (`spec.paused: true`), the nodes in that pool will **not** be upgraded. While useful for canary deployments, keeping them paused for long periods blocks critical security certificate rotations. Unpause before major upgrades unless executing a specific canary rollout strategy.

```bash
# To unpause a pool if required:
oc patch machineconfigpool/<mcp-name> --type=merge --patch='{"spec":{"paused":false}}'
```

---

## Check 4: Node Health, Capacity & Schedulability

Nodes will be rebooted one-by-one during the upgrade. The cluster must have enough capacity to reschedule workloads while nodes are being drained.

### 4.1 Node Readiness & Scheduling Status
Ensure all nodes are `Ready` and schedulable.

```bash
# List node status
oc get nodes

# Check for Unschedulable nodes (SchedulingDisabled)
oc get nodes | grep SchedulingDisabled
```
* **Expected Status:** All master and worker nodes must be in the `Ready` state. If any node is in `Ready,SchedulingDisabled`, investigate why it was cordoned.

### 4.2 Resource Allocatable Checks
Since nodes are drained during updates, remaining nodes must have sufficient resource buffer.

```bash
# Describe nodes to inspect "Allocatable" vs "Capacity"
oc describe nodes | egrep "Name:|Allocatable:|Capacity:|Resource.*Requests.*Limits"
```
Ensure that no nodes have CPU or memory usage near 100% capacity. If they do, worker node drainage will fail due to scheduling constraints.

### 4.3 Machine Health Checks (MHC)
Active MachineHealthChecks might trigger node replacements when nodes reboot during upgrades.

```bash
# List all active MachineHealthChecks
oc get mhc -n openshift-machine-api

# (Optional) Pause MHC during the maintenance window to prevent premature node replacements
oc -n openshift-machine-api annotate mhc/<mhc-name> cluster.x-k8s.io/paused="" --overwrite
```
*Note: Remember to unpause them after the upgrade is complete.*

---

## Check 5: Add-on & Installed Operators Compatibility

OLM (Operator Lifecycle Manager) manages add-on operators. These must be checked against the target OpenShift version.

### 5.1 Check OLM Operator Status
```bash
# List all CSVs (ClusterServiceVersions) and their phases
oc get csv -A
```
* **Expected Status:** All CSVs must be in the `Succeeded` phase. Any operator in `Failed` or `Replacing` state must be resolved.

### 5.2 Validate Version Compatibility
Cross-reference the compatibility of critical Red Hat and third-party operators:
* **OpenShift Data Foundation (ODF)**
* **OpenShift Virtualization**
* **Advanced Cluster Management (ACM)**
* **Red Hat OpenShift GitOps / Pipelines**

Refer to the official Red Hat Operator Compatibility Matrix to ensure the installed operator versions support the destination OCP minor version. Update operators *before* the cluster upgrade if required by the compatibility matrix.

---

## Check 6: Alerts, Events, and Diagnostic Logs

Analyzing logs and alerting profiles helps detect silent failures that could disrupt an upgrade.

### 6.1 Check Active Prometheus Alerts
Run this command or check the Web Console (**Observe > Alerting**) for firing alerts:

```bash
# Get firing alerts (requires port-forward or routing access, or query Prometheus directly)
# You can check for any critical alerts in the cluster:
oc get alerts -n openshift-monitoring
```
Ensure no `Critical` alerts (e.g., `etcdMembersDown`, `KubeletDown`, `TargetDown`) are firing.

### 6.2 Review Warning Events
Inspect cluster-wide warning events from the last 1-2 hours:

```bash
oc get events -A --field-selector type=Warning | sort -k 2
```
Look for recurring `FailedMount`, `DiskPressure`, `MemoryPressure`, or `BackOff` errors.

### 6.3 Standard Red Hat Support Pre-Upgrade Diagnostics
When opening a ticket, Red Hat Support requires a standard diagnostic bundle and status analysis. Run the following commands to export files to `/tmp/UPGRADE/` for upload:

```bash
# 1. Create target directory
mkdir -p /tmp/UPGRADE/

# 2. Collect must-gather for cluster storage (OCS/ODF) and virtualization (CNV)
odf_mg=$(oc -n openshift-storage get deployment.apps/ocs-operator -o jsonpath='{.metadata.ownerReferences[0].name}')
oc adm must-gather --image=$(oc -n openshift-storage get csv/$odf_mg -o json | jq '.spec.relatedImages[] | select (.name | contains ("must-gather")) | .image' | sed 's/\"//g') --image-stream=openshift/must-gather --image=registry.redhat.io/container-native-virtualization/cnv-must-gather-rhel9:v4.18-1784294790 --dest-dir=/tmp/UPGRADE/must-gather-data

# 3. Dump cluster-info
oc cluster-info dump > /tmp/UPGRADE/cluster-info.out

# 4. Dump resource configurations, pods, subscriptions, and events
oc get all -A > /tmp/UPGRADE/resource-all.out
oc get pod -A > /tmp/UPGRADE/pods-all.out
oc get subs -A > /tmp/UPGRADE/subs-all.out
oc get events -A > /tmp/UPGRADE/events-all.out

# 5. Node Descriptions
for NODE in $(oc get nodes -o go-template='{{range .items}}{{.metadata.name}}{{"\n"}}{{end}}'); do 
  oc describe node $NODE > /tmp/UPGRADE/$NODE.info
done

# 6. API Request Counts (Deprecated API Check)
oc get apirequestcounts > /tmp/UPGRADE/apirequestcounts.out

# Trace User-Agents and users calling deprecated APIs
oc get apirequestcounts -o jsonpath='{range .items[?(@.status.removedInRelease!="")]}{.metadata.name}{"\n"}{end}' | xargs -I {} sh -c 'echo "\n==> $1\n" && oc get apirequestcount $1 -o yaml | grep -E "username:|userAgent:" | sort | uniq' sh {} > /tmp/UPGRADE/apirequest-userAgent.out

# Request count per removed release version
oc get apirequestcounts -o jsonpath='{range .items[?(@.status.removedInRelease!="")]}{.status.removedInRelease}{"\t"}{.status.requestCount}{"\t"}{.metadata.name}{"\n"}{end}' > /tmp/UPGRADE/apirequest-removedInRelease_count.out

# 7. etcd Deep Status
oc exec -n openshift-etcd $(oc get pods -n openshift-etcd -l app=etcd --field-selector="status.phase==Running" -o jsonpath="{.items[0].metadata.name}") -c etcdctl -- bash -c "etcdctl member list -w table;etcdctl endpoint health --cluster -w table;etcdctl endpoint status --cluster -w table;etcdctl alarm list" > /tmp/UPGRADE/etcd-status.out

# 8. Certificate Expiry and Issuer Inspection
oc get secrets -A -o json | jq -r '.items | sort_by(.metadata.namespace,.metadata.name) |.[] |select((.type == "kubernetes.io/tls") or (.type == "SecretTypeTLS"))| "\(.metadata.namespace) \(.metadata.name) \(.data | to_entries[] | select(.key | test("key") or test("Key") | not)| .value)"' | while read namespace name cert; do 
  echo -e "\n${namespace} - ${name}\n##################################################"
  echo $cert | base64 -d | openssl crl2pkcs7 -nocrl -certfile /dev/stdin | openssl pkcs7 -print_certs -text -noout | grep -A4 Issuer:
done > /tmp/UPGRADE/certs.out

(echo -e "NAMESPACE\tNAME\tEXPIRY" && oc get secrets -A -o go-template='{{range .items}}{{if eq .type "kubernetes.io/tls"}}{{.metadata.namespace}}{{" "}}{{.metadata.name}}{{" "}}{{index .data "tls.crt"}}{{"\n"}}{{end}}{{end}}' | while read namespace name cert; do 
  echo -en "$namespace\t$name\t"
  echo $cert | base64 -d | openssl x509 -noout -enddate
done ) | column -t > /tmp/UPGRADE/certs2.out
```

### 6.4 How to Analyze Diagnostic Dumps
* **etcd-status.out:** Ensure no active alarms exist (like `NOSPACE` or `CORRUPT`) and check that endpoint roundtrip latencies are within healthy thresholds (< 100ms).
* **apirequest-removedInRelease_count.out:** Inspect any API that lists the target version in the `removedInRelease` column. If request counts are greater than 0, block the upgrade and migrate those clients.
* **certs2.out:** Ensure no active certificates in `openshift-*` namespaces are expiring in less than 30 days.

---

## Post-Verification & Monitoring Playbook

Once the upgrade is initiated with `oc adm upgrade --to=<version>`, monitor progress using the following tools:

### Monitor the Upgrade Status
```bash
# Enable the upgrade status command environment variable
export OC_ENABLE_CMD_UPGRADE_STATUS=true

# Check the overall upgrade progress
oc adm upgrade status

# Monitor cluster operator progress during upgrade
oc get co -w
```

### Verification Actions
1. **Verify New Version:** `oc get clusterversion`
2. **Verify Nodes Operating System:** `oc get nodes -o wide` (ensure kernel and OS versions are updated).
3. **Verify All MCPs are Updated:** `oc get mcp` (Ensure all pools report `UPDATED=True` and `UPDATING=False`).
4. **Re-enable MachineHealthChecks:** Remove the pause annotation if it was added.
   ```bash
   oc -n openshift-machine-api annotate mhc/<mhc-name> cluster.x-k8s.io/paused-
   ```
