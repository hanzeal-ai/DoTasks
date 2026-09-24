from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class GithubDeployWorkflowTest(unittest.TestCase):
    def test_hosted_build_and_guarded_ssh_deployment(self):
        source = (ROOT / '.github/workflows/deploy-cloud.yml').read_text()
        self.assertIn('branches:\n      - main', source)
        self.assertIn('./scripts/test', source)
        self.assertIn('platforms: linux/amd64', source)
        self.assertIn('tags: dotasks-cloud:${{ github.sha }}', source)
        self.assertIn("vars.DEPLOY_ENABLED == 'true'", source)
        self.assertIn("github.ref == 'refs/heads/main'", source)
        self.assertIn('deployment/send.sh site', source)
        self.assertNotIn('self-hosted', source)
        self.assertNotIn('id-token: write', source)
        self.assertNotIn('ACR_PASSWORD', source)
        self.assertIn('DEPLOY_KNOWN_HOSTS', source)
        self.assertIn('needs: build', source)


if __name__ == '__main__':
    unittest.main()
