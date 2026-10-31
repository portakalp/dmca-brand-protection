"""Dramatiq entry module of worker-default; imported by every worker-default process."""

from kalkan.workers.boot import boot_worker

broker = boot_worker("default")
