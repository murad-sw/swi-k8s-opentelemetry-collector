#!/usr/bin/env python3
"""
Automated Docker Image Updater for Helm Charts

This script monitors and updates Docker image versions in Helm charts,
specifically designed for Kubernetes OpenTelemetry Collector deployments.
"""

import os
import re
import json
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, Any
from pathlib import Path

import requests
from github import Github, GithubException, InputGitTreeElement
from packaging import version
from ruamel.yaml import YAML


# Configure logging
def setup_logging():
    """Set up structured logging with timestamps."""
    log_format = '%(asctime)s - %(levelname)s - %(message)s'
    logging.basicConfig(
        level=logging.INFO,
        format=log_format,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(f'image_update_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log')
        ]
    )
    return logging.getLogger(__name__)


class DockerImageUpdater:
    """Main class for updating Docker images in Helm charts."""
    
    def __init__(self, github_token: str, dry_run: bool = False, 
                 image_filter: str = "", force_update: bool = False):
        self.github_token = github_token
        self.github = Github(github_token)
        self.dry_run = dry_run
        self.image_filter = image_filter
        self.force_update = force_update
        self.logger = setup_logging()
        self.changes = []
        self.yaml = YAML()
        self.yaml.preserve_quotes = True
        self.yaml.width = 4096
        
        # Configuration
        self.values_file_path = Path("deploy/helm/values.yaml")
        self.chart_file_path = Path("deploy/helm/Chart.yaml")
        self.branch_name = "update-images"
        self.timeout = 10
        
        # Get repository info
        repo_info = os.environ.get('GITHUB_REPOSITORY', '').split('/')
        if len(repo_info) == 2:
            self.repo_owner, self.repo_name = repo_info
            self.repo = self.github.get_repo(f"{self.repo_owner}/{self.repo_name}")
        else:
            raise ValueError("GITHUB_REPOSITORY environment variable not set properly")

    def get_docker_hub_tags(self, repository: str) -> List[str]:
        """Fetch tags from Docker Hub API with pagination support."""
        try:
            all_tags = []
            
            # Handle library images (e.g., busybox -> library/busybox)
            if '/' not in repository:
                repo_path = f"library/{repository}"
            else:
                repo_path = repository
                
            url = f"https://hub.docker.com/v2/repositories/{repo_path}/tags"
            
            while url:
                self.logger.info(f"Fetching Docker Hub tags for {repository}: {url}")
                response = requests.get(url, timeout=self.timeout)
                response.raise_for_status()
                
                data = response.json()
                tags = [tag['name'] for tag in data.get('results', [])]
                all_tags.extend(tags)
                
                url = data.get('next')
                if len(all_tags) > 1000:  # Prevent excessive API calls
                    break
                    
            self.logger.info(f"Found {len(all_tags)} tags for {repository}")
            return all_tags
            
        except Exception as e:
            self.logger.error(f"Failed to fetch Docker Hub tags for {repository}: {e}")
            return []

    def get_ghcr_tags(self, repository: str) -> List[str]:
        """Fetch tags from GitHub Container Registry with fallback to releases."""
        try:
            # Extract owner and repo name from repository
            if repository.startswith('ghcr.io/'):
                repo_path = repository.replace('ghcr.io/', '')
            else:
                repo_path = repository
                
            parts = repo_path.split('/')
            if len(parts) < 2:
                return []
                
            owner = parts[0]
            package_name = '/'.join(parts[1:])
            
            # Try GHCR API first
            tags = self._get_ghcr_api_tags(owner, package_name)
            if tags:
                return tags
                
            # Fallback to GitHub releases
            return self._get_github_release_tags(owner, parts[1])
            
        except Exception as e:
            self.logger.error(f"Failed to fetch GHCR tags for {repository}: {e}")
            return []

    def _get_ghcr_api_tags(self, owner: str, package_name: str) -> List[str]:
        """Get tags from GHCR API."""
        try:
            headers = {
                'Authorization': f'token {self.github_token}',
                'Accept': 'application/vnd.github.v3+json'
            }
            
            # Try different API endpoints
            urls_to_try = [
                f"https://api.github.com/orgs/{owner}/packages/container/{package_name}/versions",
                f"https://api.github.com/users/{owner}/packages/container/{package_name}/versions"
            ]
            
            for url in urls_to_try:
                try:
                    response = requests.get(url, headers=headers, timeout=self.timeout)
                    if response.status_code == 200:
                        data = response.json()
                        tags = []
                        for version_info in data:
                            if version_info.get('metadata', {}).get('container', {}).get('tags'):
                                tags.extend(version_info['metadata']['container']['tags'])
                        return tags
                except Exception:
                    continue
                    
            return []
            
        except Exception as e:
            self.logger.debug(f"GHCR API failed for {owner}/{package_name}: {e}")
            return []

    def _get_github_release_tags(self, owner: str, repo_name: str) -> List[str]:
        """Get tags from GitHub releases as fallback."""
        try:
            releases_repo = self.github.get_repo(f"{owner}/{repo_name}")
            releases = releases_repo.get_releases()
            tags = [release.tag_name for release in releases[:100]]  # Limit to recent releases
            self.logger.info(f"Found {len(tags)} release tags for {owner}/{repo_name}")
            return tags
            
        except Exception as e:
            self.logger.debug(f"GitHub releases failed for {owner}/{repo_name}: {e}")
            return []

    def get_latest_version(self, repository: str, current_version: str = "") -> Optional[str]:
        """Get the latest semantic version for a Docker image."""
        # Clean repository name
        clean_repo = repository.strip().replace('docker.io/', '').replace('index.docker.io/', '')
        
        # Determine registry type and get tags
        if clean_repo.startswith('ghcr.io/'):
            tags = self.get_ghcr_tags(clean_repo)
        elif '/' in clean_repo and not clean_repo.startswith('library/'):
            # Could be Docker Hub with org/image format, but check for known registries
            if any(registry in clean_repo for registry in ['ghcr.io', 'gcr.io', 'quay.io']):
                if 'ghcr.io' in clean_repo:
                    tags = self.get_ghcr_tags(clean_repo)
                else:
                    # Other registries not supported yet, try Docker Hub as fallback
                    tags = self.get_docker_hub_tags(clean_repo)
            else:
                # Likely Docker Hub with org/image format
                tags = self.get_docker_hub_tags(clean_repo)
        else:
            # Library image or simple name
            tags = self.get_docker_hub_tags(clean_repo)
            
        if not tags:
            self.logger.warning(f"No tags found for {repository}")
            return None
            
        # Filter and sort versions
        valid_versions = []
        version_pattern = re.compile(r'^v?(\d+\.\d+\.\d+(?:-[\w\.-]+)?)$')
        
        for tag in tags:
            match = version_pattern.match(tag)
            if match:
                try:
                    # Handle special cases for solarwinds-otel-collector
                    if 'solarwinds-otel-collector' in repository and not tag.startswith('v'):
                        # For solarwinds collector, prefer versions without 'v' prefix
                        valid_versions.append((version.parse(match.group(1)), tag))
                    else:
                        valid_versions.append((version.parse(match.group(1)), tag))
                except Exception:
                    continue
                    
        if not valid_versions:
            self.logger.warning(f"No valid semantic versions found for {repository}")
            return None
            
        # Sort by version and get latest
        valid_versions.sort(key=lambda x: x[0], reverse=True)
        latest_tag = valid_versions[0][1]
        
        # Compare with current version if provided
        if current_version and not self.force_update:
            try:
                current_parsed = version.parse(current_version.lstrip('v'))
                latest_parsed = valid_versions[0][0]
                
                if latest_parsed <= current_parsed:
                    self.logger.info(f"{repository}: Current version {current_version} is up to date")
                    return None
            except Exception as e:
                self.logger.debug(f"Version comparison failed for {repository}: {e}")
                
        self.logger.info(f"{repository}: Latest version {latest_tag} (current: {current_version})")
        return latest_tag

    def find_images_in_yaml(self, yaml_data: Any, path: str = "") -> List[Dict[str, Any]]:
        """Recursively find image configurations in YAML data."""
        images = []
        
        if isinstance(yaml_data, dict):
            if 'repository' in yaml_data and 'tag' in yaml_data:
                # Found an image configuration
                images.append({
                    'path': path,
                    'repository': yaml_data['repository'],
                    'tag': yaml_data['tag'],
                    'yaml_data': yaml_data
                })
            else:
                # Recurse into nested dictionaries
                for key, value in yaml_data.items():
                    new_path = f"{path}.{key}" if path else key
                    images.extend(self.find_images_in_yaml(value, new_path))
                    
        elif isinstance(yaml_data, list):
            # Recurse into list items
            for i, item in enumerate(yaml_data):
                new_path = f"{path}[{i}]"
                images.extend(self.find_images_in_yaml(item, new_path))
                
        return images

    def should_update_image(self, repository: str) -> bool:
        """Check if image should be updated based on filter."""
        if not self.image_filter:
            return True
            
        try:
            return bool(re.search(self.image_filter, repository))
        except re.error as e:
            self.logger.error(f"Invalid regex filter '{self.image_filter}': {e}")
            return True

    def update_values_yaml(self) -> List[Dict[str, Any]]:
        """Update image tags in values.yaml file."""
        if not self.values_file_path.exists():
            self.logger.error(f"Values file not found: {self.values_file_path}")
            return []
            
        # Load YAML data
        with open(self.values_file_path, 'r') as f:
            yaml_data = self.yaml.load(f)
            
        # Find all image configurations
        images = self.find_images_in_yaml(yaml_data)
        updates = []
        
        for image_config in images:
            repository = image_config['repository']
            current_tag = image_config['tag']
            path = image_config['path']
            
            # Skip if repository doesn't match filter
            if not self.should_update_image(repository):
                continue
                
            # Skip empty or placeholder tags
            if not current_tag or current_tag.startswith('<') or current_tag.startswith('${'):
                self.logger.info(f"Skipping {repository} with placeholder tag: {current_tag}")
                continue
                
            self.logger.info(f"Checking {repository}:{current_tag} at {path}")
            
            # Get latest version
            latest_tag = self.get_latest_version(repository, current_tag)
            
            if latest_tag and latest_tag != current_tag:
                # Update the tag in YAML data
                image_config['yaml_data']['tag'] = latest_tag
                
                update_info = {
                    'path': path,
                    'repository': repository,
                    'old_tag': current_tag,
                    'new_tag': latest_tag
                }
                updates.append(update_info)
                self.logger.info(f"Updated {repository}: {current_tag} → {latest_tag}")
                
        # Save updated YAML if there are changes and not in dry run mode
        if updates and not self.dry_run:
            with open(self.values_file_path, 'w') as f:
                self.yaml.dump(yaml_data, f)
                
        return updates

    def update_chart_version(self, updates: List[Dict[str, Any]]) -> bool:
        """Optionally update Chart.yaml version and appVersion."""
        if not updates or not self.chart_file_path.exists():
            return False
            
        try:
            with open(self.chart_file_path, 'r') as f:
                chart_data = self.yaml.load(f)
                
            # Find main collector image update for appVersion
            main_image_update = None
            for update in updates:
                if 'solarwinds-otel-collector' in update['repository']:
                    main_image_update = update
                    break
                    
            # Update appVersion if main image was updated
            if main_image_update:
                old_app_version = chart_data.get('appVersion', '')
                new_app_version = main_image_update['new_tag'].lstrip('v')
                
                if old_app_version != new_app_version:
                    chart_data['appVersion'] = new_app_version
                    self.logger.info(f"Updated Chart appVersion: {old_app_version} → {new_app_version}")
                    
            # Bump chart version (patch version)
            old_chart_version = chart_data.get('version', '0.0.0')
            try:
                # Handle different version formats
                if '-alpha.' in old_chart_version:
                    # Handle format like "4.6.0-alpha.6"
                    base_version, alpha_part = old_chart_version.split('-alpha.')
                    if alpha_part.isdigit():
                        alpha_num = int(alpha_part) + 1
                        new_chart_version = f"{base_version}-alpha.{alpha_num}"
                    else:
                        # Fallback: increment patch version
                        parts = base_version.split('.')
                        if len(parts) == 3 and parts[2].isdigit():
                            parts[2] = str(int(parts[2]) + 1)
                            new_chart_version = '.'.join(parts)
                        else:
                            new_chart_version = old_chart_version
                elif '-beta.' in old_chart_version:
                    # Handle format like "4.6.0-beta.1"
                    base_version, beta_part = old_chart_version.split('-beta.')
                    if beta_part.isdigit():
                        beta_num = int(beta_part) + 1
                        new_chart_version = f"{base_version}-beta.{beta_num}"
                    else:
                        # Fallback: increment patch version
                        parts = base_version.split('.')
                        if len(parts) == 3 and parts[2].isdigit():
                            parts[2] = str(int(parts[2]) + 1)
                            new_chart_version = '.'.join(parts)
                        else:
                            new_chart_version = old_chart_version
                else:
                    # Regular semantic version (e.g., "1.2.3")
                    version_parts = old_chart_version.split('.')
                    if len(version_parts) == 3:
                        # Check if patch version has pre-release suffix
                        if '-' in version_parts[2]:
                            base_part, pre_release = version_parts[2].split('-', 1)
                            if base_part.isdigit():
                                new_base = str(int(base_part) + 1)
                                new_chart_version = f"{version_parts[0]}.{version_parts[1]}.{new_base}"
                            else:
                                new_chart_version = old_chart_version
                        elif version_parts[2].isdigit():
                            # Simple patch increment
                            version_parts[2] = str(int(version_parts[2]) + 1)
                            new_chart_version = '.'.join(version_parts)
                        else:
                            new_chart_version = old_chart_version
                    else:
                        new_chart_version = old_chart_version
                        
                if new_chart_version != old_chart_version:
                    chart_data['version'] = new_chart_version
                    self.logger.info(f"Updated Chart version: {old_chart_version} → {new_chart_version}")
                else:
                    self.logger.info(f"Chart version unchanged: {old_chart_version}")
                        
            except Exception as e:
                self.logger.warning(f"Could not update chart version: {e}")
                self.logger.debug(f"Chart version: {old_chart_version}")
                # Don't update chart version if parsing fails
                
            # Save updated Chart.yaml if not in dry run mode
            if not self.dry_run:
                with open(self.chart_file_path, 'w') as f:
                    self.yaml.dump(chart_data, f)
                    
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to update Chart.yaml: {e}")
            return False

    def create_or_update_branch(self, updates: List[Dict[str, Any]]) -> bool:
        """Create or update the update branch with changes."""
        if not updates:
            return False
            
        try:
            # Check if branch exists
            try:
                branch_ref = self.repo.get_git_ref(f"heads/{self.branch_name}")
                branch_exists = True
                self.logger.info(f"Branch {self.branch_name} already exists")
            except GithubException:
                branch_exists = False
                self.logger.info(f"Creating new branch {self.branch_name}")
                
            # Get main branch reference
            main_branch = self.repo.get_branch(self.repo.default_branch)
            
            if not branch_exists:
                # Create new branch
                self.repo.create_git_ref(
                    ref=f"refs/heads/{self.branch_name}",
                    sha=main_branch.commit.sha
                )
            else:
                # Update existing branch to latest main
                branch_ref.edit(sha=main_branch.commit.sha)
                
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to create/update branch: {e}")
            return False

    def commit_changes(self, updates: List[Dict[str, Any]]) -> bool:
        """Commit changes to the update branch."""
        if not updates or self.dry_run:
            return True
            
        try:
            # Prepare commit message
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            commit_message = f"chore: update Docker image versions ({timestamp})\n\n"
            
            for update in updates:
                commit_message += f"- {update['repository']}: {update['old_tag']} → {update['new_tag']}\n"
                
            # Get branch reference
            branch_ref = self.repo.get_git_ref(f"heads/{self.branch_name}")
            base_commit = self.repo.get_git_commit(branch_ref.object.sha)
            
            # Prepare tree elements for modification
            tree_elements = []
            
            # Read and prepare values.yaml
            if self.values_file_path.exists():
                with open(self.values_file_path, 'r', encoding='utf-8') as f:
                    values_content = f.read()
                
                # CORRECTED PART
                tree_elements.append(InputGitTreeElement(
                    path=str(self.values_file_path).replace('\\', '/'),
                    mode='100644',
                    type='blob',
                    content=values_content  # <-- Use content directly, PyGithub handles blob creation
                ))
                
            # Read and prepare Chart.yaml
            if self.chart_file_path.exists():
                with open(self.chart_file_path, 'r', encoding='utf-8') as f:
                    chart_content = f.read()

                # CORRECTED PART
                tree_elements.append(InputGitTreeElement(
                    path=str(self.chart_file_path).replace('\\', '/'),
                    mode='100644',
                    type='blob',
                    content=chart_content # <-- Use content directly
                ))
                
            # Create new tree with updated files
            # No need to specify base_tree here, the API will handle it
            new_tree = self.repo.create_git_tree(tree_elements)
            
            # Create commit
            commit = self.repo.create_git_commit(commit_message, new_tree, [base_commit])
            
            # Update branch reference
            branch_ref.edit(sha=commit.sha)
            
            self.logger.info(f"Committed changes to {self.branch_name}")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to commit changes: {e}")
            import traceback
            self.logger.debug(traceback.format_exc())
            return False

    def create_or_update_pr(self, updates: List[Dict[str, Any]]) -> Optional[str]:
        """Create or update pull request with changes."""
        if not updates:
            return None
            
        try:
            # Check for existing PR
            existing_pr = None
            prs = self.repo.get_pulls(state='open', head=f"{self.repo_owner}:{self.branch_name}")
            for pr in prs:
                existing_pr = pr
                break
                
            # Prepare PR content
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            title = f"chore: update Docker image versions ({timestamp})"
            
            # Create detailed body
            body_parts = [
                "## 🤖 Automated Docker Image Updates",
                "",
                f"This PR was automatically generated on **{timestamp}**.",
                "",
                "### 📦 Updated Images",
                ""
            ]
            
            for update in updates:
                body_parts.append(f"- **{update['repository']}**: `{update['old_tag']}` → `{update['new_tag']}`")
                
            body_parts.extend([
                "",
                "### 🔍 Validation",
                "- [x] All image tags have been verified against their respective registries",
                "- [x] Semantic versioning rules have been applied",
                "- [x] Only newer versions are included in this update",
                "",
                "### 🚀 Next Steps",
                "1. Review the changes above",
                "2. Test the deployment in a staging environment if needed",  
                "3. Merge this PR to apply the updates",
                "",
                "---",
                "*This PR was created by the Automated Docker Image Updater workflow.*"
            ])
            
            body = "\n".join(body_parts)
            
            if existing_pr:
                # Update existing PR
                existing_pr.edit(title=title, body=body)
                self.logger.info(f"Updated existing PR #{existing_pr.number}")
                return existing_pr.html_url
            else:
                # Create new PR
                if not self.dry_run:
                    new_pr = self.repo.create_pull(
                        title=title,
                        body=body,
                        head=self.branch_name,
                        base=self.repo.default_branch
                    )
                    self.logger.info(f"Created new PR #{new_pr.number}")
                    return new_pr.html_url
                else:
                    self.logger.info("Would create new PR (dry run mode)")
                    return "dry-run-pr-url"
                    
        except Exception as e:
            self.logger.error(f"Failed to create/update PR: {e}")
            return None

    def save_changes_log(self, updates: List[Dict[str, Any]]):
        """Save changes to JSON file for debugging and artifacts."""
        log_data = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'dry_run': self.dry_run,
            'filter': self.image_filter,
            'force_update': self.force_update,
            'updates': updates,
            'summary': {
                'total_updates': len(updates),
                'repositories_updated': len(set(u['repository'] for u in updates))
            }
        }
        
        filename = f"changes_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(filename, 'w') as f:
            json.dump(log_data, f, indent=2)
            
        self.logger.info(f"Saved changes log to {filename}")

    def run(self) -> bool:
        """Main execution method."""
        self.logger.info("Starting Docker Image Updater")
        self.logger.info(f"Dry run: {self.dry_run}")
        self.logger.info(f"Image filter: {self.image_filter or 'None'}")
        self.logger.info(f"Force update: {self.force_update}")
        
        try:
            # Update images in values.yaml
            updates = self.update_values_yaml()
            
            if not updates:
                self.logger.info("No image updates found")
                return True
                
            self.logger.info(f"Found {len(updates)} image updates")
            
            # Update Chart.yaml if needed
            self.update_chart_version(updates)
            
            # Save changes log
            self.save_changes_log(updates)
            
            if self.dry_run:
                self.logger.info("Dry run complete - no changes were made")
                return True
                
            # Create/update branch and commit changes
            if not self.create_or_update_branch(updates):
                return False
                
            if not self.commit_changes(updates):
                return False
                
            # Create or update PR
            pr_url = self.create_or_update_pr(updates)
            if pr_url:
                self.logger.info(f"PR available at: {pr_url}")
                
            self.logger.info("Docker Image Updater completed successfully")
            return True
            
        except Exception as e:
            self.logger.error(f"Docker Image Updater failed: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            return False


def main():
    """Main entry point."""
    # Get configuration from environment
    github_token = os.environ.get('GITHUB_TOKEN')
    if not github_token:
        print("ERROR: GITHUB_TOKEN environment variable is required")
        sys.exit(1)
        
    dry_run = os.environ.get('DRY_RUN', 'false').lower() == 'true'
    image_filter = os.environ.get('IMAGE_FILTER', '')
    force_update = os.environ.get('FORCE_UPDATE', 'false').lower() == 'true'
    
    # Create and run updater
    updater = DockerImageUpdater(
        github_token=github_token,
        dry_run=dry_run,
        image_filter=image_filter,
        force_update=force_update
    )
    
    success = updater.run()
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
