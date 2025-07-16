#!/usr/bin/env python3
import re
import requests
import argparse
import logging
import os
import json
from datetime import datetime
from packaging import version
from ruamel.yaml import YAML
from github import Github
from urllib.parse import urlparse
import base64
import hashlib

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("image-updater")

PR_BRANCH = "update-images"
PR_TITLE = "chore: update container images"

class ImageUpdater:
    def __init__(self, github_token, repository, values_file, chart_file):
        self.github = Github(github_token)
        self.repo = self.github.get_repo(repository)
        self.github_token = github_token
        self.repository = repository
        self.values_file = values_file
        self.chart_file = chart_file
        self.pr_description_updates = []

        
    def get_docker_hub_tags(self, repository, version_pattern=None):
        """Get all tags for a Docker Hub repository"""
        all_tags = []
        
        if "/" not in repository:
            api_repository = f"library/{repository}"
        else:
            api_repository = repository
        
        next_url = f"https://hub.docker.com/v2/repositories/{api_repository}/tags?page_size=100"
        logger.info(f"Fetching tags from: {next_url}")
        
        while next_url:
            try:
                response = requests.get(next_url, timeout=10)
                response.raise_for_status()
                data = response.json()
                
                tags = [tag["name"] for tag in data.get("results", [])]
                if version_pattern:
                    tags = [tag for tag in tags if re.match(version_pattern, tag)]
                
                all_tags.extend(tags)
                next_url = data.get("next")
            except Exception as e:
                logger.error(f"Error fetching tags for {repository}: {str(e)}")
                break
        
        return all_tags

    def get_ghcr_tags(self, repository, version_pattern=None):
        """Get tags from ghcr.io using Docker Registry HTTP API v2"""
        tags = []
        headers = {}
        if self.github_token:
            headers["Authorization"] = f"Bearer {self.github_token}"
        
        if repository.startswith('ghcr.io/'):
            repository = repository[len('ghcr.io/'):]
        
        url = f"https://ghcr.io/v2/{repository}/tags/list"

        try:
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            data = response.json()
            tags = data.get("tags", [])
            
            if version_pattern:
                tags = [tag for tag in tags if re.match(version_pattern, tag)]
            
        except Exception as e:
            logger.error(f"Error fetching GHCR tags for {repository}: {str(e)}")
            # Fallback to GitHub releases
            try:
                parts = repository.split('/')
                if len(parts) >= 2:
                    github_repo = f"{parts[0]}/{parts[1]}"
                    logger.info(f"Trying to fetch tags directly from GitHub repo: {github_repo}")
                    tags = self.get_github_repo_tags(github_repo)
            except Exception:
                pass
        
        return tags

    def get_github_repo_tags(self, repository_path):
        """Get tags from GitHub repository"""
        tags = []
        try:
            repo = self.github.get_repo(repository_path)
            tags = [tag.name for tag in repo.get_tags()]
        except Exception as e:
            logger.error(f"Error fetching GitHub tags for {repository_path}: {str(e)}")
        
        return tags

    def get_latest_version(self, tags):
        """Find the latest semantic version from a list of tags"""
        if not tags:
            return None
        
        version_tags = []
        for tag in tags:
            try:
                # Handle 'v' prefix
                ver_str = tag[1:] if tag.startswith('v') else tag
                v = version.parse(ver_str)
                if isinstance(v, version.Version):
                    version_tags.append((v, tag))
            except Exception:
                continue
        
        if not version_tags:
            return None
        
        version_tags.sort(reverse=True)
        return version_tags[0][1]

    def get_latest_tag(self, registry, repository, version_pattern=None):
        """Get the latest tag for an image"""
        if registry == "docker.io":
            tags = self.get_docker_hub_tags(repository, version_pattern)
            return self.get_latest_version(tags)
        elif registry == "ghcr.io":
            parsed_url = urlparse(repository)
            if parsed_url.hostname == "ghcr.io":
                repo_name = parsed_url.path.lstrip("/")
            else:
                repo_name = repository
                
            tags = self.get_ghcr_tags(repo_name, version_pattern)
            return self.get_latest_version(tags)
        
        logger.warning(f"Unsupported registry: {registry}")
        return None

    def detect_images_in_yaml(self, yaml_data, path=None):
        """Recursively find image configurations in YAML"""
        if path is None:
            path = []
        
        found_images = []
        
        if isinstance(yaml_data, dict):
            if "repository" in yaml_data and "tag" in yaml_data:
                repository = yaml_data["repository"]
                
                if not isinstance(repository, str):
                    return found_images
                    
                if "/" in repository and "." in repository.split("/")[0]:
                    registry = repository.split("/")[0]
                    repo_name = "/".join(repository.split("/")[1:])
                else:
                    registry = "docker.io"
                    repo_name = repository
                
                if repo_name.startswith("localhost") or "<" in repo_name or "example" in repo_name:
                    return found_images
                    
                name = repository.split("/")[-1].title()
                if path:
                    name = f"{'.'.join(path)}"
                
                current_tag = yaml_data.get("tag", "")
                version_pattern = None
                if current_tag:
                    if current_tag.startswith("v") and re.match(r"^v\d+\.\d+\.\d+", current_tag):
                        version_pattern = r"^v\d+\.\d+\.\d+$"
                    elif re.match(r"^\d+\.\d+\.\d+", current_tag):
                        version_pattern = r"^\d+\.\d+\.\d+$"
                
                found_images.append({
                    "name": name,
                    "registry": registry,
                    "repository": repository,
                    "yaml_path": path,
                    "version_pattern": version_pattern,
                    "current_tag": current_tag
                })
            
            for key, value in yaml_data.items():
                if isinstance(value, (dict, list)):
                    found_images.extend(self.detect_images_in_yaml(value, path + [key]))
        
        elif isinstance(yaml_data, list):
            for i, item in enumerate(yaml_data):
                if isinstance(item, (dict, list)):
                    found_images.extend(self.detect_images_in_yaml(item, path + [str(i)]))
        
        return found_images

    def update_yaml_file(self, file_path, changes):
        """Update the YAML file with new image tags"""
        yaml = YAML()
        yaml.preserve_quotes = True
        yaml.indent(mapping=2, sequence=4, offset=2)
        
        with open(file_path, 'r') as f:
            data = yaml.load(f)
        
        applied_changes = []
        
        for change in changes:
            path = change["path"]
            new_tag = change["new_tag"]
            
            current = data
            for part in path[:-1]:
                current = current[part]
            
            last_key = path[-1]
            if last_key in current and "tag" in current[last_key]:
                old_tag = current[last_key]["tag"]
                current[last_key]["tag"] = new_tag
                applied_changes.append({
                    "name": change["name"],
                    "old_tag": old_tag,
                    "new_tag": new_tag
                })
                logger.info(f"Updated {change['name']}: {old_tag} -> {new_tag}")
        
        with open(file_path, 'w') as f:
            yaml.dump(data, f)
        
        return applied_changes

    def update_chart_version(self, chart_file, app_version=None):
        """Update the Chart.yaml file with a new version"""
        yaml = YAML()
        yaml.preserve_quotes = True
        yaml.indent(mapping=2, sequence=4, offset=2)
        
        try:
            with open(chart_file, 'r') as f:
                chart_data = yaml.load(f)
            
            chart_version = chart_data.get("version", "0.0.0")
            version_parts = chart_version.split('.')
            
            if len(version_parts) == 3:
                version_parts[2] = str(int(version_parts[2].split('-')[0]) + 1)
                
                if '-' in chart_version:
                    suffix = chart_version.split('-', 1)[1]
                    new_chart_version = '.'.join(version_parts) + '-' + suffix
                else:
                    new_chart_version = '.'.join(version_parts)
                
                chart_data["version"] = new_chart_version
                
                if app_version:
                    chart_data["appVersion"] = app_version
                
                with open(chart_file, 'w') as f:
                    yaml.dump(chart_data, f)
                
                logger.info(f"Updated Chart.yaml: version {chart_version} -> {new_chart_version}")
        except Exception as e:
            logger.error(f"Error updating Chart.yaml: {str(e)}")

    def check_for_existing_pr(self):
        """Check if there's an existing PR for image updates"""
        try:
            pulls = self.repo.get_pulls(state='open', head=f"{self.repo.owner.login}:{PR_BRANCH}")
            for pr in pulls:
                if pr.title == PR_TITLE:
                    logger.info(f"Found existing PR: {pr.number}")
                    return pr
        except Exception as e:
            logger.error(f"Error checking for existing PR: {str(e)}")
        return None

    def get_branch_diff(self, branch_name):
        """Get differences between branch and current local changes"""
        try:
            # Get branch ref
            branch_ref = self.repo.get_git_ref(f"heads/{branch_name}")
            branch_commit = self.repo.get_commit(branch_ref.object.sha)
            
            # Get current values.yaml from branch
            try:
                branch_values = self.repo.get_contents(self.values_file, ref=branch_name)
                branch_content = base64.b64decode(branch_values.content).decode('utf-8')
                
                # Read current local values.yaml
                with open(self.values_file, 'r') as f:
                    local_content = f.read()
                
                return branch_content != local_content
            except Exception:
                # If file doesn't exist in branch, there's definitely a difference
                return True
                
        except Exception as e:
            logger.error(f"Error getting branch diff: {str(e)}")
            return True  # Assume there's a difference if we can't check

    def create_or_update_pr(self, changes, existing_pr=None):
        """Create a new PR or update an existing one"""
        # Generate PR description
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")
        
        if existing_pr:
            # Parse existing description to extract previous updates
            existing_description = existing_pr.body or ""
            
            # Look for the updates section
            updates_section_match = re.search(r'## Updated Images\n(.*?)(?=\n##|\Z)', existing_description, re.DOTALL)
            existing_updates = []
            
            if updates_section_match:
                existing_updates_text = updates_section_match.group(1)
                for line in existing_updates_text.split('\n'):
                    if line.strip().startswith('- '):
                        existing_updates.append(line.strip())
            
            # Add new updates
            new_updates = [f"- **{change['name']}**: `{change['old_tag']}` → `{change['new_tag']}`" for change in changes]
            
            # Combine and deduplicate updates
            all_updates = existing_updates + new_updates
            unique_updates = []
            seen_images = set()
            
            for update in all_updates:
                image_name = update.split('**')[1].split('**')[0] if '**' in update else update
                if image_name not in seen_images:
                    unique_updates.append(update)
                    seen_images.add(image_name)
            
            description = f"""This PR updates container images to their latest versions.

## Updated Images
{chr(10).join(unique_updates)}

---
*Last updated: {current_time}*
*This PR is automatically maintained by the image update workflow.*"""
            
            # Update the PR
            existing_pr.edit(body=description)
            logger.info(f"Updated existing PR #{existing_pr.number}")
            return existing_pr
        else:
            # Create new PR
            description = f"""This PR updates container images to their latest versions.

## Updated Images
{chr(10).join([f"- **{change['name']}**: `{change['old_tag']}` → `{change['new_tag']}`" for change in changes])}

---
*Last updated: {current_time}*
*This PR is automatically maintained by the image update workflow.*"""
            
            try:
                # Create branch first
                main_branch = self.repo.get_branch("main")
                self.repo.create_git_ref(ref=f"refs/heads/{PR_BRANCH}", sha=main_branch.commit.sha)
                
                # Create PR
                pr = self.repo.create_pull(
                    title=PR_TITLE,
                    body=description,
                    head=PR_BRANCH,
                    base="main"
                )
                logger.info(f"Created new PR #{pr.number}")
                return pr
            except Exception as e:
                logger.error(f"Error creating PR: {str(e)}")
                return None

    def commit_changes(self, changes, branch_name=PR_BRANCH):
        """Commit changes to the specified branch"""
        try:
            # Get current branch reference
            try:
                branch_ref = self.repo.get_git_ref(f"heads/{branch_name}")
                branch_sha = branch_ref.object.sha
            except:
                # Branch doesn't exist, create it from main
                main_branch = self.repo.get_branch("main")
                branch_sha = main_branch.commit.sha
                self.repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=branch_sha)
                branch_ref = self.repo.get_git_ref(f"heads/{branch_name}")
            
            # Read updated files
            files_to_commit = []
            
            # Values file
            with open(self.values_file, 'r') as f:
                values_content = f.read()
            files_to_commit.append({
                "path": self.values_file,
                "content": values_content
            })
            
            # Chart file (if it exists and was updated)
            if os.path.exists(self.chart_file):
                with open(self.chart_file, 'r') as f:
                    chart_content = f.read()
                files_to_commit.append({
                    "path": self.chart_file,
                    "content": chart_content
                })
            
            # Create commit message
            commit_message = f"chore: update container images\n\n"
            for change in changes:
                commit_message += f"- {change['name']}: {change['old_tag']} → {change['new_tag']}\n"
            
            # Create tree with updated files
            tree_elements = []
            for file_info in files_to_commit:
                blob = self.repo.create_git_blob(file_info["content"], "utf-8")
                tree_elements.append({
                    "path": file_info["path"],
                    "mode": "100644",
                    "type": "blob",
                    "sha": blob.sha
                })
            
            tree = self.repo.create_git_tree(tree_elements, base_tree=self.repo.get_git_commit(branch_sha).tree)
            
            # Create commit
            commit = self.repo.create_git_commit(
                message=commit_message,
                tree=tree,
                parents=[self.repo.get_git_commit(branch_sha)]
            )
            
            # Update branch reference
            branch_ref.edit(sha=commit.sha)
            
            logger.info(f"Committed changes to branch {branch_name}")
            return True
            
        except Exception as e:
            logger.error(f"Error committing changes: {str(e)}")
            return False

    def run_update_process(self, dry_run=False, update_chart=False, filter_string=None):
        """Main update process following the specified logic"""
        
        # Step 1: Check for image updates
        yaml = YAML()
        yaml.preserve_quotes = True
        with open(self.values_file, 'r') as f:
            values = yaml.load(f)

        logger.info(f"Detecting images in {self.values_file}...")
        detected_images = self.detect_images_in_yaml(values)
        logger.info(f"Found {len(detected_images)} images")

        if filter_string:
            detected_images = [img for img in detected_images if filter_string in img["repository"]]
            logger.info(f"After filtering: {len(detected_images)} images")

        changes = []
        app_version = None

        for image in detected_images:
            name = image["name"]
            registry = image["registry"]
            repository = image["repository"]
            yaml_path = image["yaml_path"]
            version_pattern = image["version_pattern"]
            current_tag = image["current_tag"]

            logger.info(f"Checking for updates to {name} ({repository})...")

            if not current_tag:
                logger.info(f"  Skipping {name}, no current tag")
                continue

            latest_tag = self.get_latest_tag(registry, repository, version_pattern)
            if not latest_tag:
                logger.warning(f"  Skipping {name}, couldn't determine latest tag")
                continue

            if repository == "solarwinds/solarwinds-otel-collector" and yaml_path == ["otel", "image"]:
                app_version = latest_tag

            if current_tag == latest_tag:
                logger.info(f"  Latest version already in use: {current_tag}")
                continue
            else:
                logger.info(f"  Update available: {current_tag} -> {latest_tag}")
                changes.append({
                    "name": name,
                    "path": yaml_path,
                    "old_tag": current_tag,
                    "new_tag": latest_tag
                })

        # Step 4: If no updates, exit early
        if not changes:
            logger.info("No updates needed.")
            return

        # Apply changes locally
        if not dry_run:
            applied_changes = self.update_yaml_file(self.values_file, changes)
            if update_chart:
                self.update_chart_version(self.chart_file, app_version)
        else:
            logger.info("Dry run mode: Listing changes only")
            for change in changes:
                print(f"{change['name']}: {change['old_tag']} -> {change['new_tag']}")
            return

        # Step 2: Check if PR exists
        existing_pr = self.check_for_existing_pr()
        
        if existing_pr:
            # Step 2a: Check differences between branch and local changes
            has_diff = self.get_branch_diff(PR_BRANCH)
            
            if has_diff:
                # Commit changes to existing branch
                if self.commit_changes(applied_changes, PR_BRANCH):
                    # Update PR description
                    self.create_or_update_pr(applied_changes, existing_pr)
                else:
                    logger.error("Failed to commit changes to existing branch")
            else:
                logger.info("No differences found between branch and local changes")
        else:
            # Step 3: Create new PR
            if self.commit_changes(applied_changes):
                self.create_or_update_pr(applied_changes)
            else:
                logger.error("Failed to commit changes for new PR")

        # Write changes log for GitHub Actions summary
        with open("changes.log", "w") as log_file:
            for change in applied_changes:
                log_file.write(f"{change['name']}: {change['old_tag']} -> {change['new_tag']}\n")
    
def main():
    parser = argparse.ArgumentParser(description="Update Docker image versions in Helm charts")
    parser.add_argument("--dry-run", action="store_true", help="Only print what would be updated")
    parser.add_argument("--github-token", help="GitHub token for API access")
    parser.add_argument("--repository", default="murad-sw/swi-k8s-opentelemetry-collector", help="GitHub repository")
    parser.add_argument("--update-chart", action="store_true", help="Update Chart.yaml version")
    parser.add_argument("--values-file", default="deploy/helm/values.yaml", help="Path to values.yaml")
    parser.add_argument("--chart-file", default="deploy/helm/Chart.yaml", help="Path to Chart.yaml")
    parser.add_argument("--filter", help="Only update images containing this string in repository")
    args = parser.parse_args()

    # Get GitHub token from environment if not provided
    github_token = args.github_token or os.getenv('GITHUB_TOKEN')
    if not github_token:
        logger.error("GitHub token is required. Set GITHUB_TOKEN environment variable or use --github-token")
        exit(1)

    try:
        updater = ImageUpdater(
            github_token=github_token,
            repository=args.repository,
            values_file=args.values_file,
            chart_file=args.chart_file
        )
        
        updater.run_update_process(
            dry_run=args.dry_run,
            update_chart=args.update_chart,
            filter_string=args.filter
        )
        
    except Exception as e:
        logger.error(f"Error during update process: {str(e)}")
        exit(1)

if __name__ == "__main__":
    main()