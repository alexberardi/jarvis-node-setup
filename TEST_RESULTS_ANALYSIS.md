# Test Results Analysis Guide

## Overview

The `test_command_parsing.py` suite now writes comprehensive test results to a JSON file for analysis. This allows you to:
- Review all test results (passed and failed) in detail
- Identify which commands are being confused with each other  
- Find parameter extraction issues
- Get actionable recommendations for improving command descriptions
- Track performance across test runs

## Running Tests with Results Output

### Basic Usage

```bash
# Run all tests and write results to test_results.json (default)
python3 test_command_parsing.py

# Specify custom output file
python3 test_command_parsing.py --output my_results.json
python3 test_command_parsing.py -o results_2024-01-15.json

# Run specific commands and save results
python3 test_command_parsing.py -c get_weather get_sports_scores -o weather_sports_results.json

# Run specific test indices
python3 test_command_parsing.py -t 5 7 11 -o specific_tests.json
```

## Results File Structure

The JSON file contains four main sections:

### 1. Summary

High-level statistics about the test run:

```json
{
  "summary": {
    "total_tests": 50,
    "passed": 42,
    "failed": 8,
    "success_rate": 84.0,
    "avg_response_time": 0.523,
    "min_response_time": 0.234,
    "max_response_time": 1.245,
    "slow_tests_count": 3,
    "test_run_timestamp": "2025-11-09 15:30:45"
  }
}
```

### 2. Test Results

Detailed results for every test:

```json
{
  "test_results": [
    {
      "test_number": 0,
      "passed": true,
      "description": "Basic current weather request",
      "voice_command": "What's the weather like?",
      "expected": {
        "command": "get_weather",
        "parameters": {}
      },
      "actual": {
        "command": "get_weather",
        "parameters": {}
      },
      "response_time_seconds": 0.456,
      "conversation_id": "uuid-here",
      "failure_reason": null,
      "full_response": { /* complete API response */ }
    },
    {
      "test_number": 15,
      "passed": false,
      "description": "Sports score with relative date",
      "voice_command": "How did the Giants do yesterday?",
      "expected": {
        "command": "get_sports_scores",
        "parameters": {"team_name": "Giants", "datetimes": ["2025-11-08T00:00:00"]}
      },
      "actual": {
        "command": "get_sports_schedule",
        "parameters": {"team_name": "Giants", "datetimes": ["2025-11-08T00:00:00"]}
      },
      "response_time_seconds": 0.523,
      "conversation_id": "uuid-here",
      "failure_reason": "Command mismatch: expected 'get_sports_scores', got 'get_sports_schedule'",
      "full_response": { /* complete API response */ }
    }
  ]
}
```

### 3. Analysis

Automated analysis identifying patterns and issues:

```json
{
  "analysis": {
    "command_success_rates": {
      "get_sports_scores": {
        "success_rate": 60.0,
        "passed": 6,
        "failed": 4,
        "total": 10
      },
      "get_weather": {
        "success_rate": 95.0,
        "passed": 19,
        "failed": 1,
        "total": 20
      }
    },
    "command_confusion_matrix": {
      "get_sports_scores → get_sports_schedule": 3,
      "get_sports_schedule → get_sports_scores": 2,
      "answer_question → search_web": 1
    },
    "parameter_extraction_issues": {
      "get_weather": [
        {
          "voice_command": "What's the weather tomorrow?",
          "expected_params": {"datetimes": ["2025-11-10T00:00:00"]},
          "actual_params": {},
          "failure_reason": "Missing parameters: datetimes"
        }
      ]
    },
    "recommendations": [
      {
        "priority": "HIGH",
        "command": "get_sports_scores",
        "issue": "Low success rate: 60.0%",
        "suggestion": "Review and improve command description. Consider adding more specific use cases and anti-patterns to distinguish from similar commands."
      },
      {
        "priority": "MEDIUM",
        "command": "get_sports_scores",
        "issue": "Confused with 'get_sports_schedule' 3 time(s)",
        "suggestion": "Add explicit anti-pattern in 'get_sports_scores' description: 'Do NOT use for [actual command use case]. Use get_sports_schedule instead.'"
      }
    ]
  }
}
```

### 4. Slow Tests

Tests that took longer than 2 seconds:

```json
{
  "slow_tests": [
    {
      "test_number": 23,
      "description": "Complex calendar query",
      "voice_command": "What meetings do I have next week?",
      "response_time": 2.345,
      "conversation_id": "uuid-here",
      "passed": true
    }
  ]
}
```

## Analyzing Results

### Using the Analysis Section

The `analysis` section automatically identifies:

1. **Command Success Rates** - Sorted by lowest success rate first to highlight problems
2. **Command Confusion Matrix** - Shows which commands are being selected instead of the expected ones
3. **Parameter Extraction Issues** - Lists failures where the command was correct but parameters were wrong
4. **Recommendations** - Actionable suggestions prioritized by severity

### Workflow for Improving Descriptions

1. **Run the test suite:**
   ```bash
   python3 test_command_parsing.py -o results.json
   ```

2. **Review the analysis section** in `results.json`:
   - Check `command_success_rates` for low-performing commands (<70%)
   - Review `command_confusion_matrix` for common misidentifications
   - Examine `parameter_extraction_issues` for parameter problems

3. **Follow the recommendations:**
   - HIGH priority: Commands with <70% success rate need immediate attention
   - MEDIUM priority: Address common confusions and parameter issues

4. **Update command descriptions** based on findings:
   - Add anti-patterns to distinguish confused commands
   - Improve parameter descriptions with more examples
   - Clarify use cases and scope

5. **Re-run tests** to validate improvements:
   ```bash
   python3 test_command_parsing.py -o results_after_fix.json
   ```

6. **Compare results** to track improvement

### Example: Fixing Command Confusion

**Problem identified in results:**
```json
"command_confusion_matrix": {
  "get_sports_scores → get_sports_schedule": 5
}
```

**Analysis:** The LLM is selecting `get_sports_schedule` when it should select `get_sports_scores` 5 times.

**Solution:** Update `get_sports_scores` description:

```python
# Before
description = "Get sports scores for past games"

# After  
description = "Get sports scores and game results for completed/past games. Use this for questions about how teams performed, final scores, who won/lost, or results from games that already happened (past or today). Do NOT use for: upcoming games or schedules (use get_sports_schedule), live/in-progress game updates, player statistics, or team standings."
```

**Re-test** and verify confusion is reduced.

### Example: Fixing Parameter Extraction

**Problem identified in results:**
```json
"parameter_extraction_issues": {
  "get_weather": [
    {
      "voice_command": "What's the weather tomorrow?",
      "expected_params": {"datetimes": ["2025-11-10T00:00:00"]},
      "actual_params": {},
      "failure_reason": "Missing parameters: datetimes"
    }
  ]
}
```

**Analysis:** The LLM isn't extracting the `datetimes` parameter for forecast requests.

**Solution:** Update parameter description:

```python
# Before
description="Array of ISO datetime strings for weather forecast."

# After
description="Array of ISO datetime strings for weather forecast dates (e.g., ['2025-11-10T00:00:00', '2025-11-11T00:00:00']). Omit this parameter for current weather conditions. Include dates for forecasts. Maximum 5 days in the future."
```

## Using Results Programmatically

You can also load and analyze results programmatically:

```python
import json

# Load results
with open('test_results.json', 'r') as f:
    results = json.load(f)

# Find all failures
failures = [t for t in results['test_results'] if not t['passed']]
print(f"Found {len(failures)} failures")

# Group failures by expected command
by_command = {}
for test in failures:
    cmd = test['expected']['command']
    by_command[cmd] = by_command.get(cmd, []) + [test]

for cmd, tests in by_command.items():
    print(f"\n{cmd}: {len(tests)} failures")
    for test in tests:
        print(f"  - {test['voice_command']}")
        print(f"    Got: {test['actual']['command']}")

# Check for slow tests
slow = results['slow_tests']
if slow:
    print(f"\n{len(slow)} slow tests (>2s):")
    for test in slow:
        print(f"  - #{test['test_number']}: {test['response_time']:.2f}s")
```

## Comparing Test Runs

To track improvements over time:

```bash
# Before improvements
python3 test_command_parsing.py -o results_before.json

# After improvements
python3 test_command_parsing.py -o results_after.json

# Compare
python3 << 'EOF'
import json

with open('results_before.json') as f:
    before = json.load(f)
with open('results_after.json') as f:
    after = json.load(f)

print(f"Success rate: {before['summary']['success_rate']}% → {after['summary']['success_rate']}%")
print(f"Failures: {before['summary']['failed']} → {after['summary']['failed']}")

# Compare per-command success rates
for cmd in before['analysis']['command_success_rates']:
    before_rate = before['analysis']['command_success_rates'][cmd]['success_rate']
    after_rate = after['analysis']['command_success_rates'].get(cmd, {}).get('success_rate', 0)
    if after_rate != before_rate:
        change = "📈" if after_rate > before_rate else "📉"
        print(f"{change} {cmd}: {before_rate}% → {after_rate}%")
EOF
```

## Tips

1. **Run tests after every description change** to see immediate impact
2. **Focus on HIGH priority recommendations first** (commands with <70% success)
3. **Address command confusion systematically** - start with most common confusions
4. **Use specific test indices** (-t flag) to test specific scenarios during iteration
5. **Keep historical results** to track progress over time
6. **Check full_response** in failed tests to understand what the LLM actually returned
7. **Compare parameter values** carefully - sometimes the format is slightly different (string vs number, date format, etc.)

## Success Metrics

Target metrics for a well-tuned command set:

- **Overall success rate:** >90%
- **Per-command success rate:** >80%
- **Command confusion incidents:** <5% of total tests
- **Parameter extraction accuracy:** >95% when command is correct
- **Average response time:** <1 second

## Next Steps After Analysis

Once you've analyzed results and made improvements:

1. Update command descriptions based on recommendations
2. Re-run test suite to validate improvements
3. Document changes in COMMAND_DESCRIPTIONS_UPDATED.md
4. Test with real voice commands to ensure improvements work in practice
5. Monitor production usage (once deployed) to identify edge cases not in test suite

## File Maintenance

- Keep at least the last 3-5 test result files for comparison
- Name files with timestamps or version numbers for tracking
- Add custom test cases for edge cases discovered in production
- Review and update expected parameters as commands evolve

