"""Powerful scraper pipeline: search -> validate -> fetch -> extract -> persist."""
from .pipeline import PipelineResult, scrape
from .targets import TARGETS, TargetSpec, all_target_names, get

__all__ = ["scrape", "PipelineResult", "TARGETS", "TargetSpec", "get", "all_target_names"]
