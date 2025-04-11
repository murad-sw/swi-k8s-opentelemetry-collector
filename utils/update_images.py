#!/usr/bin/env python3
import os
import sys
import re
import requests
import yaml
import argparse
import tempfile
import subprocess
import logging
from datetime import datetime
from packaging import version
from github import Github

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("image-updater")

# Images to check and update
IMAGES = [
    {
        "name": "SWO Collector",
        "registry": "docker.io",
        "repository": "solarwinds/solarwinds-otel-collector",
        "yaml_path": ["otel", "image"],
        "version_pattern": r"^v?\d+\.\d+\.\d+$",
    },
    {
        "name": "BusyBox",
        "registry": "docker.io",
        "repository": "busybox",
        "yaml_path": ["otel", "init_images", "busy_box"],
        "version_pattern": r"^\d+\.\d+\.\d+$",
    },
    {
        "name": "SWO Windows Collector",
        "registry": "docker.io",
        "repository": "solarwinds/solarwinds-otel-collector",
        "yaml_path": ["otel", "windows", "image"],
        "version_pattern": r"^v?\d+\.\d+\.\d+$",
    },
    {
        "name": "SWO Agent",
        "registry": "docker.io",
        "repository": "solarwinds/swo-agent",
        "yaml_path": ["swoagent", "image"],
        "version_pattern": r"^v\d+\.\d+\.\d+$",
    },
    {
        "name": "eBPF Kernel Collector",
        "registry": "docker.io",
        "repository": "solarwinds/opentelemetry-ebpf-kernel-collector",
        "yaml_path": ["ebpfNetworkMonitoring", "kernelCollector", "image"],
        "version_pattern": r"^v\d+\.\d+\.\d+$",
    },
    {
        "name": "eBPF K8s Watcher",
        "registry": "docker.io",
        "repository": "solarwinds/opentelemetry-ebpf-k8s-watcher",
        "yaml_path": ["ebpfNetworkMonitoring", "k8sCollector", "watcher", "image"],
        "version_pattern": r"^v\d+\.\d+\.\d+$",
    },
    {
        "name": "eBPF K8s Relay",
        "registry": "docker.io",
        "repository": "solarwinds/opentelemetry-ebpf-k8s-relay",
        "yaml_path": ["ebpfNetworkMonitoring", "k8sCollector", "relay", "image"],
        "version_pattern": r"^v\d+\.\d+\.\d+$",
    },
    {
        "name": "eBPF Reducer",
        "registry": "docker.io",
        "repository": "solarwinds/opentelemetry-ebpf-reducer",
        "yaml_path": ["ebpfNetworkMonitoring", "reducer", "image"],
        "version_pattern": r"^v\d+\.\d+\.\d+$",
    },
    {
        "name": "Beyla",
        "registry": "docker.io",
        "repository": "grafana/beyla",
        "yaml_path": ["beyla", "image"],
        "version_pattern": r"^\d+\.\d+\.\d+$",
    },
    {
        "name": "Alpine K8s",
        "registry": "docker.io",
        "repository": "alpine/k8s",
        "yaml_path": ["autoupdate", "image"],
        "version_pattern": r"^\d+\.\d+\.\d+$",
    },
    {
        "name": "Alpine K8s (Wait Jobs)",
        "registry": "docker.io",
        "repository": "alpine/k8s",
        "yaml_path": ["waitJobs", "operator", "image"],
        "version_pattern": r"^\d+\.\d+\.\d+$",
    },
    # Add other images as needed
]

def get_docker_hub_tags(repository, version_pattern=None):
    """Get all tags for a Docker Hub repository with pagination"""
    all_tags = []
    next_url = f"https://hub.docker.com/v2/repositories/{repository}/tags?page_size=1000"
    
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

def get_latest_tag(registry, repository, version_pattern=None):
    """Get the latest tag for an image"""
    if registry == "docker.io":
        tags = get_docker_hub_tags(repository, version_pattern)
        return get_latest_version(tags)
    
    # Add support for other registries as needed
    logger.warning(f"Unsupported registry: {registry}")
    return None

def get_yaml_value(yaml_data, path):
    """Get a value from a nested YAML dictionary"""
    current = yaml_data
    for part in path:
        if part not in current:
            return None
        current = current[part]
    return current

def update_chart_version(chart_file, app_version=None):
    """Update the Chart.yaml file with a new version"""
    try:
        with open(chart_file, "r") as f:
            chart_data = yaml.safe_load(f)
        
        # Increment chart version
        chart_version = chart_data.get("version", "0.0.0")
        version_parts = chart_version.split('.')
        
        if len(version_parts) == 3:
            version_parts[2] = str(int(version_parts[2]) + 1)
            new_chart_version = '.'.join(version_parts)
            chart_data["version"] = new_chart_version
            
            if app_version:
                chart_data["appVersion"] = app_version
            
            with open(chart_file, "w") as f:
                yaml.dump(chart_data, f, default_flow_style=False)
            
            logger.info(f"Updated Chart.yaml: version {chart_version} -> {new_chart_version}")
            return True
    except Exception as e:
        logger.error(f"Error updating Chart.yaml: {str(e)}")
    
    return False

def create_pr(changes, github_token, repository, chart_updated=False):
    """Create a PR with the image updates"""
    branch_name = f"update-images-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    
    try:
        # Create branch and commit changes
        subprocess.run(["git", "checkout", "-b", branch_name], check=True)
        subprocess.run(["git", "add", "deploy/helm/values.yaml"], check=True)
        
        if chart_updated:
            subprocess.run(["git", "add", "deploy/helm/Chart.yaml"], check=True)
        
        # Prepare commit message
        commit_msg = "Update Docker image versions\n\n"
        for change in changes:
            commit_msg += f"- {change['name']}: {change['old_tag']} -> {change['new_tag']}\n"
        
        if chart_updated:
            commit_msg += "\nAlso updated Chart.yaml with new version and appVersion."
        
        with tempfile.NamedTemporaryFile(mode='w', delete=False) as temp:
            temp.write(commit_msg)
            temp_path = temp.name
        
        subprocess.run(["git", "commit", "-F", temp_path], check=True)
        os.unlink(temp_path)
        
        # Push branch
        subprocess.run(["git", "push", "origin", branch_name], check=True)
        
        # Create PR via GitHub API
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
    parser.add_argument("--repository", default="solarwinds/swi-k8s-opentelemetry-collector", help="GitHub repository")
    parser.add_argument("--update-chart", action="store_true", help="Update Chart.yaml version")
    args = parser.parse_args()
    
    # Path to values.yaml
    values_file = "deploy/helm/values.yaml"
    chart_file = "deploy/helm/Chart.yaml"
    
    # Read the current values
    try:
        with open(values_file, "r") as f:
            values = yaml.safe_load(f)
    except Exception as e:
        logger.error(f"Error reading {values_file}: {str(e)}")
        sys.exit(1)
    
    changes = []
    app_version = None
    
    for image_info in IMAGES:
        name = image_info["name"]
        registry = image_info["registry"]
        repository = image_info["repository"]
        yaml_path = image_info["yaml_path"]
        version_pattern = image_info.get("version_pattern")
        
        logger.info(f"Checking for updates to {name} ({repository})...")
        
        # Get the latest tag
        latest_tag = get_latest_tag(registry, repository, version_pattern)
        if not latest_tag:
            logger.warning(f"  Skipping {name}, couldn't determine latest tag")
            continue
        
        # Get the current values
        current_values = get_yaml_value(values, yaml_path)
        if not current_values or not isinstance(current_values, dict):
            logger.warning(f"  Skipping {name}, path {yaml_path} not found in values.yaml")
            continue
        
        current_tag = current_values.get("tag")
        
        # Save main collector version for Chart.appVersion
        if name == "SWO Collector" and latest_tag:
            app_version = latest_tag
        
        # Check if update needed
        if current_tag != latest_tag:
            logger.info(f"  Update available: {current_tag} -> {latest_tag}")
            changes.append({
                "name": name,
                "path": yaml_path,
                "old_tag": current_tag,
                "new_tag": latest_tag
            })
            
            # Update the values
            if not args.dry_run:
                current_values["tag"] = latest_tag
    
    # If there are changes, update files
    if changes and not args.dry_run:
        # Update values.yaml
        try:
            with open(values_file, "w") as f:
                yaml.dump(values, f, default_flow_style=False)
            logger.info(f"Updated {len(changes)} images in {values_file}")
        except Exception as e:
            logger.error(f"Error writing to {values_file}: {str(e)}")
            sys.exit(1)
        
        # Update Chart.yaml if requested
        chart_updated = False
        if args.update_chart:
            chart_updated = update_chart_version(chart_file, app_version)
        
        # Create a PR if requested
        if args.create_pr:
            if args.github_token:
                create_pr(changes, args.github_token, args.repository, chart_updated)
            else:
                logger.error("GitHub token is required to create a PR")
    elif not changes:
        logger.info("No updates needed.")

if __name__ == "__main__":
    main()