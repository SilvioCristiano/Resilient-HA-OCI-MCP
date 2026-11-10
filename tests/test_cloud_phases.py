"""Regional cache provisioning must reject an invalid dependency before OCI writes."""
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('cloud_phases', SCRIPTS / 'cloud.py')
cloud = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud)


class CachePhaseTests(unittest.TestCase):
    def test_secondary_without_confirmed_primary_does_not_call_oci(self):
        for observed in ({}, {'cache': {cloud.REGIONS[0]: {'id': 'pending-primary'}}}):
            with self.subTest(observed=observed), patch.object(cloud, 'state', return_value=observed), patch.object(cloud, 'client') as client:
                with self.assertRaisesRegex(ValueError, 'Sao Paulo'):
                    cloud.cache({}, (cloud.REGIONS[1],))
                client.assert_not_called()
