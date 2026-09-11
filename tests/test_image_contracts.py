import json, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]

class ImageContractTests(unittest.TestCase):
    def test_kali_and_target_are_non_root_without_host_publish(self):
        kali = (ROOT / 'images/kali-desktop/Dockerfile').read_text()
        target = (ROOT / 'images/hydra-target/Dockerfile').read_text()
        self.assertIn('USER student', kali)
        self.assertIn('USER 1000:1000', target)
        self.assertNotIn('VOLUME', kali)
        self.assertNotIn('VOLUME', target)
        self.assertIn('HEALTHCHECK', kali)
        self.assertIn('HEALTHCHECK', target)
        self.assertNotIn('--privileged', kali)
        self.assertNotIn('--privileged', target)

    def test_infra_spec_matches_worker_contract(self):
        spec = json.loads((ROOT / 'images/specs/hydra-infra-v1.json').read_text())
        try:
            from cyberpod_infra.model import validate_request
        except ImportError:
            self.skipTest('cyberpod_infra is not on PYTHONPATH')
        validate_request(spec)
        self.assertEqual({s['id'] for s in spec['services']}, {'kali', 'target'})
