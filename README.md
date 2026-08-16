# OpenShift Upgrade Planner Tool

This project provides an automated tool and documentation to run pre-upgrade validation checks on a Red Hat OpenShift Container Platform (OCP) 4.x cluster before performing an upgrade.

## Structure
1. [`openshift_upgrade_plan.md`](openshift_upgrade_plan.md): The detailed, production-grade checklist and manual planning guide.
2. [`openshift_upgrade_planner.py`](openshift_upgrade_planner.py): An automated Python script that queries your live cluster via `oc` to check prerequisites and generates a markdown report.

## Requirements
* `python3` installed.
* `oc` CLI configured and logged into the target OpenShift cluster.

## Usage

### 1. Live Mode (Requires active `oc` connection)
This mode queries the live cluster, dumps all logs/files to `/tmp/UPGRADE`, and runs the validation logic.
```bash
python3 openshift_upgrade_planner.py 4.13.10 --mode live
```

### 2. Offline Mode (Analyzes local data files)
If the cluster is disconnected or you are running this on a developer machine with downloaded files, copy the diagnostic files to a directory (e.g. `./my-dumps/` or `/tmp/UPGRADE/`) and run:
```bash
python3 openshift_upgrade_planner.py 4.13.10 --mode offline --dir ./my-dumps
```

This will parse the existing data files (such as `pods-all.out`, `certs2.out`, `etcd-status.out`, `apirequest-removedInRelease_count.out`) and write the results to `upgrade_readiness_report.md` without making any API calls.
