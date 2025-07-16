#!/usr/bin/env python3
import re
import requests
import argparse
import logging
import os
import subprocess
import json
from packaging import version
from ruamel.yaml import YAML
from github import Github, InputGitAuthor
from urllib.parse import urlparse


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("image-updater")

def get_docker_hub_tags(repository, version_pattern=None):
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

def get_ghcr_tags(repository, version_pattern=None, github_token=None):
    """Get tags from ghcr.io using Docker Registry HTTP API v2"""
    tags = []
    headers = {}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
    
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
    
    return tags

def get_latest_version(tags):
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

def get_latest_tag(registry, repository, version_pattern=None, github_token=None):
    """Get the latest tag for an image"""
    if registry == "docker.io":
        tags = get_docker_hub_tags(repository, version_pattern)
        return get_latest_version(tags)
    elif registry == "ghcr.io":
        parsed_url = urlparse(repository)
        if parsed_url.hostname == "ghcr.io":
            repo_name = parsed_url.path.lstrip("/")
        else:
            repo_name = repository
            
        tags = get_ghcr_tags(repo_name, version_pattern, github_token)
        
        if not tags and github_token:
            parts = repo_name.split('/')
            if len(parts) >= 2:
                fallback_repo = f"{parts[0]}/{parts[1]}"
                tags = get_ghcr_tags(fallback_repo, version_pattern, github_token)
                
        return get_latest_version(tags)
    
    logger.warning(f"Unsupported registry: {registry}")
    return None

def detect_images_in_yaml(yaml_data, path=None):
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
                registry, repo_name = repository.split("/", 1)
            else:
                registry = "docker.io"
                repo_name = repository
            
            if repo_name.startswith("localhost") or "<" in repo_name or "example" in repo_name:
                return found_images
                
            name = repository.split("/")[-1].title()
            if path:
                name = f"{path[-1].title()}-{name}"
            
            current_tag = yaml_data.get("tag", "")
            version_pattern = None
            if current_tag:
                version_pattern = r"^v?\d+\.\d+\.\d+.*$"
            
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
                found_images.extend(detect_images_in_yaml(value, path + [key]))
    
    elif isinstance(yaml_data, list):
        for i, item in enumerate(yaml_data):
            if isinstance(item, (dict, list)):
                found_images.extend(detect_images_in_yaml(item, path + [str(i)]))
    
    return found_images

def update_yaml_file(file_path, changes):
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

def run_command(cmd, cwd=None):
    """Run a shell command and return the output"""
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=cwd)
        return result.stdout.strip(), result.stderr.strip(), result.returncode
    except Exception as e:
        logger.error(f"Error running command '{cmd}': {str(e)}")
        return "", str(e), 1

def get_current_branch():
    """Get the current git branch"""
    stdout, stderr, code = run_command("git rev-parse --abbrev-ref HEAD")
    if code == 0:
        return stdout
    return None

def branch_exists(branch_name):
    """Check if a branch exists locally or remotely"""
    stdout, stderr, code = run_command(f"git show-ref --verify --quiet refs/heads/{branch_name}")
    local_exists = code == 0
    
    stdout, stderr, code = run_command(f"git show-ref --verify --quiet refs/remotes/origin/{branch_name}")
    remote_exists = code == 0
    
    return local_exists or remote_exists

def create_or_switch_branch(branch_name):
    """Create a new branch or switch to existing one"""
    if branch_exists(branch_name):
        logger.info(f"Branch {branch_name} exists, switching to it")
        run_command(f"git checkout {branch_name}")
        # Pull latest changes if branch exists remotely
        stdout, stderr, code = run_command(f"git show-ref --verify --quiet refs/remotes/origin/{branch_name}")
        if code == 0:
            run_command(f"git pull origin {branch_name}")
    else:
        logger.info(f"Creating new branch {branch_name}")
        run_command(f"git checkout -b {branch_name}")

def create_verified_commit(github_repo, branch_name, changes, values_file="deploy/helm/values.yaml"):
    """Create a verified commit using GitHub API"""
    if not changes:
        return False
    
    try:
        # Create commit message
        commit_msg = "Update Docker images\n\n"
        for change in changes:
            commit_msg += f"- {change['name']}: {change['old_tag']} -> {change['new_tag']}\n"
        
        # Get current commit SHA of the branch
        try:
            ref = github_repo.get_git_ref(f"heads/{branch_name}")
            current_sha = ref.object.sha
        except Exception:
            # If branch doesn't exist on remote, get master branch
            master_ref = github_repo.get_git_ref("heads/master")
            current_sha = master_ref.object.sha
            # Create the branch reference
            github_repo.create_git_ref(f"refs/heads/{branch_name}", current_sha)
            ref = github_repo.get_git_ref(f"heads/{branch_name}")
        
        # Get current tree
        commit = github_repo.get_git_commit(current_sha)
        base_tree = commit.tree
        
        # Read and create blob for values.yaml
        with open(values_file, 'r') as f:
            values_content = f.read()
        values_blob = github_repo.create_git_blob(values_content, "utf-8")
        
        # Create tree elements for modified files
        tree_elements = [{
            "path": values_file,
            "mode": "100644",
            "type": "blob",
            "sha": values_blob.sha
        }]
        
        # Create new tree with base tree
        new_tree = github_repo.create_git_tree(tree_elements, base_tree.sha)
        
        # Create commit with github-actions bot as author
        author = InputGitAuthor(
            "github-actions[bot]", 
            "41898282+github-actions[bot]@users.noreply.github.com"
        )
        
        new_commit = github_repo.create_git_commit(
            commit_msg,
            new_tree,
            [commit],
            author=author,
            committer=author
        )
        
        # Update branch reference
        ref.edit(new_commit.sha)
        
        logger.info("Verified commit created successfully via GitHub API")
        return True
        
    except Exception as e:
        logger.error(f"Error creating verified commit: {str(e)}")
        return False

def has_uncommitted_changes():
    """Check if there are uncommitted changes"""
    stdout, stderr, code = run_command("git status --porcelain")
    return len(stdout.strip()) > 0

def get_existing_pr(github_repo, branch_name):
    """Check if PR already exists for the branch"""
    try:
        pulls = github_repo.get_pulls(state='open', head=f"{github_repo.owner.login}:{branch_name}")
        for pull in pulls:
            return pull
    except Exception as e:
        logger.error(f"Error checking for existing PR: {str(e)}")
    return None

def create_pr_description(changes):
    """Create a description for the PR"""
    if not changes:
        return "No image updates found."
    
    description = "## Docker Image Updates\n\n"
    description += "This PR updates the following Docker images to their latest versions:\n\n"
    
    for change in changes:
        description += f"- **{change['name']}**: `{change['old_tag']}` → `{change['new_tag']}`\n"
    
    description += "\n---\n*This PR was automatically generated by the image update workflow.*"
    return description

def update_pr_description(existing_pr, new_changes):
    """Update existing PR description with new changes"""
    current_body = existing_pr.body or ""
    
    # Parse existing changes from description
    existing_changes = []
    if "## Docker Image Updates" in current_body:
        lines = current_body.split('\n')
        for line in lines:
            if line.strip().startswith('- **') and '→' in line:
                existing_changes.append(line.strip())
    
    # Add new changes
    new_lines = []
    for change in new_changes:
        new_line = f"- **{change['name']}**: `{change['old_tag']}` → `{change['new_tag']}`"
        
        # Check if this image was already updated (replace old entry)
        updated = False
        for i, existing_line in enumerate(existing_changes):
            if f"**{change['name']}**" in existing_line:
                existing_changes[i] = new_line
                updated = True
                break
        
        if not updated:
            new_lines.append(new_line)
    
    # Combine all changes
    all_changes = existing_changes + new_lines
    
    # Rebuild description
    description = "## Docker Image Updates\n\n"
    description += "This PR updates the following Docker images to their latest versions:\n\n"
    
    for change_line in all_changes:
        description += change_line + "\n"
    
    description += "\n---\n*This PR was automatically generated by the image update workflow.*"
    return description

def create_or_update_pr(github_repo, branch_name, changes):
    """Create a new PR or update existing one"""
    existing_pr = get_existing_pr(github_repo, branch_name)
    
    if existing_pr:
        logger.info(f"Updating existing PR #{existing_pr.number}")
        new_description = update_pr_description(existing_pr, changes)
        existing_pr.edit(body=new_description)
        return existing_pr
    else:
        logger.info("Creating new PR")
        title = "Update Docker images to latest versions"
        description = create_pr_description(changes)
        
        new_pr = github_repo.create_pull(
            title=title,
            body=description,
            head=branch_name,
            base="master"
        )
        return new_pr

def main():
    parser = argparse.ArgumentParser(description="Update Docker image versions in Helm charts")
    parser.add_argument("--github-token", help="GitHub token for fetching tags")
    parser.add_argument("--repository", default="murad-sw/swi-k8s-opentelemetry-collector", help="GitHub repository")
    parser.add_argument("--values-file", default="deploy/helm/values.yaml", help="Path to values.yaml")
    args = parser.parse_args()

    # Get GitHub token from environment if not provided
    if not args.github_token:
        args.github_token = os.getenv('GITHUB_TOKEN')
    
    # Get repository from environment if not provided
    if not args.repository:
        args.repository = os.getenv('REPOSITORY', 'murad-sw/swi-k8s-opentelemetry-collector')

    if not args.github_token:
        logger.error("GitHub token is required")
        return 1

    yaml = YAML()
    yaml.preserve_quotes = True
    with open(args.values_file, 'r') as f:
        values = yaml.load(f)

    logger.info(f"Detecting images in {args.values_file}...")
    detected_images = detect_images_in_yaml(values)
    logger.info(f"Found {len(detected_images)} images")

    changes = []

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

        latest_tag = get_latest_tag(registry, repository, version_pattern, args.github_token)
        if not latest_tag:
            logger.warning(f"  Skipping {name}, couldn't determine latest tag")
            continue

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

    if not changes:
        logger.info("No updates needed.")
        return 0

    branch_name = "update-images"
    original_branch = get_current_branch()
    
    try:
        # Initialize GitHub API client
        g = Github(args.github_token)
        repo = g.get_repo(args.repository)
        
        # Create or switch to update branch
        create_or_switch_branch(branch_name)
        
        # Apply changes
        applied_changes = update_yaml_file(args.values_file, changes)
        logger.info(f"Updated {len(applied_changes)} images in {args.values_file}")
        
        # Create verified commit via GitHub API
        if create_verified_commit(repo, branch_name, applied_changes, args.values_file):
            logger.info("Verified commit created successfully")
            
            # Create or update PR
            try:
                pr = create_or_update_pr(repo, branch_name, applied_changes)
                logger.info(f"PR created/updated: {pr.html_url}")
            except Exception as e:
                logger.error(f"Error creating/updating PR: {str(e)}")
                return 1
        else:
            logger.error("Failed to create verified commit")
            return 1
            
    finally:
        # Return to original branch
        if original_branch:
            run_command(f"git checkout {original_branch}")

    return 0

if __name__ == "__main__":
    import sys
    sys.exit(main())
