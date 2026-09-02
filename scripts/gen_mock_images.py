"""生成 mock 图片占位图，路径严格对齐聊天记录的 image_path（spec §2.5.1）。

官方未提供图片文件。这些图仅用于打通 qwen3-vl 链路，
内容为语义标签的文字卡片，不伪装成真实照片。
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw

from core.config import MOCK_IMAGE_DIR
from etl.reader import read_sheet

SIZE = (640, 480)

# 语义目录 -> (中文说明, 背景色)
CATEGORY = {
    "broken_pump": ("粉底液泵头破损", (214, 92, 92)),
    "broken_parcel": ("包裹外箱破损", (201, 106, 74)),
    "wrong_shade": ("色号发错", (176, 120, 190)),
    "missing_item": ("漏发赠品", (222, 158, 74)),
    "short_item": ("少发正装", (203, 145, 66)),
    "refund_screenshot": ("退款记录截图", (86, 132, 196)),
    "logistics_screenshot": ("物流轨迹截图", (74, 148, 158)),
    "live_promise": ("直播承诺截图", (196, 92, 148)),
    "swatch": ("试色对比图", (150, 122, 182)),
    "consult_card": ("咨询商品卡片", (104, 142, 110)),
}

DISCLAIMER = "MOCK — 虚构占位图，非真实照片"


def _draw(path: Path, category: str, stem: str) -> None:
    label, colour = CATEGORY[category]
    img = Image.new("RGB", SIZE, colour)
    d = ImageDraw.Draw(img)
    d.rectangle([20, 20, SIZE[0] - 20, SIZE[1] - 20], outline=(255, 255, 255), width=3)
    d.text((48, 180), label, fill=(255, 255, 255))
    d.text((48, 210), category, fill=(255, 255, 255))
    d.text((48, 240), stem, fill=(255, 255, 255))
    d.text((48, 420), DISCLAIMER, fill=(255, 255, 255))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=85)


def generate(out_root: Path | None = None) -> list[Path]:
    """为每个声明的 image_path 生成一张占位图。幂等：重复运行覆盖同名文件。"""
    root = Path(out_root) if out_root is not None else MOCK_IMAGE_DIR.parent

    declared = sorted({r["image_path"] for r in read_sheet("chat") if r["image_path"]})
    written = []
    for rel in declared:
        parts = Path(rel).parts          # ("mock_images", "<category>", "<file>.jpg")
        category = parts[1]
        if category not in CATEGORY:
            raise KeyError(f"未知语义目录 {category}（来自 {rel}），请补进 CATEGORY")
        target = root / rel
        _draw(target, category, Path(rel).stem)
        written.append(target)
    return written


if __name__ == "__main__":
    paths = generate()
    print(f"已生成 {len(paths)} 张 mock 图片 → {paths[0].parent.parent}")
    sys.exit(0)
