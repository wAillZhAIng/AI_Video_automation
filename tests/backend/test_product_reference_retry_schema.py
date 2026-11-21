import pytest
from pydantic import ValidationError

from app.api.studio import ProductReferenceRetry


def test_product_reference_retry_requires_node_id_and_accepts_video_selection():
    payload = ProductReferenceRetry.model_validate({
        "expected_run_id": "run-1",
        "node_id": "video:shot-1",
        "products": [{
            "product_id": "product-1",
            "expected_revision": 2,
            "video_image_ids": ["cover-1"],
            "keyframe_image_ids": ["cover-1", "detail-2"],
        }],
    })
    assert payload.node_id == "video:shot-1"
    assert payload.products[0].video_image_ids == ["cover-1"]
    assert payload.products[0].keyframe_image_ids == ["cover-1", "detail-2"]

    with pytest.raises(ValidationError):
        ProductReferenceRetry.model_validate({
            "expected_run_id": "run-1",
            "products": [],
        })
