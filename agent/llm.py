"""模型适配器。

测试永不联网：全部走 FixtureClient 回放。真实调用只发生在
scripts/record_fixtures.py 和最终的批处理运行。

所有请求带 enable_thinking=False —— 这批是混合推理模型，开着思考链
单会话 token 上升一个数量级，而意图分类/情绪打分/摘要是理解与抽取
任务，不需要长推理（spec §2.6）。
"""
import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from openai import OpenAI

from core.config import PROJECT_ROOT, load_env

FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures" / "llm"


class FixtureMissing(RuntimeError):
    pass


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    tokens_in: int
    tokens_out: int


def fixture_key(model: str, system: str, user: str) -> str:
    h = hashlib.sha256()
    for part in (model, system, user):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


class LLMClient(Protocol):
    def complete(self, model: str, system: str, user: str, *,
                 max_tokens: int = 800, temperature: float = 0.1) -> LLMResponse: ...


class FixtureClient:
    """从磁盘回放录制好的响应。测试专用。"""

    def __init__(self, fixture_dir: Path | None = None) -> None:
        self.dir = Path(fixture_dir) if fixture_dir is not None else FIXTURE_DIR

    def complete(self, model, system, user, *, max_tokens=800, temperature=0.1):
        key = fixture_key(model, system, user)
        path = self.dir / f"{key}.json"
        if not path.is_file():
            raise FixtureMissing(
                f"缺少 fixture {path}。用 scripts/record_fixtures.py 录制后重试。"
            )
        return LLMResponse(**json.loads(path.read_text(encoding="utf-8")))


class DashScopeClient:
    """真实调用。阿里云百炼专属空间，OpenAI 兼容模式。"""

    def __init__(self) -> None:
        load_env()
        self._client = OpenAI(
            api_key=os.environ["DASHSCOPE_API_KEY"],
            base_url=os.environ["DASHSCOPE_BASE_URL"],
        )

    def complete(self, model, system, user, *, max_tokens=800, temperature=0.1):
        r = self._client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            temperature=temperature,
            max_tokens=max_tokens,
            extra_body={"enable_thinking": False},
        )
        return LLMResponse(
            text=(r.choices[0].message.content or "").strip(),
            model=model,
            tokens_in=r.usage.prompt_tokens,
            tokens_out=r.usage.completion_tokens,
        )


class RecordingClient:
    """包一层真实客户端，把响应落盘成 fixture。只在录制脚本里用。"""

    def __init__(self, inner: LLMClient, fixture_dir: Path | None = None) -> None:
        self.inner = inner
        self.dir = Path(fixture_dir) if fixture_dir is not None else FIXTURE_DIR

    def complete(self, model, system, user, *, max_tokens=800, temperature=0.1):
        r = self.inner.complete(model, system, user,
                                max_tokens=max_tokens, temperature=temperature)
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / f"{fixture_key(model, system, user)}.json").write_text(
            json.dumps(asdict(r), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return r
