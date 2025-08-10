#!/usr/bin/env python3
"""
Validation script for the Docker Image Updater setup

This script validates that the current repository structure and configuration
are compatible with the Docker Image Updater.
"""

import os
import sys
from pathlib import Path
import re

def check_file_exists(file_path, description):
    """Check if a file exists and report status."""
    if file_path.exists():
        print(f"✅ {description}: {file_path}")
        return True
    else:
        print(f"❌ {description}: {file_path} (not found)")
        return False

def validate_yaml_structure(yaml_path):
    """Validate YAML structure for image configurations."""
    try:
        from ruamel.yaml import YAML
        yaml_loader = YAML()
        
        with open(yaml_path, 'r') as f:
            data = yaml_loader.load(f)
        
        # Find image configurations
        images_found = find_images_recursive(data)
        
        print(f"📦 Found {len(images_found)} image configurations in {yaml_path}:")
        for img in images_found:
            print(f"   - {img['repository']}:{img['tag']} (at {img['path']})")
        
        return len(images_found) > 0
        
    except ImportError:
        print("⚠️  Cannot validate YAML structure (ruamel.yaml not installed)")
        return True
    except Exception as e:
        print(f"❌ Error validating YAML structure: {e}")
        return False

def find_images_recursive(data, path=""):
    """Recursively find image configurations in YAML data."""
    images = []
    
    if isinstance(data, dict):
        if 'repository' in data and 'tag' in data:
            images.append({
                'path': path,
                'repository': data['repository'],
                'tag': data['tag']
            })
        else:
            for key, value in data.items():
                new_path = f"{path}.{key}" if path else key
                images.extend(find_images_recursive(value, new_path))
    elif isinstance(data, list):
        for i, item in enumerate(data):
            new_path = f"{path}[{i}]"
            images.extend(find_images_recursive(item, new_path))
    
    return images

def validate_github_workflow():
    """Validate GitHub workflow configuration."""
    workflow_path = Path(".github/workflows/update-docker-images.yml")
    
    if not check_file_exists(workflow_path, "GitHub workflow"):
        return False
    
    try:
        with open(workflow_path, 'r') as f:
            workflow_content = f.read()
        
        # Check for required workflow elements
        required_elements = [
            'workflow_dispatch',
            'schedule',
            'update_docker_images.py',
            'GITHUB_TOKEN'
        ]
        
        missing_elements = []
        for element in required_elements:
            if element not in workflow_content:
                missing_elements.append(element)
        
        if missing_elements:
            print(f"❌ Missing workflow elements: {', '.join(missing_elements)}")
            return False
        else:
            print("✅ GitHub workflow structure looks good")
            return True
            
    except Exception as e:
        print(f"❌ Error validating workflow: {e}")
        return False

def validate_python_script():
    """Validate Python script and dependencies."""
    script_path = Path(".github/scripts/update_docker_images.py")
    requirements_path = Path(".github/scripts/requirements.txt")
    
    script_ok = check_file_exists(script_path, "Python updater script")
    requirements_ok = check_file_exists(requirements_path, "Requirements file")
    
    if not (script_ok and requirements_ok):
        return False
    
    # Check script structure
    try:
        with open(script_path, 'r') as f:
            script_content = f.read()
        
        required_classes = ['DockerImageUpdater']
        required_methods = ['get_docker_hub_tags', 'get_ghcr_tags', 'get_latest_version']
        
        missing_items = []
        
        for cls in required_classes:
            if f"class {cls}" not in script_content:
                missing_items.append(f"class {cls}")
        
        for method in required_methods:
            if f"def {method}" not in script_content:
                missing_items.append(f"method {method}")
        
        if missing_items:
            print(f"❌ Missing script components: {', '.join(missing_items)}")
            return False
        else:
            print("✅ Python script structure looks good")
            return True
            
    except Exception as e:
        print(f"❌ Error validating Python script: {e}")
        return False

def check_repository_permissions():
    """Check if we're in a git repository with proper structure."""
    git_dir = Path(".git")
    if not git_dir.exists():
        print("❌ Not in a git repository")
        return False
    
    print("✅ Git repository detected")
    
    # Check if GitHub Actions directory exists
    actions_dir = Path(".github")
    if not actions_dir.exists():
        print("⚠️  .github directory not found - will be created by the workflow")
    else:
        print("✅ .github directory exists")
    
    return True

def simulate_dry_run():
    """Simulate a dry run to check if the script would work."""
    print("\n🧪 Simulating dry run execution...")
    
    # Check if we can import the required modules
    try:
        sys.path.insert(0, str(Path(".github/scripts")))
        
        # Try importing dependencies
        import requests
        print("✅ requests module available")
        
        try:
            from ruamel.yaml import YAML
            print("✅ ruamel.yaml module available")
        except ImportError:
            print("⚠️  ruamel.yaml not installed - install with: pip install -r .github/scripts/requirements.txt")
        
        try:
            from packaging import version
            print("✅ packaging module available")
        except ImportError:
            print("⚠️  packaging not installed - install with: pip install -r .github/scripts/requirements.txt")
        
        try:
            from github import Github
            print("✅ PyGithub module available")
        except ImportError:
            print("⚠️  PyGithub not installed - install with: pip install -r .github/scripts/requirements.txt")
        
        return True
        
    except Exception as e:
        print(f"❌ Error during dry run simulation: {e}")
        return False

def main():
    """Main validation function."""
    print("🔍 Validating Docker Image Updater Setup\n")
    
    # Change to repository root if we're in a subdirectory
    current_dir = Path.cwd()
    repo_root = current_dir
    
    # Look for repository root indicators
    while repo_root.parent != repo_root:
        if (repo_root / ".git").exists() or (repo_root / "deploy").exists():
            break
        repo_root = repo_root.parent
    
    if repo_root != current_dir:
        print(f"📁 Changing to repository root: {repo_root}")
        os.chdir(repo_root)
    
    # Run validation checks
    checks = [
        ("Repository structure", check_repository_permissions),
        ("GitHub workflow", validate_github_workflow),
        ("Python script", validate_python_script),
        ("Helm values.yaml", lambda: validate_yaml_structure(Path("deploy/helm/values.yaml"))),
        ("Dependencies", simulate_dry_run)
    ]
    
    results = {}
    for check_name, check_func in checks:
        print(f"\n🔎 Checking {check_name}...")
        try:
            results[check_name] = check_func()
        except Exception as e:
            print(f"❌ Error during {check_name} check: {e}")
            results[check_name] = False
    
    # Print summary
    print(f"\n📋 Validation Summary:")
    print("=" * 50)
    
    passed = sum(1 for result in results.values() if result)
    total = len(results)
    
    for check_name, result in results.items():
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status} {check_name}")
    
    print(f"\nOverall: {passed}/{total} checks passed ({passed/total*100:.1f}%)")
    
    if passed == total:
        print("\n🎉 All checks passed! The Docker Image Updater is ready to use.")
        print("\nNext steps:")
        print("1. Install dependencies: pip install -r .github/scripts/requirements.txt")
        print("2. Test with dry run: python .github/scripts/update_docker_images.py")
        print("3. Commit and push the changes")
        print("4. The workflow will run automatically every Monday at 9:00 AM UTC")
    else:
        print(f"\n⚠️  {total - passed} checks failed. Please fix the issues above before using the updater.")
        return False
    
    return True

if __name__ == '__main__':
    success = main()
    sys.exit(0 if success else 1)
