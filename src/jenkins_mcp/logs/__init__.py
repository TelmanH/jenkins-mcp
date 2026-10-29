"""Console log handling: download + cache, cleaning, stage detection, error grouping."""

from .cache import load_log
from .errors import ErrorGroup, root_cause_candidates
from .parse import ParsedLog, fmt_line, meaningful_tail
from .stages import pick_failed_stage, pipeline_stages, stage_str

__all__ = [
    "ErrorGroup", "ParsedLog", "fmt_line", "load_log", "meaningful_tail",
    "pick_failed_stage", "pipeline_stages", "root_cause_candidates", "stage_str",
]
