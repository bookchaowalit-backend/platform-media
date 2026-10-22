"""Structured, deterministic graphics template batch product."""

from .errors import ArtifactIntegrityError, GraphicsError
from .inputs import VARIANTS, load_job, variant_dimensions
from .models import BatchResult, GraphicIR, GraphicJob
from .service import render_batch
from .templates import compile_graphic, fit_text

__all__ = [
    "VARIANTS",
    "ArtifactIntegrityError",
    "BatchResult",
    "GraphicIR",
    "GraphicJob",
    "GraphicsError",
    "compile_graphic",
    "fit_text",
    "load_job",
    "render_batch",
    "variant_dimensions",
]
