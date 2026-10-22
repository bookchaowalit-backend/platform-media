from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Canvas:
    variant: str
    width: int
    height: int


@dataclass(frozen=True)
class BrandTokens:
    colors: Mapping[str, str]
    font_family: str


@dataclass(frozen=True)
class RasterAsset:
    asset_id: str
    path: Path
    mime_type: str
    sha256: str
    data: bytes
    width: int
    height: int


@dataclass(frozen=True)
class GraphicJob:
    job_id: str
    template: str
    brand: BrandTokens
    variants: tuple[str, ...]
    records: tuple[Mapping[str, str], ...]
    assets: Mapping[str, RasterAsset]
    spec_path: Path


@dataclass(frozen=True)
class ShapeNode:
    node_id: str
    shape: str
    x: float
    y: float
    width: float
    height: float
    fill: str
    radius: float = 0
    opacity: float = 1


@dataclass(frozen=True)
class TextNode:
    node_id: str
    source_text: str
    lines: tuple[str, ...]
    box_x: float
    box_y: float
    box_width: float
    box_height: float
    font_size: int
    line_height: float
    color: str
    anchor: str = "start"
    weight: int = 400


@dataclass(frozen=True)
class ImageNode:
    node_id: str
    asset_id: str
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class GraphicIR:
    canvas: Canvas
    template: str
    brand: BrandTokens
    nodes: tuple[ShapeNode | TextNode | ImageNode, ...]
    overflow_policy: str = "auto-fit-or-reject"


@dataclass(frozen=True)
class Artifact:
    record_id: str
    variant: str
    role: str
    path: str
    sha256: str
    bytes: int
    width: int
    height: int


@dataclass(frozen=True)
class BatchResult:
    status: str
    job_id: str
    output_count: int
    model_calls: int
    monetary_cost: None
    manifest_path: Path
    fresh_wall_seconds: float
    replay_wall_seconds: float
