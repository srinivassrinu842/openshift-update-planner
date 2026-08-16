import unittest
from openshift_upgrade_planner import OpenShiftUpgradePlanner

class TestOpenShiftUpgradePlanner(unittest.TestCase):
    def setUp(self):
        self.planner = OpenShiftUpgradePlanner(target_version="4.13.10", output_dir="/tmp/UPGRADE-TEST", mode="offline")

    def test_fallback_yaml_parse(self):
        test_yaml = """
apiVersion: operators.coreos.com/v1alpha1
kind: ClusterServiceVersion
metadata:
  name: ocs-operator.v4.12.5
  annotations:
    olm.maxOpenShiftVersion: "4.12"
status:
  phase: Succeeded
"""
        parsed = self.planner._fallback_yaml_parse(test_yaml)
        self.assertEqual(parsed["metadata"]["name"], "ocs-operator.v4.12.5")
        self.assertEqual(parsed["metadata"]["annotations"]["olm.maxOpenShiftVersion"], "4.12")
        self.assertEqual(parsed["status"]["phase"], "Succeeded")

    def test_calculate_upgrade_path(self):
        # Mock graph structure
        graph = {
            "nodes": ["4.12.0", "4.12.10", "4.12.20", "4.13.0", "4.13.5", "4.13.10"],
            "edges": [
                [0, 1],
                [1, 2],
                [2, 3],
                [3, 4],
                [4, 5]
            ]
        }
        self.planner.report_data["current_version"] = "4.12.10"
        self.planner.calculate_upgrade_path(graph)
        self.assertEqual(self.planner.report_data["upgrade_path"], ["4.12.10", "4.12.20", "4.13.0", "4.13.5", "4.13.10"])

if __name__ == "__main__":
    unittest.main()
