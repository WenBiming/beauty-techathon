"""多模态图片分析（spec §4.6）。

只有会话含图片消息才调用——全库 29 条，不做无意义的全量分析。产出用于
自动判定补发/换货类型并预填工单。

官方未提供图片文件，data/mock_images/ 下是 M1 生成的占位图，路径与
chat.image_path 逐字对齐。

**已知限制（对照实验证实，R11）**：在这些占位图上，模型走的是 OCR 路径而
不是视觉理解——占位图把语义类型的英文标签直接画在图上，模型读标签即可答对。
对照实验：涂掉图上的英文标签后重跑，分类即出错。所以这里的准确率**不能**
当作真实买家图片上的视觉理解能力来汇报；换成真实图片需要重新评测。
（这条写在代码里而不是只写在台账里——台账不跟着代码走。）
"""
import base64
import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from openai import OpenAI

from agent.llm import FIXTURE_DIR, FixtureMissing, LLMResponse
from core.config import DATA_DIR, load_env

MODEL = "qwen3-vl-flash"

# 与 image_path 的目录名一一对应（M1 实测 10 个语义目录）
DAMAGE_TYPES = [
    "broken_pump", "broken_parcel", "wrong_shade", "missing_item", "short_item",
    "refund_screenshot", "logistics_screenshot", "live_promise", "swatch",
    "consult_card",
]

# 语义类型 -> 建议工单类型
TICKET_HINT = {
    "broken_pump": "补发换货", "broken_parcel": "物流", "wrong_shade": "补发换货",
    "missing_item": "补发换货", "short_item": "补发换货",
    "refund_screenshot": "线下打款", "logistics_screenshot": "物流",
    "live_promise": "补发换货", "swatch": "售后退货", "consult_card": "售后退货",
}

SYSTEM = f"""你是美妆电商售后图片审核助手。看图判断买家上传的是哪一类凭证或问题。

damage_type 必须严格从以下列表选一个：{"、".join(DAMAGE_TYPES)}
suggested_ticket_type 从以下选一个：补发换货、线下打款、物流、不良反应、售后退货

只输出 JSON，不要 markdown 代码块，不要解释。格式：
{{"damage_type":"...","description":"不超过30字的客观描述","suggested_ticket_type":"..."}}"""


@dataclass(frozen=True)
class VisionResult:
    session_id: str
    image_path: str
    damage_type: str
    description: str
    suggested_ticket_type: str
    model: str
    tokens_in: int
    tokens_out: int
    degraded: bool


def vision_fixture_key(model: str, image_path: str) -> str:
    """用图片路径而非字节做 key——图片重新生成后字节会变，语义没变。"""
    h = hashlib.sha256()
    h.update(model.encode("utf-8"))
    h.update(b"\x00")
    h.update(image_path.encode("utf-8"))
    return "vl-" + h.hexdigest()[:16]


def sessions_with_images(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return [(r["session_id"], r["image_path"]) for r in conn.execute(
        "SELECT session_id, image_path FROM chat WHERE image_path IS NOT NULL"
        " ORDER BY session_id, sent_at")]


def encode_image(path: Path) -> str:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"图片不存在: {p}")
    return "data:image/jpeg;base64," + base64.b64encode(p.read_bytes()).decode("ascii")


class VisionClient(Protocol):
    def complete_vision(self, model: str, system: str, text: str,
                        image_data_uri: str, *, max_tokens: int = 500
                        ) -> LLMResponse: ...


class DashScopeVisionClient:
    def __init__(self) -> None:
        load_env()
        self._client = OpenAI(api_key=os.environ["DASHSCOPE_API_KEY"],
                              base_url=os.environ["DASHSCOPE_BASE_URL"])

    def complete_vision(self, model, system, text, image_data_uri, *, max_tokens=500):
        r = self._client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": [
                    {"type": "text", "text": text},
                    {"type": "image_url", "image_url": {"url": image_data_uri}},
                ]},
            ],
            temperature=0.1, max_tokens=max_tokens,
            extra_body={"enable_thinking": False},
        )
        return LLMResponse(text=(r.choices[0].message.content or "").strip(),
                           model=model, tokens_in=r.usage.prompt_tokens,
                           tokens_out=r.usage.completion_tokens)


class FixtureVisionClient:
    def __init__(self, fixture_dir: Path | None = None) -> None:
        self.dir = Path(fixture_dir) if fixture_dir is not None else FIXTURE_DIR
        self._path: str | None = None

    def for_image(self, image_path: str) -> "FixtureVisionClient":
        self._path = image_path
        return self

    def complete_vision(self, model, system, text, image_data_uri, *, max_tokens=500):
        if self._path is None:
            raise FixtureMissing("调用前请先 for_image(image_path) 指定图片路径")
        f = self.dir / f"{vision_fixture_key(model, self._path)}.json"
        if not f.is_file():
            raise FixtureMissing(f"缺少 fixture {f}，用 scripts/record_fixtures.py 录制")
        return LLMResponse(**json.loads(f.read_text(encoding="utf-8")))


def _parse(text: str) -> dict:
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[-1] if "\n" in body else body
        body = body.removeprefix("json").strip()
        if body.endswith("```"):
            body = body[: body.rindex("```")]
    data = json.loads(body.strip())
    if data.get("damage_type") not in DAMAGE_TYPES:
        raise ValueError(f"damage_type 不在白名单内: {data.get('damage_type')!r}")
    return data


def analyse_image(conn: sqlite3.Connection, client: VisionClient, session_id: str,
                  image_path: str, *, retries: int = 1) -> VisionResult:
    uri = encode_image(DATA_DIR / image_path)
    tokens_in = tokens_out = 0
    last_error = None
    for _ in range(retries + 1):
        r = client.complete_vision(MODEL, SYSTEM, "请判断这张图属于哪一类。", uri)
        tokens_in += r.tokens_in
        tokens_out += r.tokens_out
        try:
            data = _parse(r.text)
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            continue
        dtype = data["damage_type"]
        return VisionResult(
            session_id=session_id, image_path=image_path, damage_type=dtype,
            description=str(data.get("description", "")).strip(),
            suggested_ticket_type=str(
                data.get("suggested_ticket_type") or TICKET_HINT[dtype]),
            model=MODEL, tokens_in=tokens_in, tokens_out=tokens_out, degraded=False)

    return VisionResult(session_id=session_id, image_path=image_path, damage_type="",
                        description=f"VL 解析失败降级：{last_error}",
                        suggested_ticket_type="", model=MODEL, tokens_in=tokens_in,
                        tokens_out=tokens_out, degraded=True)
