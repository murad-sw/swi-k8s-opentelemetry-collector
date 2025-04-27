#!/usr/bin/env python3
import os
import re
import sys
import requests
import argparse
import subprocess
import logging
from datetime import datetime
from packaging import version
from github import Github
from ruamel.yaml import YAML

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("image-updater")

def get_docker_hub_tags(repository, version_pattern=None):
    """Get all tags for a Docker Hub repository with pagination"""
    all_tags = []
    next_url = f"https://hub.docker.com/v2/repositories/{repository}/tags?page_size=100"
    
    while next_url:
        try:
            response = requests.get(next_url)
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
    """
    Get tags from ghcr.io using Docker Registry HTTP API v2
    """
    tags = []
    headers = {}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
    
    url = f"https://ghcr.io/v2/{repository}/tags/list"

    try:
        response = requests.get(url, headers=headers)
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
        tags = get_ghcr_tags(repository, version_pattern, github_token)
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
    """Update the YAML file with new image tags, preserving comments and structure"""
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
            if part not in current:
                logger.warning(f"Path {path} not found in YAML")
                break
            current = current[part]
        
        if path[-1] in current and "tag" in current[path[-1]]:
            old_tag = current[path[-1]]["tag"]
            current[path[-1]]["tag"] = new_tag
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
            return new_chart_version
    except Exception as e:
        logger.error(f"Error updating Chart.yaml: {str(e)}")
    
    return None

def chart_release(changes, github_token, repository, chart_updated=False, new_chart_version=None):
    """Create a PR with the image updates using the chart release script for verified commits"""
    branch_name = f"update-images-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    
    try:
        # First, create a branch and commit the changes
        subprocess.run(["git", "checkout", "-b", branch_name], check=True)
        subprocess.run(["git", "add", "deploy/helm/values.yaml"], check=True)
        
        if chart_updated:
            subprocess.run(["git", "add", "deploy/helm/Chart.yaml"], check=True)
        
        commit_msg = "Update Docker image versions\n\n"
        for change in changes:
            commit_msg += f"- {change['name']}: {change['old_tag']} -> {change['new_tag']}\n"
        
        with open("commit_msg.txt", "w") as f:
            f.write(commit_msg)
        
        subprocess.run(["git", "commit", "-F", "commit_msg.txt"], check=True)
        os.remove("commit_msg.txt")
        
        # Push the branch
        subprocess.run(["git", "push", "origin", branch_name], check=True)
        
        # If chart was updated, use the chart release script
        if chart_updated and new_chart_version:
            release_name = f"swo-k8s-collector-{new_chart_version}"
            logger.info(f"Running chart release script for {release_name}...")
            
            # Set the necessary environment variables for the chart release script
            env = os.environ.copy()
            env["GITHUB_TOKEN"] = github_token
            
            # Run the chart release script
            try:
                subprocess.run(["bash", ".github/cr.sh"], check=True, env=env)
                logger.info(f"Chart release script completed successfully")
            except subprocess.CalledProcessError as e:
                logger.error(f"Error running chart release script: {str(e)}")
        
        # Create a PR with the GitHub API
        g = Github(github_token)
        repo = g.get_repo(repository)
        
        labels = ["docker", "dependencies"]
        pr = repo.create_pull(
            title="Update Docker image versions",
            body=commit_msg,
            head=branch_name,
            base="master"
        )
        pr.add_to_labels(*labels)
        
        logger.info(f"Created PR #{pr.number}: {pr.html_url}")
        
    except Exception as e:
        logger.error(f"Error creating PR: {str(e)}")

def main():
    parser = argparse.ArgumentParser(description="Update Docker image versions in Helm charts")
    parser.add_argument("--create-pr", action="store_true", help="Create a PR with the changes")
    parser.add_argument("--dry-run", action="store_true", help="Only print what would be updated")
    parser.add_argument("--github-token", help="GitHub token for creating PRs")
    parser.add_argument("--repository", default="murad-sw/swi-k8s-opentelemetry-collector", help="GitHub repository")
    parser.add_argument("--update-chart", action="store_true", help="Update Chart.yaml version")
    parser.add_argument("--values-file", default="deploy/helm/values.yaml", help="Path to values.yaml")
    parser.add_argument("--chart-file", default="deploy/helm/Chart.yaml", help="Path to Chart.yaml")
    parser.add_argument("--filter", help="Only update images containing this string in repository")
    parser.add_argument("--verified", action="store_true", help="Use chart release script for verified commits")
    args = parser.parse_args()
    
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
    
    if changes and not args.dry_run:
        applied_changes = update_yaml_file(args.values_file, changes)
        logger.info(f"Updated {len(applied_changes)} images in {args.values_file}")
        
        chart_updated = False
        new_chart_version = None
        if args.update_chart:
            new_chart_version = update_chart_version(args.chart_file, app_version)
            chart_updated = (new_chart_version is not None)
        
        if args.create_pr and applied_changes:
            if args.github_token:
                if args.verified:
                    chart_release(applied_changes, args.github_token, args.repository, chart_updated, new_chart_version)
                else:
                    # Original PR creation function
                    create_pr(applied_changes, args.github_token, args.repository, chart_updated)
            else:
                logger.error("GitHub token is required to create a PR")
    elif not changes:
        logger.info("No updates needed.")

def create_pr(changes, github_token, repository, chart_updated=False):
    """Original PR creation function (kept for backward compatibility)"""
    branch_name = f"update-images-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    
    try:
        subprocess.run(["git", "checkout", "-b", branch_name], check=True)
        subprocess.run(["git", "add", "deploy/helm/values.yaml"], check=True)
        
        if chart_updated:
            subprocess.run(["git", "add", "deploy/helm/Chart.yaml"], check=True)
        
        commit_msg = "Update Docker image versions\n\n"
        for change in changes:
            commit_msg += f"- {change['name']}: {change['old_tag']} -> {change['new_tag']}\n"
        
        with open("commit_msg.txt", "w") as f:
            f.write(commit_msg)
        
        subprocess.run(["git", "commit", "-F", "commit_msg.txt"], check=True)
        os.remove("commit_msg.txt")
        
        subprocess.run(["git", "push", "origin", branch_name], check=True)
        
        g = Github(github_token)
        repo = g.get_repo(repository)
        
        labels = ["docker", "dependencies"]
        pr = repo.create_pull(
            title="Update Docker image versions",
            body=commit_msg,
            head=branch_name,
            base="master"
        )
        pr.add_to_labels(*labels)
        
        logger.info(f"Created PR #{pr.number}: {pr.html_url}")
        
    except Exception as e:
        logger.error(f"Error creating PR: {str(e)}")

if __name__ == "__main__":
    main()