#!/bin/bash
set -e

# Automated Docker Image Updater - Setup and Testing Script

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SCRIPTS_DIR="$REPO_ROOT/.github/scripts"

echo "🚀 Docker Image Updater - Setup and Testing"
echo "============================================="
echo ""
echo "Repository root: $REPO_ROOT"
echo "Scripts directory: $SCRIPTS_DIR"
echo ""

# Function to print status messages
print_status() {
    echo -e "\n🔹 $1"
}

print_success() {
    echo -e "✅ $1"
}

print_error() {
    echo -e "❌ $1"
}

print_warning() {
    echo -e "⚠️  $1"
}

# Check if Python is available
check_python() {
    print_status "Checking Python installation..."
    
    if command -v python3 &> /dev/null; then
        PYTHON_CMD="python3"
        PYTHON_VERSION=$(python3 --version)
        print_success "Python found: $PYTHON_VERSION"
    elif command -v python &> /dev/null; then
        PYTHON_CMD="python"
        PYTHON_VERSION=$(python --version)
        print_success "Python found: $PYTHON_VERSION"
    else
        print_error "Python not found. Please install Python 3.7 or later."
        exit 1
    fi
}

# Install dependencies
install_dependencies() {
    print_status "Installing Python dependencies..."
    
    cd "$SCRIPTS_DIR"
    
    if [ -f "requirements.txt" ]; then
        $PYTHON_CMD -m pip install --upgrade pip
        $PYTHON_CMD -m pip install -r requirements.txt
        print_success "Dependencies installed successfully"
    else
        print_error "requirements.txt not found in $SCRIPTS_DIR"
        exit 1
    fi
}

# Run validation
run_validation() {
    print_status "Running setup validation..."
    
    cd "$REPO_ROOT"
    
    if [ -f ".github/scripts/validate_setup.py" ]; then
        $PYTHON_CMD .github/scripts/validate_setup.py
        if [ $? -eq 0 ]; then
            print_success "Validation passed"
        else
            print_error "Validation failed"
            return 1
        fi
    else
        print_error "validate_setup.py not found"
        return 1
    fi
}

# Run tests
run_tests() {
    print_status "Running unit tests..."
    
    cd "$SCRIPTS_DIR"
    
    if [ -f "test_update_docker_images.py" ]; then
        export PYTHONPATH="$SCRIPTS_DIR:$PYTHONPATH"
        $PYTHON_CMD test_update_docker_images.py
        if [ $? -eq 0 ]; then
            print_success "All tests passed"
        else
            print_warning "Some tests failed"
            return 1
        fi
    else
        print_error "test_update_docker_images.py not found"
        return 1
    fi
}

# Run dry run test
run_dry_run() {
    print_status "Running dry run test..."
    
    cd "$REPO_ROOT"
    
    # Check if GITHUB_TOKEN is set
    if [ -z "$GITHUB_TOKEN" ]; then
        print_warning "GITHUB_TOKEN not set - using dummy token for dry run"
        export GITHUB_TOKEN="dummy-token-for-dry-run"
    fi
    
    export DRY_RUN="true"
    export IMAGE_FILTER=""
    export FORCE_UPDATE="false"
    
    if [ -f ".github/scripts/update_docker_images.py" ]; then
        $PYTHON_CMD .github/scripts/update_docker_images.py
        if [ $? -eq 0 ]; then
            print_success "Dry run completed successfully"
        else
            print_warning "Dry run encountered issues (this may be expected without a real GitHub token)"
        fi
    else
        print_error "update_docker_images.py not found"
        return 1
    fi
}

# Show usage
show_usage() {
    echo "Usage: $0 [command]"
    echo ""
    echo "Commands:"
    echo "  setup     - Install dependencies and validate setup"
    echo "  validate  - Run setup validation only"
    echo "  test      - Run unit tests only"
    echo "  dry-run   - Run a dry-run test of the updater"
    echo "  all       - Run all checks (setup, validate, test, dry-run)"
    echo "  help      - Show this help message"
    echo ""
    echo "Environment variables:"
    echo "  GITHUB_TOKEN          - GitHub token for API access (required for real runs)"
    echo "  RUN_INTEGRATION_TESTS - Set to '1' to run integration tests"
    echo ""
}

# Main execution
main() {
    case "${1:-all}" in
        "setup")
            check_python
            install_dependencies
            run_validation
            ;;
        "validate")
            check_python
            run_validation
            ;;
        "test")
            check_python
            run_tests
            ;;
        "dry-run")
            check_python
            run_dry_run
            ;;
        "all")
            check_python
            install_dependencies
            run_validation
            run_tests
            run_dry_run
            print_success "All checks completed!"
            ;;
        "help"|"-h"|"--help")
            show_usage
            exit 0
            ;;
        *)
            print_error "Unknown command: $1"
            show_usage
            exit 1
            ;;
    esac
}

# Make sure we're in the right directory
cd "$REPO_ROOT"

# Run main function with all arguments
main "$@"
