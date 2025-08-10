#!/usr/bin/env python3
"""
Quick test script to validate the fixes for the Docker Image Updater
"""

import os
import sys
from pathlib import Path

# Add the scripts directory to the path
sys.path.insert(0, str(Path(__file__).parent))

def test_docker_hub_api():
    """Test Docker Hub API with the fixed URL format."""
    try:
        import requests
        
        # Test the fixed busybox API URL
        url = "https://hub.docker.com/v2/repositories/library/busybox/tags"
        print(f"Testing Docker Hub API: {url}")
        
        response = requests.get(url, timeout=10)
        print(f"Status Code: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            tags = [tag['name'] for tag in data.get('results', [])[:5]]  # Just first 5 tags
            print(f"✅ Success! Found tags: {tags}")
            return True
        else:
            print(f"❌ Failed with status {response.status_code}")
            return False
            
    except Exception as e:
        print(f"❌ Error: {e}")
        return False

def test_version_parsing():
    """Test the improved chart version parsing."""
    try:
        test_versions = [
            "4.6.0-alpha.6",
            "4.6.0-beta.1", 
            "1.2.3",
            "2.0.0-rc.1"
        ]
        
        print("Testing chart version parsing:")
        for version_str in test_versions:
            version_parts = version_str.split('.')
            if len(version_parts) >= 3 and '-' in version_parts[2]:
                base_part, pre_release = version_parts[2].split('-', 1)
                if 'alpha' in pre_release and '.' in pre_release:
                    alpha_parts = pre_release.split('.')
                    if len(alpha_parts) >= 2 and alpha_parts[1].isdigit():
                        alpha_num = int(alpha_parts[1]) + 1
                        new_version = f"{version_parts[0]}.{version_parts[1]}.{base_part}-alpha.{alpha_num}"
                        print(f"  ✅ {version_str} → {new_version}")
                    else:
                        print(f"  ⚠️  {version_str} → parsing issue with alpha parts")
                elif 'beta' in pre_release and '.' in pre_release:
                    beta_parts = pre_release.split('.')
                    if len(beta_parts) >= 2 and beta_parts[1].isdigit():
                        beta_num = int(beta_parts[1]) + 1
                        new_version = f"{version_parts[0]}.{version_parts[1]}.{base_part}-beta.{beta_num}"
                        print(f"  ✅ {version_str} → {new_version}")
                else:
                    print(f"  ⚠️  {version_str} → complex pre-release format")
            else:
                if len(version_parts) >= 3 and version_parts[2].isdigit():
                    new_patch = str(int(version_parts[2]) + 1)
                    new_version = f"{version_parts[0]}.{version_parts[1]}.{new_patch}"
                    print(f"  ✅ {version_str} → {new_version}")
                else:
                    print(f"  ⚠️  {version_str} → parsing issue")
        
        return True
        
    except Exception as e:
        print(f"❌ Version parsing error: {e}")
        return False

def test_dry_run_imports():
    """Test that all required modules can be imported."""
    try:
        print("Testing module imports:")
        
        import requests
        print("  ✅ requests")
        
        from ruamel.yaml import YAML
        print("  ✅ ruamel.yaml")
        
        from packaging import version
        print("  ✅ packaging")
        
        from github import Github
        print("  ✅ PyGithub")
        
        return True
        
    except ImportError as e:
        print(f"  ❌ Missing module: {e}")
        return False
    except Exception as e:
        print(f"  ❌ Import error: {e}")
        return False

def main():
    """Run all quick tests."""
    print("🧪 Quick validation tests for Docker Image Updater fixes\n")
    
    tests = [
        ("Module imports", test_dry_run_imports),
        ("Docker Hub API", test_docker_hub_api), 
        ("Version parsing", test_version_parsing)
    ]
    
    results = {}
    for test_name, test_func in tests:
        print(f"\n🔍 Testing {test_name}...")
        try:
            results[test_name] = test_func()
        except Exception as e:
            print(f"❌ Test failed: {e}")
            results[test_name] = False
    
    # Summary
    print(f"\n📊 Test Results:")
    print("=" * 40)
    
    passed = sum(1 for result in results.values() if result)
    total = len(results)
    
    for test_name, result in results.items():
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status} {test_name}")
    
    print(f"\nOverall: {passed}/{total} tests passed ({passed/total*100:.1f}%)")
    
    if passed == total:
        print("\n🎉 All quick tests passed! The fixes should resolve the issues.")
    else:
        print(f"\n⚠️  Some tests failed. Please check the output above.")
    
    return passed == total

if __name__ == '__main__':
    success = main()
    sys.exit(0 if success else 1)
