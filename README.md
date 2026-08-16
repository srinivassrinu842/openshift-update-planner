# OpenShift Upgrade Planner Tool

This project provides an automated tool and documentation to run pre-upgrade validation checks on a Red Hat OpenShift Container Platform (OCP) 4.x cluster before performing an upgrade.

## Structure
1. [`openshift_upgrade_plan.md`](openshift_upgrade_plan.md): The detailed, production-grade checklist and manual planning guide.
2. [`openshift_upgrade_planner.py`](openshift_upgrade_planner.py): An automated Python script that queries your live cluster via `oc` to check prerequisites and generates a markdown report.

## Requirements
* `python3` installed.
* `oc` CLI configured and logged into the target OpenShift cluster.

## Usage
Run the planner script by passing your desired target version:

```bash
python3 openshift_upgrade_planner.py <target_version>
```

Example:
```bash
python3 openshift_upgrade_planner.py 4.13.10
```

This will run all checks against your cluster and write the results to a detailed markdown report: `upgrade_readiness_report.md`.
