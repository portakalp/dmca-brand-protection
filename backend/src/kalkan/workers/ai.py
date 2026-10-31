"""Dramatiq entry module of worker-ai; imported by every worker-ai process."""

from kalkan.workers.boot import boot_worker

broker = boot_worker("ai")
