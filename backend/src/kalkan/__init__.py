"""Kalkan: brand protection and phishing takedown automation."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("kalkan")
except PackageNotFoundError:  # pragma: no cover - source tree without an installed package
    __version__ = "0.0.0"
