"""Reusable helper to run a per-item function across many items concurrently.

Kept UI-agnostic so any tab/command can fan work out over a thread pool. The
worker function is responsible for its own thread-safety (e.g. marshalling
tkinter updates back to the UI thread via ``self.after``).
"""

from concurrent.futures import ThreadPoolExecutor

# Cap concurrency so we never spawn an unbounded number of git/dotnet processes.
DEFAULT_MAX_WORKERS = 8


def run_in_parallel(items, worker_fn, max_workers=None):
    """Call *worker_fn(item)* for every item concurrently and wait for all.

    Returns the results in the same order as *items*. Blocks until every worker
    has finished. Exceptions raised inside a worker propagate to the caller.
    """
    items = list(items)
    if not items:
        return []
    if max_workers is None:
        max_workers = min(DEFAULT_MAX_WORKERS, len(items))
    results = [None] * len(items)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(worker_fn, item): i
                   for i, item in enumerate(items)}
        for future, index in futures.items():
            results[index] = future.result()
    return results
