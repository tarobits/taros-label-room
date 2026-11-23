import pytest
import zxingcpp
from PIL import Image, ImageOps

from app.barcodes import encode_barcode
from app.i18n import translate
from app.render import RenderSpec, render_document
from app.templates.logic_source import compile_source
from app.templates.template_logic import logic_fields, logic_targets
from app.templates.template_schema import TemplateDocument


def compact_document(**changes):
    return TemplateDocument.model_validate(
        {
            "width_mm": 24,
            "length_mm": 60,
            "elements": [
                {
                    "id": "code",
                    "kind": "barcode",
                    "content": "42",
                    "x": 10,
                    "y": 5,
                    "width": 20,
                    "height": 10,
                    "barcode_caption": False,
                    "barcode_quiet_zone_in_box": False,
                    **changes,
                }
            ],
        }
    )


def quiet_warnings(output):
    return [
        warning
        for warning in output.warnings
        if getattr(warning, "key", "") == "domainMessages.barcodeQuietZoneObstructed"
    ]


def test_new_barcode_fills_its_box_and_can_be_narrower_than_the_legacy_box():
    doc = compact_document(width=14)
    result = render_document(doc, {}, RenderSpec())
    bounds = ImageOps.invert(result.image.convert("L")).getbbox()
    assert bounds is not None
    assert bounds[0] == pytest.approx(10 * 180 / 25.4, abs=1)
    assert bounds[2] == pytest.approx(24 * 180 / 25.4, abs=1)
    assert not quiet_warnings(result)
    decoded = zxingcpp.read_barcode(result.image)
    assert decoded and decoded.text == "42"
    doc.elements[0].barcode_quiet_zone_in_box = True
    with pytest.raises(ValueError, match="too narrow"):
        render_document(doc, {}, RenderSpec())


def test_quiet_zone_checks_actual_ink_and_recovers_when_obstruction_is_removed():
    doc = compact_document()
    # Outside the bars' box, but inside its left scanning margin.
    obstruction = {
        "id": "block",
        "kind": "rectangle",
        "x": 7,
        "y": 6,
        "width": 1,
        "height": 5,
        "filled": True,
    }
    doc = TemplateDocument.model_validate(
        {**doc.model_dump(), "elements": [*doc.model_dump()["elements"], obstruction]}
    )
    assert len(quiet_warnings(render_document(doc, {}, RenderSpec()))) == 1
    doc.elements[1].visible = False
    assert not quiet_warnings(render_document(doc, {}, RenderSpec()))
    doc.elements[1].visible = True
    doc.elements[1].filled = False
    doc.elements[1].x, doc.elements[1].y = 3, 3
    doc.elements[1].width, doc.elements[1].height = 34, 14
    # An enclosing outline has overlapping bounds but leaves both margins clear.
    assert not quiet_warnings(render_document(doc, {}, RenderSpec()))


def test_label_edge_warns_but_does_not_prevent_rendering():
    output = render_document(compact_document(x=1), {}, RenderSpec())
    assert len(quiet_warnings(output)) == 1
    assert output.image.size == (425, 170)


@pytest.mark.parametrize("orientation", ["horizontal", "vertical"])
@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("dpi_x", [180, 360])
def test_tight_barcodes_decode_and_check_rotated_quiet_zones(orientation, rotation, dpi_x):
    doc = compact_document(x=25, y=25, rotation=rotation)
    doc.width_mm, doc.length_mm, doc.orientation = 80, 100, orientation
    spec = RenderSpec(dpi_x=dpi_x, stretch_canvas=True)
    result = render_document(doc, {}, spec)
    assert not quiet_warnings(result)
    image = result.image
    if dpi_x == 360:
        image = image.resize(
            (
                image.width * (2 if orientation == "vertical" else 1),
                image.height * (2 if orientation == "horizontal" else 1),
            ),
            Image.Resampling.NEAREST,
        )
    decoded = zxingcpp.read_barcode(image)
    assert decoded and decoded.text == "42"
    # Local strip at x=-2 becomes left, top, right, or bottom after rotation.
    x, y = {0: (23, 28), 90: (34, 18), 180: (46, 28), 270: (34, 41)}[rotation]
    raw = doc.model_dump()
    raw["elements"].append(
        {
            "id": "obstruction",
            "kind": "rectangle",
            "filled": True,
            "x": x,
            "y": y,
            "width": 1,
            "height": 1,
        }
    )
    blocked = TemplateDocument.model_validate(raw)
    assert len(quiet_warnings(render_document(blocked, {}, spec))) == 1


@pytest.mark.parametrize(
    "format,content",
    [
        ("code128", "42"),
        ("code39", "ABC"),
        ("ean8", "9638507"),
        ("ean13", "590123412345"),
    ],
)
def test_tight_barcode_minimum_boundary_and_legacy_raster(format, content):
    pattern, _ = encode_barcode(format, content)
    minimum = len(pattern) * 2 * 25.4 / 180
    doc = compact_document(width=minimum, barcode_format=format, content=content)
    doc.length_mm = 100.0
    output = render_document(doc, {}, RenderSpec(stretch_canvas=True))
    assert not quiet_warnings(output)
    decoded = zxingcpp.read_barcode(output.image)
    assert decoded is not None
    doc.elements[0].width = minimum - 0.01
    with pytest.raises(ValueError, match="too narrow"):
        render_document(doc, {}, RenderSpec())
    doc.elements[0].width = 70
    doc.elements[0].barcode_quiet_zone_in_box = True
    legacy = doc.model_dump()
    legacy["elements"][0].pop("barcode_quiet_zone_in_box")
    before = render_document(TemplateDocument.model_validate(legacy), {}, RenderSpec())
    after = render_document(doc, {}, RenderSpec())
    assert before.svg == after.svg
    assert before.image.tobytes() == after.image.tobytes()


def test_quiet_margin_warning_is_localized_and_advisory_for_saved_jobs(client):
    doc = compact_document(x=1)
    output = render_document(doc, {}, RenderSpec())
    warning = quiet_warnings(output)[0]
    assert "Ruhezonen" in translate(warning, "de")
    response = client.post(
        "/api/preview", json={"document": doc.model_dump()}, headers={"Accept-Language": "de"}
    )
    assert response.status_code == 200
    assert any("Ruhezonen" in warning for warning in response.json()["warnings"])
    saved = client.post(
        "/api/templates", json={"name": "Tight barcode", "document": doc.model_dump()}
    )
    assert saved.status_code == 201
    from conftest import profile

    tape = profile(client)
    queued = client.post(
        "/api/jobs", json={"template": saved.json()["id"], "tape_profile": tape["id"], "data": {}}
    )
    assert queued.status_code == 201, queued.text
    job = client.get(f"/api/jobs/{queued.json()['id']}").json()
    assert job["template_snapshot"]["document"]["elements"][0]["barcode_quiet_zone_in_box"] is False


def test_csv_quiet_warnings_remain_localizable_after_reload(client):
    from conftest import profile

    saved = client.post(
        "/api/templates",
        json={
            "name": "Barcode run",
            "document": compact_document(x=1).model_dump(),
        },
    ).json()
    tape = profile(client)
    prepared = client.post(
        "/api/runs/prepare",
        json={
            "request_key": "barcode-warning",
            "name": "Barcodes",
            "template": saved["id"],
            "expected_revision": 1,
            "tape_profile": tape["id"],
            "csv_text": "Unused\n1\n",
            "mapping": {},
            "rows": [{"source_row": 2, "copies": 1}],
        },
    )
    assert prepared.status_code == 200, prepared.text
    run = prepared.json()["run"]
    assert any("scanning margins" in warning for warning in run["warnings"])
    for path in (f"/api/runs/{run['id']}", f"/api/runs/{run['id']}/rows/0/preview"):
        result = client.get(path, headers={"Accept-Language": "de"})
        assert result.status_code == 200
        warnings = result.json()["warnings"]
        assert len(warnings) == 1
        assert "Ruhezonen" in warnings[0]
    # Changing the saved template must not change the frozen run's warnings.
    doc = compact_document(x=10)
    assert (
        client.put(
            f"/api/templates/{saved['id']}",
            json={
                "name": "Changed",
                "document": doc.model_dump(),
                "expected_revision": 1,
            },
        ).status_code
        == 200
    )
    assert (
        "Ruhezonen"
        in client.get(
            f"/api/runs/{run['id']}",
            headers={
                "Accept-Language": "de",
            },
        ).json()["warnings"][0]
    )


@pytest.mark.parametrize(
    "kind,format,content,expected",
    [
        ("barcode", "code128", "Inventory-42", "Inventory-42"),
        ("barcode", "code39", "ABC-42", "ABC-42"),
        ("barcode", "ean13", "590123412345", "5901234123457"),
        ("barcode", "ean8", "9638507", "96385074"),
        ("datamatrix", "code128", "Data Matrix 42", "Data Matrix 42"),
        ("aztec", "code128", "Aztec 42", "Aztec 42"),
    ],
)
@pytest.mark.parametrize("rotation,dpi", [(0, 180), (90, 180), (0, 360), (90, 360)])
def test_codes_decode_at_device_resolution(kind, format, content, expected, rotation, dpi):
    doc = TemplateDocument.model_validate(
        {
            "width_mm": 80,
            "length_mm": 100,
            "elements": [
                {
                    "id": "code",
                    "kind": kind,
                    "barcode_format": format,
                    "barcode_caption": True,
                    "content": content,
                    "x": 25,
                    "y": 30,
                    "width": 70 if kind == "barcode" else 24,
                    "height": 15 if kind == "barcode" else 24,
                    "rotation": rotation,
                }
            ],
        }
    )
    image = render_document(doc, {}, RenderSpec(dpi_x=dpi)).image
    if dpi == 360:
        image = image.resize((image.width, image.height * 2), Image.Resampling.NEAREST)
    result = zxingcpp.read_barcode(image)
    assert result is not None
    assert result.text == expected


@pytest.mark.parametrize(
    "kind,content",
    [
        ("ean13", "5901234123450"),
        ("ean8", "96385070"),
        ("code39", "lowercase"),
        ("code128", "😀"),
        ("ean13", "123"),
        ("code128", ""),
    ],
)
def test_invalid_content_is_not_silently_changed(kind, content):
    with pytest.raises(ValueError):
        encode_barcode(kind, content)


@pytest.mark.parametrize("kind", ["barcode", "datamatrix", "aztec"])
def test_code_content_can_be_bound_by_logic_and_small_codes_are_rejected(kind):
    raw = {
        "version": 2,
        "width_mm": 24,
        "length_mm": 100,
        "fields": [{"name": "payload", "label": "Payload", "type": "text", "default": "12345678"}],
        "elements": [
            {
                "id": "code",
                "logic_name": "code1",
                "kind": kind,
                "x": 2,
                "y": 2,
                "width": 70 if kind == "barcode" else 20,
                "height": 20,
                "barcode_caption": False,
            }
        ],
    }
    doc = TemplateDocument.model_validate(raw)
    raw["logic"] = compile_source(
        "code1.content = payload;", logic_fields(doc), logic_targets(doc)
    ).model_dump()
    doc = TemplateDocument.model_validate(raw)
    rendered = render_document(doc, {"payload": "87654321"}, RenderSpec())
    assert rendered.document.elements[0].content == "87654321"
    result = zxingcpp.read_barcode(rendered.image)
    assert result is not None and result.text == "87654321"
    doc.elements[0].width = 2
    with pytest.raises(ValueError, match="too (narrow|small)"):
        render_document(doc, {}, RenderSpec())
