"""Project review presets shared by local host control adapters."""

PRESETS = {
    "minimal": {"max_calls_per_day": 5, "max_calls_per_batch": 1, "max_batch_seconds": 150, "max_run_seconds": 180},
    "light": {"max_calls_per_day": 15, "max_calls_per_batch": 2, "max_batch_seconds": 180, "max_run_seconds": 300},
    "balanced": {"max_calls_per_day": 100, "max_calls_per_batch": 5, "max_batch_seconds": 300, "max_run_seconds": 600},
    "active": {"max_calls_per_day": 300, "max_calls_per_batch": 10, "max_batch_seconds": 600, "max_run_seconds": 1200},
}
