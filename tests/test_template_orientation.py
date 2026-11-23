"""Portrait design coordinates preserve physical tape geometry and saved jobs."""

import base64
import copy
import io
from dataclasses import asdict

import pytest
import zxingcpp
from conftest import profile
from PIL import Image, ImageOps
from pydantic import ValidationError
from test_access_security import accounts as account_fixture

from app.models import LabelCopy, PrintJob
from app.render import RenderSpec, render_document, render_job
from app.templates.logic_source import compile_source
from app.templates.template_dimensions import template_dimensions
from app.templates.template_logic import logic_fields, logic_targets
from app.templates.template_schema import TemplateDocument

accounts = account_fixture


def document(**changes):
    return TemplateDocument.model_validate(
        {
            "orientation": "vertical",
            "width_mm": 24,
            "length_mm": 60,
            "elements": [
                {
                    "id": "box",
                    "kind": "rectangle",
                    "x": 4,
                    "y": 10,
                    "width": 5,
                    "height": 8,
                    "filled": True,
                }
            ],
            **changes,
        }
    )


def physical_raster(doc, spec):
    job = PrintJob(
        width_mm=doc.width_mm,
        length_mm=doc.length_mm,
        payload={},
        template_snapshot={"document": doc.model_dump(), "render_spec": asdict(spec)},
    )
    return render_job(job, LabelCopy(id="copy"))


@pytest.mark.parametrize("dpi_x,dpi_y", [(180, 180), (360, 180), (300, 600)])
def test_portrait_preview_and_printer_raster_have_correct_independent_axes(dpi_x, dpi_y):
    doc = document()
    spec = RenderSpec(dpi=dpi_y, dpi_x=dpi_x, stretch_canvas=True)
    original = doc.model_dump()
    preview = render_document(doc, {}, spec)
    assert preview.image.size == (round(24 * dpi_y / 25.4), round(60 * dpi_x / 25.4))
    assert 'viewBox="0 0 24.0 60.0"' in preview.svg
    assert doc.model_dump() == original
    raster = physical_raster(doc, spec)
    assert (raster.pixel_width, raster.pixel_height) == (
        round(60 * dpi_x / 25.4),
        round(24 * dpi_y / 25.4),
    )
    assert (raster.length_mm, raster.tape_width_mm, raster.dpi_x, raster.dpi_y) == (
        60,
        24,
        dpi_x,
        dpi_y,
    )
    ink = Image.frombytes("1", (raster.pixel_width, raster.pixel_height), raster.raster)
    # A clockwise quarter turn maps the authored box to physical X=42..50, Y=4..9.
    assert ink.getbbox() == pytest.approx(
        (42 * dpi_x / 25.4, 4 * dpi_y / 25.4, 50 * dpi_x / 25.4, 9 * dpi_y / 25.4), abs=1
    )


def test_legacy_templates_default_to_identical_horizontal_output():
    data = document().model_dump()
    data.pop("orientation")
    legacy = TemplateDocument.model_validate(data)
    assert legacy.orientation == "horizontal"
    explicit = TemplateDocument.model_validate({**data, "orientation": "horizontal"})
    spec = RenderSpec(dpi_x=360)
    assert (
        render_document(legacy, {}, spec).image.tobytes()
        == render_document(explicit, {}, spec).image.tobytes()
    )
    assert physical_raster(legacy, spec).raster == physical_raster(explicit, spec).raster


def test_vertical_automatic_length_follows_bottom_edges_and_distance_holders():
    doc = document(auto_length=True)
    doc.elements.append(doc.elements[0].model_copy(update={"id": "second", "y": 2, "height": 7}))
    values = doc.model_dump()
    values["elements"][1]["distance_holder"] = {
        "element_id": "box",
        "axis": "vertical",
        "gap_mm": 3,
    }
    doc = TemplateDocument.model_validate(values)
    result = render_document(doc, {}, RenderSpec())
    assert result.document.elements[1].y == 21
    assert result.document.length_mm == 29
    assert result.document.elements[1].distance_holder is None
    assert template_dimensions(doc) == {"variable_length": False, "length_mm": 29}
    assert result.image.height > result.image.width


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_portrait_auto_text_width_only_changes_length_when_projected_vertically(rotation):
    doc = document(
        auto_length=True,
        out_of_bounds="clip",
        fields=[{"name": "text", "label": "Text"}],
        elements=[
            {
                "id": "text",
                "kind": "text",
                "x": 4,
                "y": 25,
                "width": 8,
                "height": 8,
                "auto_width": True,
                "content": "{{text}}",
                "rotation": rotation,
            }
        ],
    )
    variable = rotation in (90, 270)
    dimensions = template_dimensions(doc)
    assert dimensions["variable_length"] == variable
    lengths = [
        render_document(doc, {"text": text}, RenderSpec()).document.length_mm
        for text in ("A", "Longer text")
    ]
    assert (lengths[0] != lengths[1]) == variable
    if not variable:
        assert dimensions["length_mm"] == lengths[0]


def test_vertical_logic_can_position_elements_beyond_the_old_tape_width_limit():
    data = document(auto_length=True).model_dump()
    data["version"] = 2
    data["elements"][0]["logic_name"] = "box"
    doc = TemplateDocument.model_validate(data)
    doc.logic = compile_source(
        "box.y = 140; box.height = 120;", logic_fields(doc), logic_targets(doc)
    )
    result = render_document(doc, {}, RenderSpec())
    assert result.document.elements[0].y == 140
    assert result.document.elements[0].height == 120
    assert result.document.length_mm == 261
    frozen = render_document(result.document, {}, RenderSpec())
    assert frozen.image.tobytes() == result.image.tobytes()


def test_portrait_clips_across_tape_width_and_checks_both_canvas_axes():
    spec = RenderSpec(printable_widths_mm={"24": 18.1}, align_printable_band=True)
    doc = document(
        elements=[
            {
                "id": "edge",
                "kind": "rectangle",
                "x": 0,
                "y": 0,
                "width": 24,
                "height": 60,
                "filled": True,
            }
        ]
    )
    with pytest.raises(ValueError, match="outside"):
        render_document(doc, {}, spec)
    doc.out_of_bounds = "clip"
    result = render_document(doc, {}, spec)
    assert any("clipped" in warning for warning in result.warnings)
    raster = physical_raster(doc, spec)
    bounds = Image.frombytes(
        "1", (raster.pixel_width, raster.pixel_height), raster.raster
    ).getbbox()
    rows = round(18.1 * 180 / 25.4)
    top = (raster.pixel_height - rows) // 2
    assert bounds is not None
    assert bounds[1:4:2] == (top, top + rows)
    # A rejected layout must not affect a subsequent valid render.
    assert not render_document(document(), {}, spec).warnings


def test_vertical_length_limits_and_invalid_orientation_are_rejected():
    for orientation in (None, True, "diagonal", "<script>", 90):
        with pytest.raises(ValidationError):
            document(orientation=orientation)
    for values in ({"y": 1001}, {"height": 1001}, {"y": float("inf")}):
        data = document().model_dump()
        data["elements"][0].update(values)
        with pytest.raises(ValidationError):
            TemplateDocument.model_validate(data)
    doc = document(auto_length=True)
    doc.elements[0].y = 295
    with pytest.raises(ValueError, match="300"):
        render_document(doc, {}, RenderSpec())


def test_portrait_qr_stays_decodable_with_asymmetric_printer_resolution():
    doc = document(
        elements=[
            {
                "id": "qr",
                "kind": "qr",
                "x": 3,
                "y": 20,
                "width": 18,
                "height": 18,
                "content": "https://example.org/portrait",
            }
        ]
    )
    raster = physical_raster(doc, RenderSpec(dpi=180, dpi_x=360, stretch_canvas=True))
    ink = Image.frombytes("1", (raster.pixel_width, raster.pixel_height), raster.raster)
    image = ImageOps.invert(ink.convert("L")).resize((raster.pixel_width, raster.pixel_height * 2))
    decoded = zxingcpp.read_barcode(image)
    assert decoded and decoded.text == "https://example.org/portrait"


def test_vertical_fixed_stock_keeps_physical_media_dimensions_and_rejects_auto_length():
    doc = document(
        width_mm=29, length_mm=90, media={"media_family": "fixed_label", "label_length_mm": 90}
    )
    spec = RenderSpec(
        media_family="fixed_label", media_kind="fixed_label", fixed_label_sizes_mm=((29, 90),)
    )
    result = render_document(doc, {}, spec)
    assert (result.document.canvas_width_mm, result.document.canvas_height_mm) == (29, 90)
    raster = physical_raster(doc, spec)
    assert (raster.tape_width_mm, raster.length_mm) == (29, 90)
    with pytest.raises(ValidationError):
        TemplateDocument.model_validate({**doc.model_dump(), "auto_length": True})
    with pytest.raises(ValueError, match="fixed-label size"):
        render_document(
            doc,
            {},
            RenderSpec(
                media_family="fixed_label",
                media_kind="fixed_label",
                fixed_label_sizes_mm=((90, 29),),
            ),
        )


def test_csv_row_previews_keep_the_frozen_portrait_orientation(client):
    values = document().model_dump()
    values["elements"] = [
        {
            "id": "caption",
            "kind": "text",
            "x": 4,
            "y": 25,
            "width": 16,
            "height": 8,
            "content": "{{title}}",
        }
    ]
    values["fields"] = [{"name": "title", "label": "Title", "sample": "Shelf"}]
    saved = client.post("/api/templates", json={"name": "CSV portrait", "document": values}).json()
    tape = profile(client)
    prepared = client.post(
        "/api/runs/prepare",
        json={
            "request_key": "portrait-run",
            "name": "Portraits",
            "template": saved["id"],
            "expected_revision": 1,
            "tape_profile": tape["id"],
            "csv_text": "Title\nShelf\n",
            "mapping": {"title": "Title"},
            "rows": [{"source_row": 2, "copies": 1}],
        },
    )
    assert prepared.status_code == 200, prepared.text
    assert prepared.json()["valid"]
    run = prepared.json()["run"]
    path = f"/api/runs/{run['id']}/rows/0/preview"
    preview = client.get(path).json()
    assert preview["orientation"] == "vertical"
    image = Image.open(io.BytesIO(base64.b64decode(preview["png"].split(",")[1])))
    assert image.height > image.width
    values["orientation"] = "horizontal"
    values["elements"][0]["y"] = 4
    assert (
        client.put(
            f"/api/templates/{saved['id']}",
            json={
                "name": saved["name"],
                "document": values,
                "expected_revision": 1,
            },
        ).status_code
        == 200
    )
    assert client.get(path).json() == preview


def test_orientation_is_per_template_revision_and_queued_snapshot(client):
    response = client.post(
        "/api/templates", json={"name": "Portrait", "document": document().model_dump()}
    )
    assert response.status_code == 201, response.text
    saved = response.json()
    path = f"/api/templates/{saved['id']}"
    tape = profile(client)
    job = client.post(
        "/api/jobs", json={"template": saved["id"], "tape_profile": tape["id"], "data": {}}
    )
    assert job.status_code == 201, job.text
    job_path = f"/api/jobs/{job.json()['id']}"
    frozen = client.get(job_path).json()["template_snapshot"]
    assert frozen["document"]["orientation"] == "vertical"
    preview = client.post("/api/preview", json={"template_id": saved["id"]}).json()
    assert preview["orientation"] == "vertical"
    assert preview["pixel_height"] > preview["pixel_width"]
    thumb = client.get(f"{path}/preview.webp")
    assert thumb.status_code == 200, thumb.text
    image = Image.open(io.BytesIO(thumb.content))
    assert image.height > image.width
    changed = copy.deepcopy(saved["document"])
    changed["orientation"] = "horizontal"
    response = client.put(
        path, json={"name": "Landscape", "document": changed, "expected_revision": 1}
    )
    assert response.status_code == 200, response.text
    assert response.json()["document"]["elements"] == saved["document"]["elements"]
    history_preview = client.get(f"{path}/revisions/1/preview").json()
    assert history_preview["orientation"] == "vertical"
    historic_image = Image.open(io.BytesIO(base64.b64decode(history_preview["png"].split(",")[1])))
    assert historic_image.height > historic_image.width
    assert client.get(job_path).json()["template_snapshot"] == frozen
    assert (
        client.post(f"{path}/revisions/1/restore", json={"expected_revision": 2}).status_code == 200
    )
    assert client.get(path).json()["document"]["orientation"] == "vertical"
    other = client.post(
        "/api/templates", json={"name": "Other", "document": {"elements": []}}
    ).json()
    assert other["document"]["orientation"] == "horizontal"


def test_unauthorized_or_invalid_orientation_changes_leave_template_untouched(accounts):
    _, alice, bob = accounts
    saved = alice.post(
        "/api/templates", json={"name": "Private", "document": document().model_dump()}
    ).json()
    path = f"/api/templates/{saved['id']}"
    payload = {
        "name": saved["name"],
        "scope": "personal",
        "document": {**saved["document"], "orientation": "horizontal"},
        "expected_revision": 1,
    }
    assert bob.put(path, json=payload).status_code == 403
    payload["document"]["orientation"] = "invalid"
    assert alice.put(path, json=payload).status_code == 422
    assert alice.get(path).json()["revision"] == 1
    payload["document"]["orientation"] = "horizontal"
    assert alice.put(path, json=payload).status_code == 200
