from __future__ import annotations

import base64
import html

from .models import GraphicIR, GraphicJob, ImageNode, ShapeNode, TextNode


def _number(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def compile_svg(ir: GraphicIR, job: GraphicJob) -> bytes:
    canvas = ir.canvas
    out = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{canvas.width}" height="{canvas.height}" '
            f'viewBox="0 0 {canvas.width} {canvas.height}" role="img" '
            f'data-template="{html.escape(ir.template, quote=True)}" '
            f'data-overflow-policy="{html.escape(ir.overflow_policy, quote=True)}">'
        )
    ]
    for node in ir.nodes:
        if isinstance(node, ShapeNode):
            if node.shape == "circle":
                out.append(
                    f'<circle id="{html.escape(node.node_id, quote=True)}" '
                    f'cx="{_number(node.x + node.width / 2)}" cy="{_number(node.y + node.height / 2)}" '
                    f'r="{_number(min(node.width, node.height) / 2)}" fill="{node.fill}" opacity="{_number(node.opacity)}"/>'
                )
            else:
                out.append(
                    f'<rect id="{html.escape(node.node_id, quote=True)}" x="{_number(node.x)}" y="{_number(node.y)}" '
                    f'width="{_number(node.width)}" height="{_number(node.height)}" rx="{_number(node.radius)}" '
                    f'fill="{node.fill}" opacity="{_number(node.opacity)}"/>'
                )
        elif isinstance(node, TextNode):
            x = node.box_x + node.box_width / 2 if node.anchor == "middle" else node.box_x + node.box_width if node.anchor == "end" else node.box_x
            anchor = node.anchor
            out.append(
                f'<text id="{html.escape(node.node_id, quote=True)}" '
                f'data-source-text="{html.escape(node.source_text, quote=True)}" '
                f'data-box-x="{_number(node.box_x)}" data-box-y="{_number(node.box_y)}" '
                f'data-box-width="{_number(node.box_width)}" data-box-height="{_number(node.box_height)}" '
                f'x="{_number(x)}" y="{_number(node.box_y + node.font_size)}" '
                f'font-family="{html.escape(ir.brand.font_family, quote=True)}" '
                f'font-size="{node.font_size}" font-weight="{node.weight}" fill="{node.color}" '
                f'text-anchor="{anchor}" xml:space="preserve">'
            )
            for index, line in enumerate(node.lines):
                baseline = node.box_y + node.font_size + index * node.line_height
                out.append(f'<tspan x="{_number(x)}" y="{_number(baseline)}">{html.escape(line)}</tspan>')
            out.append("</text>")
        elif isinstance(node, ImageNode):
            asset = job.assets[node.asset_id]
            data = base64.b64encode(asset.data).decode("ascii")
            out.append(
                f'<image id="{html.escape(node.node_id, quote=True)}" x="{_number(node.x)}" y="{_number(node.y)}" '
                f'width="{_number(node.width)}" height="{_number(node.height)}" '
                f'href="data:{asset.mime_type};base64,{data}" preserveAspectRatio="xMidYMid slice"/>'
            )
    out.append("</svg>")
    return "".join(out).encode("utf-8")
