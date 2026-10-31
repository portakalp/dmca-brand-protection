"""Dramatiq entry module of worker-browser; imported by every worker-browser process.

The browser stores no results (K82): its Redis user reaches only the queue keys and the
proxy byte counter, so the Results middleware is left out.
"""

from kalkan.workers.boot import boot_worker

broker = boot_worker("browser", results=False)
