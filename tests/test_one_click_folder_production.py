from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import one_click_folder_production as launcher


def _write_docx(path: Path, text: str = "商品展示") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f'<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>'
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document)


def _write_png(path: Path, marker: bytes) -> None:
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + marker.ljust(8, b"0"))


def test_scan_binds_shared_folder_images_and_keeps_groups_separate(tmp_path: Path) -> None:
    root = tmp_path / "scripts"
    group_a = root / "product-a"
    group_b = root / "product-b"
    _write_docx(group_a / "a.docx")
    _write_docx(group_a / "b.docx")
    _write_png(group_a / "front.png", b"front")
    (group_a / "side.jpg").write_bytes(b"\xff\xd8\xff" + b"side".ljust(13, b"0"))
    _write_docx(group_b / "c.docx")
    _write_png(group_b / "reference.png", b"other")

    result = launcher.scan(root)

    assert len(result.candidates) == 3
    group_a_candidates = [item for item in result.candidates if item.path.parent == group_a]
    group_b_candidate = next(item for item in result.candidates if item.path.parent == group_b)
    assert all(item.product_match for item in result.candidates)
    assert len(group_a_candidates) == 2
    assert {image.sha256 for image in group_a_candidates[0].images} == {image.sha256 for image in group_a_candidates[1].images}
    assert len(group_a_candidates[0].images) == 2
    assert len(group_b_candidate.images) == 1
    assert launcher.product_identity(group_a_candidates[0]) == ("product-a", "")
    assert launcher.product_identity(group_b_candidate) == ("product-b", "")


def test_explicit_product_name_is_used_without_guessing_brand(tmp_path: Path) -> None:
    root = tmp_path / "scripts" / "generic"
    _write_docx(root / "script.docx", "主体展示")
    _write_png(root / "photo.png", b"image")
    candidate = launcher.scan(root.parent).candidates[0]

    assert launcher.product_identity(candidate, product_name="用户产品") == ("用户产品", "")
    assert launcher.product_identity(candidate, brand="明确品牌") == ("generic", "明确品牌")


def test_all_selection_respects_hard_limit(tmp_path: Path) -> None:
    root = tmp_path / "scripts"
    for number in range(3):
        _write_docx(root / f"{number}.docx")
    candidates = launcher.scan(root).candidates

    assert len(launcher.select_all(candidates, limit=3)) == 3
    with pytest.raises(launcher.WorkflowError, match="超过 --limit"):
        launcher.select_all(candidates, limit=2)


def test_state_ledger_persists_idempotency_data_without_api_query(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    with launcher.RunLedger(state_path) as ledger:
        ledger.record("fingerprint", {"status": "started", "project_id": "project-1", "stage": "started"})
        with pytest.raises(launcher.WorkflowError, match="另一个进程占用"):
            launcher.RunLedger(state_path)
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["entries"]["fingerprint"]["project_id"] == "project-1"
    assert state_path.with_suffix(".json.lock").exists()
    with launcher.RunLedger(state_path) as resumed:
        assert resumed.get("fingerprint")["status"] == "started"


def test_api_fingerprint_strips_credentials_and_query() -> None:
    assert launcher.api_fingerprint("https://user:secret@example.test:8443/api?token=private") == "https://example.test:8443/api"


def test_default_quality_uses_fast_local_realesrgan_and_exact_ai_plan() -> None:
    assert launcher.QUALITY == {
        "resolution": "4k", "mode": "fast", "processing": "ai_super_resolution", "aspect_ratio": "16:9"
    }

    class FakeApi:
        def get(self, path: str):
            assert path == "/api/quality/capabilities"
            return {"ai_plans": [{
                "requested": {"resolution": "4k", "aspect_ratio": "16:9"},
                "generation": {"width": 1024, "height": 576, "steps": 30},
                "supported": True,
                "ai_super_resolution": True,
                "postprocess_model": {"name": "RealESRGAN_x4plus"},
            }], "modes": {"fast": {"steps": 4}}}

    supported, evidence = launcher.quality_supported(FakeApi(), launcher.QUALITY)
    assert supported is True
    assert evidence["postprocess_model"]["name"] == "RealESRGAN_x4plus"
    assert evidence["generation"]["steps"] == 4
    assert evidence["requested_mode"] == "fast"
    assert evidence["mode_steps_verified"] is True


def test_quality_preflight_checks_only_the_selected_processing_plan() -> None:
    class FakeApi:
        def __init__(self, capabilities):
            self.capabilities = capabilities

        def get(self, path: str):
            assert path == "/api/quality/capabilities"
            return self.capabilities

    # The default Real-ESRGAN route remains available even when the service
    # does not expose any SeedVR2 capabilities.
    ai_capabilities = {"ai_plans": [{
        "requested": {"resolution": "4k", "aspect_ratio": "16:9"},
        "supported": True,
        "ai_super_resolution": True,
        "postprocess_model": {"name": "RealESRGAN_x4plus"},
    }]}
    assert launcher.quality_supported(FakeApi(ai_capabilities), launcher.QUALITY)[0] is True

    # Explicit SeedVR2 selection still checks SeedVR2's own exact capability;
    # it neither borrows Real-ESRGAN evidence nor forces SeedVR2 on other routes.
    seed_quality = {**launcher.QUALITY, "processing": "seedvr2"}
    assert launcher.quality_supported(FakeApi(ai_capabilities), seed_quality)[0] is False
    seed_capabilities = {"seedvr2_plans": [{
        "requested": {"resolution": "4k", "aspect_ratio": "16:9"},
        "supported": True,
    }]}
    assert launcher.quality_supported(FakeApi(seed_capabilities), seed_quality)[0] is True


def test_generation_preflight_requires_local_context_ir_and_exact_mode_steps() -> None:
    class FakeApi:
        def __init__(self, health, steps=4):
            self.health = health
            self.steps = steps

        def get(self, path: str):
            if path == "/api/providers/h3/context-health": return self.health
            if path == "/api/quality/capabilities": return {"modes": {"fast": {"steps": self.steps}}}
            raise AssertionError(path)

    quality = {"mode": "fast"}
    healthy = {"provider": "comfyui_h3_context_ir", "mode": "real", "ready": True,
        "reference_to_video_available": True}
    assert launcher.generation_supported(FakeApi(healthy), quality)[0] is True
    assert launcher.generation_supported(FakeApi({**healthy, "ready": False}), quality)[0] is False
    assert launcher.generation_supported(FakeApi(healthy, steps=20), quality)[0] is False


def test_generation_preflight_waits_for_transient_qwen_busy_state(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeApi:
        def __init__(self): self.calls = 0
        def get(self, path: str):
            if path == "/api/providers/h3/context-health":
                self.calls += 1
                if self.calls == 1:
                    return {"provider": "comfyui_h3_context_ir", "mode": "real", "ready": False,
                        "reference_to_video_available": False, "busy": True, "busy_reason": "qwen"}
                return {"provider": "comfyui_h3_context_ir", "mode": "real", "ready": True,
                    "reference_to_video_available": True, "busy": False}
            if path == "/api/quality/capabilities": return {"modes": {"fast": {"steps": 4}}}
            raise AssertionError(path)
    monkeypatch.setattr(launcher.time, "sleep", lambda _seconds: None)
    api = FakeApi()
    supported, evidence = launcher.generation_supported(api, {"mode": "fast"}, busy_wait_seconds=1)
    assert supported is True
    assert api.calls == 2
    assert evidence["health"]["ready"] is True


def test_unknown_import_reconciliation_only_replays_non_project_records() -> None:
    class FakeApi:
        def __init__(self, rows): self.rows = rows
        def get(self, path: str):
            assert path == "/api/word-imports"
            return self.rows
    digest = "a" * 64
    safe = launcher.reconcile_import_by_sha(FakeApi([
        {"id": "pending-1", "sha256": digest, "status": "pending_review"},
    ]), digest)
    assert safe["safe_to_replay"] is True
    assert safe["records"] == [{"id": "pending-1", "status": "pending_review"}]
    assert safe["import_id"] == "pending-1"

    reused = launcher.reconcile_import_by_sha(FakeApi([
        {"id": "complete-1", "sha256": digest, "status": "completed", "result_json": {"project_id": "project-1"}},
    ]), digest)
    assert reused["safe_to_replay"] is True
    assert reused["project_id"] == "project-1"

    unsafe = launcher.reconcile_import_by_sha(FakeApi([
        {"id": "running-1", "sha256": digest, "status": "processing"},
    ]), digest)
    assert unsafe["safe_to_replay"] is False


def test_crash_left_import_pending_ledger_is_reconciled_before_any_upload() -> None:
    crash_left = {"status": "import_pending", "stage": "import_pending",
        "script_sha256": "a" * 64, "project_id": None, "run_id": None}
    assert launcher._is_unresolved_import(crash_left)
    assert launcher._is_unresolved_import({**crash_left, "status": "unknown"})
    assert not launcher._is_unresolved_import({**crash_left, "project_id": "project-1"})
    assert not launcher._is_unresolved_import({**crash_left, "status": "started"})


def test_definite_start_422_is_failure_and_can_reuse_existing_project_without_reimport() -> None:
    previous = {"stage": "start_pending", "status": "unknown", "project_id": "project-1",
        "product_id": "product-1", "image_ids": ["image-1"], "outcome_unknown": False,
        "details": {"http_status": 422}}
    error = launcher.WorkflowError("HTTP 422 rejected", phase="api", details={"http_status": 422})
    assert launcher._definite_start_rejection(previous) is True
    retry_record = {**previous, "stage": "start_pending_retry", "status": "failed"}
    assert launcher._definite_start_rejection(retry_record) is True
    assert launcher._definite_start_rejection({**retry_record, "outcome_unknown": True}) is False
    assert launcher._failure_status(error, "start_pending") == "failed"
    assert launcher._failure_status(launcher.WorkflowError("timeout", outcome_unknown=True), "start_pending") == "unknown"

    class FakeApi:
        def __init__(self): self.calls = []
        def get(self, path: str):
            self.calls.append(("GET", path))
            return {"run": None, "shots": [{"id": "shot-1"}]}
        def post_json(self, path: str, body):
            self.calls.append(("POST", path, body))
            return {"run": {"id": "run-1", "status": "queued"}}

    api = FakeApi()
    quality = {"resolution": "4k", "mode": "fast", "processing": "ai_super_resolution", "aspect_ratio": "9:16"}
    result = launcher.retry_rejected_project_start(api, "project-1", quality, previous)
    assert result["status"] == "started"
    assert result["run_id"] == "run-1"
    assert result["retried_rejected_start"] is True
    assert [call[0] for call in api.calls] == ["GET", "GET", "POST"]
    assert api.calls[2][2]["product_reference_plan"] == {
        "product_id": "product-1", "image_ids": ["image-1"],
        "selection_reason": "恢复已导入项目时沿用图库封面图作为商品身份锚点。"}
    assert api.calls[2][2]["width"] == 576 and api.calls[2][2]["height"] == 1024


def test_retry_product_reference_failures_changes_video_anchor_and_preserves_keyframe_selection() -> None:
    product_error = "商品参考身份不一致或无法确定：<Picture 2> 无法唯一定位商品及独立操作者"
    nodes = [
        {"id": "video:shot-a", "kind": "video", "target_id": "shot-a", "status": "exception", "error": product_error},
        {"id": "video:shot-b", "kind": "video", "target_id": "shot-b", "status": "failed", "error": product_error},
        {"id": "quality:shot-a", "kind": "quality", "status": "waiting_dependencies", "error": None},
    ]

    class FakeApi:
        def __init__(self, current_nodes): self.current_nodes = current_nodes; self.calls = []
        def get(self, path):
            self.calls.append(("GET", path))
            if path.endswith("/references"):
                return {"products": [{"product_id": "product-1", "revision": 7,
                    "video_image_ids": ["cover-1", "detail-2"],
                    "keyframe_image_ids": ["cover-1", "detail-2"],
                    "images": [{"id": "cover-1"}, {"id": "detail-2"}]}]}
            if path == "/api/product-library/product-1":
                return {"id": "product-1", "cover_image_id": "cover-1", "images": [{"id": "cover-1"}]}
            return {"run": {"id": "run-1", "status": "running", "nodes_json": self.current_nodes}}
        def post_json(self, path, body):
            self.calls.append(("POST", path, body))
            return {"run": {"id": "run-1", "status": "queued", "nodes_json": [
                {**nodes[0], "status": "pending"}, *nodes[1:]]}}

    api = FakeApi(nodes)
    result = launcher.retry_product_reference_failures(api, "project-1", "run-1")
    assert result is not None
    assert result["retried_product_reference_nodes"] == ["video:shot-a"]
    assert result["product_reference_retry_run_id"] == "run-1"
    post = next(call for call in api.calls if call[0] == "POST")
    assert post[1] == "/api/studio/projects/project-1/retry-product-references"
    assert post[2] == {"expected_run_id": "run-1", "node_id": "video:shot-a", "products": [{
        "product_id": "product-1", "expected_revision": 7,
        "video_image_ids": ["cover-1"], "keyframe_image_ids": ["cover-1", "detail-2"]}]}

    active = FakeApi([*nodes, {"id": "video:shot-c", "kind": "video", "status": "running"}])
    assert launcher.retry_product_reference_failures(active, "project-1", "run-1") is None
    assert not any(call[0] == "POST" for call in active.calls)

    other_error = FakeApi([{"id": "video:shot-x", "kind": "video", "status": "exception",
        "error": "unrelated provider failure"}])
    assert launcher.retry_product_reference_failures(other_error, "project-1", "run-1") is None
    assert not any(call[0] == "POST" for call in other_error.calls)


def test_retry_product_reference_failures_serializes_confirmed_failed_nodes() -> None:
    nodes = [
        {"id": "video:shot-a", "kind": "video", "target_id": "shot-a", "status": "exception",
         "error": "商品参考身份不一致或无法确定"},
        {"id": "video:shot-b", "kind": "video", "target_id": "shot-b", "status": "failed",
         "error": "商品参考身份不一致或无法确定"},
    ]

    class FakeApi:
        def __init__(self): self.nodes = [dict(node) for node in nodes]; self.calls = []
        def get(self, path):
            self.calls.append(("GET", path))
            if path.endswith("/references"):
                return {"products": [{"product_id": "product-1", "revision": 1,
                    "video_image_ids": ["cover-1", "detail-2"], "keyframe_image_ids": ["cover-1", "detail-2"],
                    "images": [{"id": "cover-1"}, {"id": "detail-2"}]}]}
            if path == "/api/product-library/product-1":
                return {"cover_image_id": "cover-1", "images": [{"id": "cover-1"}]}
            return {"run": {"id": "run-1", "status": "running", "nodes_json": self.nodes}}
        def post_json(self, path, body):
            self.calls.append(("POST", path, body))
            target = body["node_id"]
            self.nodes = [{**node, "status": "succeeded" if node["id"] == target else node["status"]}
                for node in self.nodes]
            return {"run": {"id": "run-1", "status": "running", "nodes_json": self.nodes}}

    api = FakeApi()
    result = launcher.retry_product_reference_failures(api, "project-1", "run-1", wait=True,
        poll_seconds=0, wait_seconds=5)
    assert result is not None
    assert result["retried_product_reference_nodes"] == ["video:shot-a", "video:shot-b"]
    posts = [call for call in api.calls if call[0] == "POST"]
    assert [call[2]["node_id"] for call in posts] == ["video:shot-a", "video:shot-b"]


def test_ssh_tunnel_fingerprint_is_stable_and_recovers_exact_prior_ledger_entry(monkeypatch: pytest.MonkeyPatch,
                                                                                 tmp_path: Path) -> None:
    monkeypatch.setenv("VIDEO_FACTORY_API_ID", "vast-ssh://220.135.0.171:59748/127.0.0.1:18766")
    assert launcher.api_fingerprint("http://127.0.0.1:49270") == "vast-ssh://220.135.0.171:59748/127.0.0.1:18766"
    assert launcher.api_fingerprint("http://127.0.0.1:35119") == "vast-ssh://220.135.0.171:59748/127.0.0.1:18766"
    image = launcher.ImageAsset(tmp_path / "product.png", "image-hash", 1, "image/png")
    candidate = launcher.Candidate(tmp_path / "script.docx", "product/script.docx", "script-hash", 2,
        "source text", images=[image])
    quality = {"resolution": "4k", "mode": "fast", "processing": "ai_super_resolution", "aspect_ratio": "9:16"}
    rows = {"old-port-key": {"script": candidate.relative_path, "script_sha256": candidate.sha256,
        "image_sha256": [image.sha256], "quality": quality, "status": "unknown", "stage": "import_pending"}}
    assert launcher.find_prior_candidate_entry(rows, candidate, quality) == ("old-port-key", rows["old-port-key"])


def test_prior_entry_prefers_exact_existing_project_over_stale_preflight_and_rejects_duplicate_projects(tmp_path: Path) -> None:
    image = launcher.ImageAsset(tmp_path / "product.png", "image-hash", 1, "image/png")
    candidate = launcher.Candidate(tmp_path / "script.docx", "product/script.docx", "script-hash", 2,
        "source text", images=[image])
    quality = {"resolution": "4k", "mode": "fast", "processing": "ai_super_resolution", "aspect_ratio": "9:16"}
    common = {"script": candidate.relative_path, "script_sha256": candidate.sha256,
        "image_sha256": [image.sha256], "quality": quality}
    stale = {**common, "status": "preflight_failed", "stage": "preflight", "updated_at": "2026-11-20T01:00:00Z"}
    rejected = {**common, "status": "unknown", "stage": "start_pending", "project_id": "project-1",
        "details": {"http_status": 422}, "updated_at": "2026-11-20T02:00:00Z"}
    rows = {"stale-preflight": stale, "known-project": rejected}
    assert launcher.find_prior_candidate_entry(rows, candidate, quality) == ("known-project", rejected)
    with pytest.raises(launcher.WorkflowError, match="多条哈希相同记录"):
        launcher.find_prior_candidate_entry({"project-1": rejected,
            "project-2": {**rejected, "project_id": "project-2"}}, candidate, quality)


def test_unknown_pending_review_import_is_reparsed_and_confirmed_without_reupload() -> None:
    digest = "b" * 64
    class FakeApi:
        def __init__(self): self.calls = []
        def get(self, path: str):
            self.calls.append(("GET", path))
            return {"id": "import-1", "sha256": digest, "status": "pending_review", "revision": 1}
        def request(self, method: str, path: str, **kwargs):
            self.calls.append((method, path))
            assert method == "POST"
            assert kwargs.get("body") is None
            return {"id": "import-1", "sha256": digest, "status": "pending_review", "revision": 2, "issues": []}
        def post_json(self, path: str, value):
            self.calls.append(("POST_JSON", path, value))
            return {"project_id": "project-1", "script_id": "script-1", "shot_ids": ["shot-1"]}
    api = FakeApi()
    row = launcher.resume_existing_import(api, "import-1", digest)
    assert row["status"] == "imported"
    assert row["project_id"] == "project-1"
    assert row["shot_ids"] == ["shot-1"]
    assert [call[:2] for call in api.calls] == [
        ("GET", "/api/word-imports/import-1"),
        ("POST", "/api/word-imports/import-1/retry?expected_revision=1"),
        ("POST_JSON", "/api/word-imports/import-1/confirm"),
    ]


def test_unknown_pending_review_import_is_not_confirmed_when_hash_or_preview_is_invalid() -> None:
    digest = "c" * 64
    class FakeApi:
        def __init__(self, actual_hash, issues): self.actual_hash, self.issues, self.writes = actual_hash, issues, []
        def get(self, _path): return {"sha256": self.actual_hash, "status": "pending_review", "revision": 1}
        def request(self, method, path, **kwargs):
            self.writes.append((method, path))
            return {"status": "pending_review", "revision": 2, "issues": self.issues}
        def post_json(self, path, value):
            self.writes.append(("POST_JSON", path))
            return {}
    wrong_hash = FakeApi("d" * 64, [])
    with pytest.raises(launcher.WorkflowError, match="SHA-256"):
        launcher.resume_existing_import(wrong_hash, "import-1", digest)
    assert wrong_hash.writes == []

    invalid_preview = FakeApi(digest, ["缺少有效镜头"])
    with pytest.raises(launcher.WorkflowError, match="阻断问题"):
        launcher.resume_existing_import(invalid_preview, "import-1", digest)
    assert len(invalid_preview.writes) == 1
    assert invalid_preview.writes[0][0] == "POST"


def test_submit_candidate_blocks_before_word_import_when_context_ir_is_unready(tmp_path: Path) -> None:
    class FakeApi:
        def get(self, path: str):
            if path == "/api/providers/h3/context-health":
                return {"provider": "comfyui_h3_context_ir", "mode": "real", "ready": False,
                    "reference_to_video_available": False}
            if path == "/api/quality/capabilities": return {"modes": {"fast": {"steps": 4}}}
            raise AssertionError(path)

    candidate = launcher.Candidate(
        path=tmp_path / "script.docx", relative_path="product/script.docx",
        sha256="a" * 64, size=1, text="9:16", product_match=True,
    )
    with pytest.raises(launcher.WorkflowError, match="Context-IR/H3"):
        launcher.submit_candidate(FakeApi(), candidate, quality={"mode": "fast", "aspect_ratio": "9:16"},
            product_name=None, brand=None)


@pytest.mark.parametrize(
    ("run_status", "auto_status", "expected"),
    [("completed", "generated", "finished"), ("completed_with_errors", "generated", "failed"),
     ("completed", "failed", "failed"), ("waiting_review", "generated", "blocked")],
)
def test_resuming_a_known_run_never_resubmits_and_classifies_terminal_state(
    run_status: str, auto_status: str, expected: str,
) -> None:
    class FakeApi:
        def get(self, path: str):
            assert path == "/api/studio/projects/project-1"
            return {"run": {"id": "run-1", "status": run_status,
                    "result_json": {"automatic_final": {"status": auto_status}}},
                "shots": [{"video": {"id": "video-1"}}], "renders": [], "exceptions": []}

    result = launcher.poll_existing_run(FakeApi(), "project-1", "run-1")
    assert result["status"] == expected
    assert result["resumed_without_resubmitting"] is True
    assert result["videos"] == [{"id": "video-1"}]


def test_screenplay_aspect_ratio_and_context_canvas_are_preserved(tmp_path: Path) -> None:
    assert launcher.resolve_aspect_ratio("时间轴 00:00-00:09；严格锁定 9:16 竖屏（1080x1920）") == "9:16"
    assert launcher.resolve_aspect_ratio("输出尺寸 1080×1920") == "9:16"
    assert launcher.resolve_aspect_ratio("没有画幅说明") == "16:9"
    assert launcher.resolve_aspect_ratio("剧本写 9:16，但我明确覆盖为横屏", "16:9") == "16:9"
    with pytest.raises(launcher.WorkflowError, match="互相冲突"):
        launcher.resolve_aspect_ratio("成片 9:16，另一个规格写 16:9")

    assert launcher.context_native_canvas("9:16") == (576, 1024)
    assert launcher.context_native_canvas("16:9") == (1024, 576)
    assert launcher.context_native_canvas("1:1") == (768, 768)


def test_batch_continues_after_failure_and_skips_already_dispatched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "scripts"
    _write_docx(root / "a.docx")
    _write_docx(root / "b.docx")
    state_path = tmp_path / "data" / "ledger.json"

    class FakeApi:
        base = "http://example.test:8765"

        def __init__(self, *_args, **_kwargs): pass

        def get(self, path: str):
            if path == "/api/health": return {"status": "ok"}
            if path == "/api/providers/h3/context-health":
                return {"provider": "comfyui_h3_context_ir", "mode": "real", "ready": True,
                    "reference_to_video_available": True}
            if path == "/api/quality/capabilities":
                return {"ai_plans": [{"requested": {"resolution": "4k", "aspect_ratio": "16:9"},
                    "supported": True, "ai_super_resolution": True, "postprocess_model": {"name": "RealESRGAN_x4plus"}}],
                    "modes": {"fast": {"steps": 4}}}
            return {"items": []}

    submitted: list[str] = []

    def fake_submit(_api, candidate, *, progress=None, **_kwargs):
        submitted.append(candidate.relative_path)
        if candidate.path.name == "a.docx":
            raise launcher.WorkflowError("synthetic preflight failure", phase="preflight")
        if progress: progress("started", {"project_id": "p-b", "run_id": "r-b"})
        return {"status": "started", "project_id": "p-b", "run_id": "r-b"}

    monkeypatch.setattr(launcher, "Api", FakeApi)
    monkeypatch.setattr(launcher, "submit_candidate", fake_submit)
    args = ["--root", str(root), "--all", "--submit", "--confirm-submit", "--limit", "10", "--state-file", str(state_path), "--api", FakeApi.base]

    assert launcher.main(args) == 1
    assert len(submitted) == 2
    saved = json.loads(state_path.read_text(encoding="utf-8"))["entries"]
    statuses = {entry["script"]: entry["status"] for entry in saved.values()}
    assert statuses == {"a.docx": "preflight_failed", "b.docx": "started"}

    submitted.clear()
    assert launcher.main(args) == 1
    assert submitted == []
