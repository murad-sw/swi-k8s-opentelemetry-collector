#!/usr/bin/env python3
"""
Test script for Docker Image Updater

This script provides unit tests and integration tests for the Docker Image Updater.
Run this to validate the functionality before deploying changes.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

# Add the scripts directory to the path
sys.path.insert(0, str(Path(__file__).parent))

try:
    from update_docker_images import DockerImageUpdater
except ImportError as e:
    print(f"Error importing update_docker_images: {e}")
    print("Please install dependencies with: pip install -r requirements.txt")
    sys.exit(1)


class TestDockerImageUpdater(unittest.TestCase):
    """Test cases for DockerImageUpdater class."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.github_token = "test-token"
        self.updater = DockerImageUpdater(
            github_token=self.github_token,
            dry_run=True
        )
    
    def test_version_comparison(self):
        """Test version comparison logic."""
        from packaging import version
        
        # Test semantic version parsing
        v1 = version.parse("1.2.3")
        v2 = version.parse("1.2.4")
        v3 = version.parse("v1.2.5")
        
        self.assertTrue(v2 > v1)
        self.assertTrue(v3 > v2)
        
        # Test alpha/beta versions
        va = version.parse("1.2.3-alpha.1")
        vb = version.parse("1.2.3-beta.1")
        vc = version.parse("1.2.3")
        
        self.assertTrue(vb > va)
        self.assertTrue(vc > vb)
    
    def test_image_filter(self):
        """Test image filtering logic."""
        # Test with no filter
        self.updater.image_filter = ""
        self.assertTrue(self.updater.should_update_image("any-image"))
        
        # Test with simple filter
        self.updater.image_filter = "solarwinds"
        self.assertTrue(self.updater.should_update_image("solarwinds/otel-collector"))
        self.assertFalse(self.updater.should_update_image("busybox"))
        
        # Test with regex filter
        self.updater.image_filter = "solarwinds.*otel"
        self.assertTrue(self.updater.should_update_image("solarwinds/solarwinds-otel-collector"))
        self.assertFalse(self.updater.should_update_image("solarwinds/swo-agent"))
    
    def test_find_images_in_yaml(self):
        """Test YAML image detection."""
        test_yaml = {
            'otel': {
                'image': {
                    'repository': 'solarwinds/solarwinds-otel-collector',
                    'tag': '0.119.10'
                },
                'init_images': {
                    'busy_box': {
                        'repository': 'busybox',
                        'tag': '1.37.0'
                    }
                }
            },
            'other_section': {
                'not_an_image': 'value'
            }
        }
        
        images = self.updater.find_images_in_yaml(test_yaml)
        
        # Should find 2 images
        self.assertEqual(len(images), 2)
        
        # Check image repositories
        repositories = [img['repository'] for img in images]
        self.assertIn('solarwinds/solarwinds-otel-collector', repositories)
        self.assertIn('busybox', repositories)
    
    @patch('requests.get')
    def test_docker_hub_api(self, mock_get):
        """Test Docker Hub API integration."""
        # Mock successful API response
        mock_response = Mock()
        mock_response.json.return_value = {
            'results': [
                {'name': '1.36.0'},
                {'name': '1.37.0'},
                {'name': 'latest'}
            ],
            'next': None
        }
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response
        
        tags = self.updater.get_docker_hub_tags('busybox')
        
        self.assertEqual(len(tags), 3)
        self.assertIn('1.37.0', tags)
        self.assertIn('latest', tags)
    
    @patch('requests.get')
    def test_docker_hub_api_pagination(self, mock_get):
        """Test Docker Hub API with pagination."""
        # Mock paginated responses
        response1 = Mock()
        response1.json.return_value = {
            'results': [{'name': '1.0.0'}],
            'next': 'https://hub.docker.com/v2/repositories/test/tags?page=2'
        }
        response1.raise_for_status.return_value = None
        
        response2 = Mock()
        response2.json.return_value = {
            'results': [{'name': '2.0.0'}],
            'next': None
        }
        response2.raise_for_status.return_value = None
        
        mock_get.side_effect = [response1, response2]
        
        tags = self.updater.get_docker_hub_tags('test-repo')
        
        self.assertEqual(len(tags), 2)
        self.assertEqual(mock_get.call_count, 2)
    
    def test_registry_detection(self):
        """Test registry type detection."""
        # Docker Hub repositories
        docker_hub_repos = [
            'busybox',
            'solarwinds/solarwinds-otel-collector',
            'docker.io/busybox',
            'index.docker.io/library/busybox'
        ]
        
        # GHCR repositories  
        ghcr_repos = [
            'ghcr.io/owner/repo',
            'ghcr.io/solarwinds/test-image'
        ]
        
        # Test Docker Hub detection
        for repo in docker_hub_repos:
            with patch.object(self.updater, 'get_docker_hub_tags') as mock_docker:
                mock_docker.return_value = ['1.0.0']
                self.updater.get_latest_version(repo)
                mock_docker.assert_called_once()
        
        # Test GHCR detection
        for repo in ghcr_repos:
            with patch.object(self.updater, 'get_ghcr_tags') as mock_ghcr:
                mock_ghcr.return_value = ['v1.0.0']
                self.updater.get_latest_version(repo)
                mock_ghcr.assert_called_once()
    
    def test_yaml_preservation(self):
        """Test YAML formatting preservation."""
        yaml_content = '''# Comment at top
otel:
  image:
    repository: "solarwinds/solarwinds-otel-collector"
    tag: "0.119.9"  # Old version
    pullPolicy: IfNotPresent
  
  # Another section
  endpoint: <OTEL_ENVOY_ADDRESS>
'''
        
        # Create temporary file
        with tempfile.NamedTemporaryFile(mode='w+', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name
        
        try:
            # Load and modify YAML
            from ruamel.yaml import YAML
            yaml_loader = YAML()
            yaml_loader.preserve_quotes = True
            yaml_loader.width = 4096
            
            with open(temp_path, 'r') as f:
                data = yaml_loader.load(f)
            
            # Modify tag
            data['otel']['image']['tag'] = "0.119.10"
            
            # Save back
            with open(temp_path, 'w') as f:
                yaml_loader.dump(data, f)
            
            # Check that comments and structure are preserved
            with open(temp_path, 'r') as f:
                modified_content = f.read()
            
            self.assertIn('# Comment at top', modified_content)
            self.assertIn('# Another section', modified_content)
            self.assertIn('0.119.10', modified_content)
            self.assertNotIn('0.119.9', modified_content)
        
        finally:
            os.unlink(temp_path)
    
    def test_semantic_version_filtering(self):
        """Test semantic version filtering and sorting."""
        tags = [
            'latest', 'main', 'dev',  # Non-semantic versions
            '1.0.0', '1.1.0', '1.2.0',  # Standard versions
            'v2.0.0', 'v2.1.0',  # Versions with 'v' prefix
            '1.2.3-alpha.1', '1.2.3-beta.1', '1.2.3-rc.1',  # Pre-release versions
            '1.0.0-SNAPSHOT', '1.0.0-20231201'  # Invalid semantic versions
        ]
        
        # Mock tags response
        with patch.object(self.updater, 'get_docker_hub_tags') as mock_get_tags:
            mock_get_tags.return_value = tags
            
            latest = self.updater.get_latest_version('test-repo', '1.0.0')
            
            # Should get the highest version
            self.assertEqual(latest, 'v2.1.0')
    
    def test_current_version_comparison(self):
        """Test current version comparison logic."""
        tags = ['1.0.0', '1.1.0', '2.0.0']
        
        with patch.object(self.updater, 'get_docker_hub_tags') as mock_get_tags:
            mock_get_tags.return_value = tags
            
            # Test no update needed
            result = self.updater.get_latest_version('test-repo', '2.0.0')
            self.assertIsNone(result)  # Already up to date
            
            # Test update needed
            result = self.updater.get_latest_version('test-repo', '1.0.0')
            self.assertEqual(result, '2.0.0')
            
            # Test force update
            self.updater.force_update = True
            result = self.updater.get_latest_version('test-repo', '2.0.0')
            self.assertEqual(result, '2.0.0')  # Should return even if same version


class TestIntegration(unittest.TestCase):
    """Integration tests that require actual API calls."""
    
    @unittest.skipUnless(os.environ.get('RUN_INTEGRATION_TESTS'), 
                        "Integration tests skipped (set RUN_INTEGRATION_TESTS=1 to run)")
    def test_real_docker_hub_api(self):
        """Test real Docker Hub API call."""
        updater = DockerImageUpdater("fake-token", dry_run=True)
        tags = updater.get_docker_hub_tags('busybox')
        
        self.assertIsInstance(tags, list)
        self.assertGreater(len(tags), 0)
        self.assertIn('latest', tags)
    
    @unittest.skipUnless(os.environ.get('GITHUB_TOKEN'), 
                        "GHCR tests skipped (set GITHUB_TOKEN to run)")
    def test_real_ghcr_api(self):
        """Test real GHCR API call."""
        github_token = os.environ.get('GITHUB_TOKEN')
        updater = DockerImageUpdater(github_token, dry_run=True)
        
        # Test with a known public GHCR image
        tags = updater.get_ghcr_tags('ghcr.io/actions/runner')
        
        self.assertIsInstance(tags, list)
        # May be empty if no public tags available


def run_tests():
    """Run all tests with proper reporting."""
    print("🧪 Running Docker Image Updater Tests\n")
    
    # Create test suite
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    
    # Add test cases
    suite.addTests(loader.loadTestsFromTestCase(TestDockerImageUpdater))
    suite.addTests(loader.loadTestsFromTestCase(TestIntegration))
    
    # Run tests
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    
    # Report results
    print(f"\n📊 Test Results:")
    print(f"   Tests run: {result.testsRun}")
    print(f"   Failures: {len(result.failures)}")
    print(f"   Errors: {len(result.errors)}")
    print(f"   Success rate: {((result.testsRun - len(result.failures) - len(result.errors)) / result.testsRun * 100):.1f}%")
    
    if result.failures:
        print(f"\n❌ Failures:")
        for test, traceback in result.failures:
            print(f"   - {test}: {traceback}")
    
    if result.errors:
        print(f"\n💥 Errors:")
        for test, traceback in result.errors:
            print(f"   - {test}: {traceback}")
    
    return result.wasSuccessful()


if __name__ == '__main__':
    success = run_tests()
    sys.exit(0 if success else 1)
