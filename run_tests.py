#!/usr/bin/env python3
"""
Test runner for Jarvis Node Setup
"""

import sys
import os
import unittest
import argparse
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))


def run_all_tests():
    """Run all tests"""
    print("🧪 Running all Jarvis Node Setup tests...\n")
    
    # Discover and run all tests
    loader = unittest.TestLoader()
    start_dir = str(project_root / "tests")
    suite = loader.discover(start_dir, pattern="test_*.py")
    
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    
    return result.wasSuccessful()


def run_specific_test(test_name):
    """Run a specific test file or test case"""
    print(f"🧪 Running specific test: {test_name}\n")
    
    if test_name.endswith('.py'):
        # Run specific test file
        test_file = project_root / "tests" / test_name
        if not test_file.exists():
            print(f"❌ Test file not found: {test_file}")
            return False
        
        loader = unittest.TestLoader()
        suite = loader.discover(str(test_file.parent), pattern=test_file.name)
    else:
        # Run specific test case
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromName(test_name)
    
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    
    return result.wasSuccessful()


def list_tests():
    """List all available tests"""
    print("📋 Available tests:\n")
    
    test_dir = project_root / "tests"
    if not test_dir.exists():
        print("❌ No tests directory found")
        return
    
    for test_file in test_dir.glob("test_*.py"):
        print(f"  📄 {test_file.name}")
        
        # Just show the file name for now
        print(f"    └─ (Test cases will be discovered at runtime)")


def main():
    parser = argparse.ArgumentParser(description="Run Jarvis Node Setup tests")
    parser.add_argument(
        "--test", "-t",
        help="Run specific test file (e.g., test_config_service.py) or test case"
    )
    parser.add_argument(
        "--list", "-l",
        action="store_true",
        help="List all available tests"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Verbose output"
    )
    
    args = parser.parse_args()
    
    if args.verbose:
        print("🔍 Verbose mode enabled")
    
    if args.list:
        list_tests()
        return
    
    if args.test:
        success = run_specific_test(args.test)
    else:
        success = run_all_tests()
    
    if success:
        print("\n✅ All tests passed!")
        sys.exit(0)
    else:
        print("\n❌ Some tests failed!")
        sys.exit(1)


if __name__ == "__main__":
    main() 