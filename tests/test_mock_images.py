from PIL import Image

from etl.reader import read_sheet
from scripts import gen_mock_images


def test_generates_one_file_per_image_path(tmp_path):
    paths = gen_mock_images.generate(tmp_path)
    declared = {r["image_path"] for r in read_sheet("chat") if r["image_path"]}
    assert len(declared) == 29
    assert len(paths) == 29


def test_paths_align_with_declared_image_path(tmp_path):
    """路径必须与 image_path 逐字对齐，否则 M2 的 VL 链路取不到图。"""
    gen_mock_images.generate(tmp_path)
    declared = {r["image_path"] for r in read_sheet("chat") if r["image_path"]}
    for rel in declared:
        assert (tmp_path / rel).is_file(), f"缺图: {rel}"


def test_generated_files_are_valid_images(tmp_path):
    paths = gen_mock_images.generate(tmp_path)
    for p in paths[:5]:
        with Image.open(p) as im:
            im.verify()


def test_ten_semantic_categories_present(tmp_path):
    gen_mock_images.generate(tmp_path)
    dirs = {d.name for d in (tmp_path / "mock_images").iterdir() if d.is_dir()}
    assert dirs == {
        "refund_screenshot", "wrong_shade", "broken_parcel", "broken_pump",
        "live_promise", "missing_item", "short_item", "swatch",
        "consult_card", "logistics_screenshot",
    }


def test_generate_is_idempotent(tmp_path):
    first = gen_mock_images.generate(tmp_path)
    second = gen_mock_images.generate(tmp_path)
    assert sorted(first) == sorted(second)
