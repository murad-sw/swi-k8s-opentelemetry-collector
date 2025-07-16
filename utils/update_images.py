#!/usr/bin/env python3
import re
import requests
import argparse
import logging
import json
import os
from datetime import datetime
from packaging import version
from ruamel.yaml import YAML
from github import Github
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
                github_repo = f"{parts[0]}/{parts[1]}"
                logger.info(f"Trying to fetch tags directly from GitHub repo: {github_repo}")
                tags = get_github_repo_tags(github_repo, github_token)
                
        return get_latest_version(tags)
    
    logger.warning(f"Unsupported registry: {registry}")
    return None

def get_github_repo_tags(repository_path, github_token=None):
    """Get tags from GitHub repository"""
    
    tags = []
    try:
        g = Github(github_token)
        repo = g.get_repo(repository_path)
        tags = [tag.name for tag in repo.get_tags()]
    except Exception as e:
        logger.error(f"Error fetching GitHub tags for {repository_path}: {str(e)}")
    
    return tags

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

def update_chart_version(chart_file, app_version=None):
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
    
def find_existing_pr(github_client, repository, branch_name="update-images"):
    """Find existing PR for image updates"""
    try:
        repo = github_client.get_repo(repository)
        pulls = repo.get_pulls(state="open", head=f"{repo.owner.login}:{branch_name}")
        
        for pr in pulls:
            if pr.head.ref == branch_name:
                logger.info(f"Found existing PR: #{pr.number} - {pr.title}")
                return pr
        
        logger.info("No existing PR found")
        return None
    except Exception as e:
        logger.error(f"Error finding existing PR: {str(e)}")
        return None

def get_pr_description(existing_pr, new_changes):
    """Generate or update PR description"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")
    
    # Create description for new changes
    new_description = f"## 🔄 Automated Image Updates\n\n"
    new_description += f"**Last updated:** {timestamp}\n\n"
    new_description += "### Updated Images:\n\n"
    
    for change in new_changes:
        new_description += f"- **{change['name']}**: `{change['old_tag']}` → `{change['new_tag']}`\n"
    
    new_description += "\n---\n"
    new_description += "*This PR was automatically generated by the image update workflow.*"
    
    if existing_pr:
        existing_body = existing_pr.body or ""
        
        # Check if this is an automated PR (contains our marker)
        if "🔄 Automated Image Updates" in existing_body:
            # Extract existing changes from the description
            existing_changes = []
            lines = existing_body.split('\n')
            in_updates_section = False
            
            for line in lines:
                if line.strip() == "### Updated Images:":
                    in_updates_section = True
                    continue
                elif line.strip().startswith("---"):
                    break
                elif in_updates_section and line.strip().startswith("- **"):
                    existing_changes.append(line.strip())
            
            # Merge existing and new changes, avoiding duplicates
            all_changes = {}
            
            # Parse existing changes
            for line in existing_changes:
                if match := re.match(r"- \*\*(.*?)\*\*: `(.*?)` → `(.*?)`", line):
                    image_name, old_tag, new_tag = match.groups()
                    all_changes[image_name] = {"old_tag": old_tag, "new_tag": new_tag}
            
            # Add new changes (will overwrite if same image)
            for change in new_changes:
                all_changes[change['name']] = {
                    "old_tag": change['old_tag'],
                    "new_tag": change['new_tag']
                }
            
            # Reconstruct description
            description = f"## 🔄 Automated Image Updates\n\n"
            description += f"**Last updated:** {timestamp}\n\n"
            description += "### Updated Images:\n\n"
            
            for image_name, change in all_changes.items():
                description += f"- **{image_name}**: `{change['old_tag']}` → `{change['new_tag']}`\n"
            
            description += "\n---\n"
            description += "*This PR was automatically generated by the image update workflow.*"
            
            return description
    
    return new_description

def commit_changes(github_client, repository, branch_name, file_paths, commit_message):
    """Commit changes to GitHub using the API"""
    try:
        repo = github_client.get_repo(repository)
        
        # Get the latest commit on main branch
        main_branch = repo.get_branch("main")
        base_sha = main_branch.commit.sha
        
        # Try to get existing branch
        try:
            branch = repo.get_branch(branch_name)
            branch_sha = branch.commit.sha
            logger.info(f"Using existing branch: {branch_name}")
        except:
            # Create new branch from main
            ref = repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=base_sha)
            branch_sha = base_sha
            logger.info(f"Created new branch: {branch_name}")
        
        # Prepare files for commit
        files_to_commit = []
        for file_path in file_paths:
            if os.path.exists(file_path):
                with open(file_path, 'r') as f:
                    content = f.read()
                files_to_commit.append({
                    "path": file_path,
                    "content": content
                })
        
        if not files_to_commit:
            logger.warning("No files to commit")
            return False
        
        # Get current tree
        base_tree = repo.get_git_tree(branch_sha)
        
        # Create new tree with updated files
        tree_elements = []
        for file_info in files_to_commit:
            blob = repo.create_git_blob(file_info["content"], "utf-8")
            tree_elements.append({
                "path": file_info["path"],
                "mode": "100644",
                "type": "blob",
                "sha": blob.sha
            })
        
        new_tree = repo.create_git_tree(tree_elements, base_tree)
        
        # Create commit
        commit = repo.create_git_commit(
            message=commit_message,
            tree=new_tree,
            parents=[repo.get_git_commit(branch_sha)]
        )
        
        # Update branch reference
        repo.get_git_ref(f"heads/{branch_name}").edit(commit.sha)
        
        logger.info(f"Successfully committed changes to branch: {branch_name}")
        return True
        
    except Exception as e:
        logger.error(f"Error committing changes: {str(e)}")
        return False

def create_or_update_pr(github_client, repository, branch_name, changes, existing_pr=None):
    """Create new PR or update existing one"""
    try:
        repo = github_client.get_repo(repository)
        
        title = "🔄 Automated Image Updates"
        description = get_pr_description(existing_pr, changes)
        
        if existing_pr:
            # Update existing PR
            existing_pr.edit(body=description)
            logger.info(f"Updated existing PR: #{existing_pr.number}")
            return existing_pr
        else:
            # Create new PR
            pr = repo.create_pull(
                title=title,
                body=description,
                head=branch_name,
                base="main"
            )
            logger.info(f"Created new PR: #{pr.number}")
            return pr
            
    except Exception as e:
        logger.error(f"Error creating/updating PR: {str(e)}")
        return None

def check_branch_differences(github_client, repository, branch_name, local_changes):
    """Check if there are differences between branch and local changes"""
    try:
        repo = github_client.get_repo(repository)
        
        # Get the branch
        try:
            branch = repo.get_branch(branch_name)
        except:
            # Branch doesn't exist, so there are differences
            return True
        
        # Get the values.yaml file from the branch
        try:
            file_content = repo.get_contents("deploy/helm/values.yaml", ref=branch_name)
            branch_content = file_content.decoded_content.decode('utf-8')
            
            # Parse the branch content to get current image tags
            yaml = YAML()
            yaml.preserve_quotes = True
            branch_data = yaml.load(branch_content)
            
            branch_images = detect_images_in_yaml(branch_data)
            
            # Compare with local changes
            for change in local_changes:
                # Find corresponding image in branch
                branch_image = None
                for img in branch_images:
                    if img['name'] == change['name'] and img['yaml_path'] == change['path']:
                        branch_image = img
                        break
                
                if not branch_image or branch_image['current_tag'] != change['new_tag']:
                    logger.info(f"Difference found for {change['name']}: branch has {branch_image['current_tag'] if branch_image else 'N/A'}, local has {change['new_tag']}")
                    return True
            
            logger.info("No differences found between branch and local changes")
            return False
            
        except Exception as e:
            logger.warning(f"Could not compare branch content: {str(e)}")
            return True
            
    except Exception as e:
        logger.error(f"Error checking branch differences: {str(e)}")
        return True

def main():
    parser = argparse.ArgumentParser(description="Update Docker image versions in Helm charts")
    parser.add_argument("--dry-run", action="store_true", help="Only print what would be updated")
    parser.add_argument("--github-token", required=True, help="GitHub token for API access")
    parser.add_argument("--repository", default="murad-sw/swi-k8s-opentelemetry-collector", help="GitHub repository")
    parser.add_argument("--update-chart", action="store_true", help="Update Chart.yaml version")
    parser.add_argument("--values-file", default="deploy/helm/values.yaml", help="Path to values.yaml")
    parser.add_argument("--chart-file", default="deploy/helm/Chart.yaml", help="Path to Chart.yaml")
    parser.add_argument("--filter", help="Only update images containing this string in repository")
    parser.add_argument("--branch-name", default="update-images", help="Branch name for PR")
    args = parser.parse_args()

    # Initialize GitHub client
    github_client = Github(args.github_token)
    
    # Step 1: Check images for updates
    yaml = YAML()
    yaml.preserve_quotes = True
    with open(args.values_file, 'r') as f:
        values = yaml.load(f)

    logger.info(f"Detecting images in {args.values_file}...")
    detected_images = detect_images_in_yaml(values)
    logger.info(f"Found {len(detected_images)} images")

    if args.filter:
        detected_images = [img for img in detected_images if args.filter in img["repository"]]
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

        latest_tag = get_latest_tag(registry, repository, version_pattern, args.github_token)
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

    if args.dry_run:
        logger.info("Dry run mode: Listing changes only")
        for change in changes:
            print(f"{change['name']}: {change['old_tag']} -> {change['new_tag']}")
        return

    # Step 2: Check if PR exists
    existing_pr = find_existing_pr(github_client, args.repository, args.branch_name)
    
    # Apply changes locally
    applied_changes = update_yaml_file(args.values_file, changes)
    logger.info(f"Updated {len(applied_changes)} images in {args.values_file}")

    if args.update_chart:
        update_chart_version(args.chart_file, app_version)

    # Determine files to commit
    files_to_commit = [args.values_file]
    if args.update_chart:
        files_to_commit.append(args.chart_file)

    # Step 2 & 3: Handle PR logic
    if existing_pr:
        # Check if there are differences between branch and local changes
        if check_branch_differences(github_client, args.repository, args.branch_name, applied_changes):
            logger.info("Found differences, updating existing PR...")
            
            # Commit changes to existing branch
            image_updates = ', '.join([f"{c['name']} to {c['new_tag']}" for c in applied_changes])
            commit_message = f"Update images: {image_updates}"
            if commit_changes(github_client, args.repository, args.branch_name, files_to_commit, commit_message):
                # Update PR description
                create_or_update_pr(github_client, args.repository, args.branch_name, applied_changes, existing_pr)
            else:
                logger.error("Failed to commit changes")
                exit(1)
        else:
            logger.info("No differences found, PR is up to date")
    else:
        # Create new PR
        logger.info("Creating new PR...")
        
        # Commit changes to new branch
        image_updates = ', '.join([f"{c['name']} to {c['new_tag']}" for c in applied_changes])
        commit_message = f"Update images: {image_updates}"
        if commit_changes(github_client, args.repository, args.branch_name, files_to_commit, commit_message):
            # Create new PR
            pr = create_or_update_pr(github_client, args.repository, args.branch_name, applied_changes)
            if not pr:
                logger.error("Failed to create PR")
                exit(1)
        else:
            logger.error("Failed to commit changes")
            exit(1)

    # Log changes for reference
    with open("changes.log", "w") as log_file:
        for change in applied_changes:
            log_file.write(f"{change['name']}: {change['old_tag']} -> {change['new_tag']}\n")

    logger.info("Image update process completed successfully!")

if __name__ == "__main__":
    main()