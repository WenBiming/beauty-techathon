# 心迹 EmpathyTrace

欧莱雅集团第二届美妆科技黑客松 · 赛题一「数据共情者 — 消费者的AI管家」

> ⚠ 本项目使用的业务数据全部为官方提供的 AI 生成虚构 MOCK 数据，
> 与任何真实企业、品牌、个人或交易无关，不得用于生产用途。

## 快速开始

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python openpyxl python-dotenv openai pandas pillow pytest

# 生成 mock 图片
.venv/bin/python -m scripts.gen_mock_images

# 构建数据层
.venv/bin/python -m etl.build

# 跑测试
.venv/bin/python -m pytest -v
```

需要 `.env`（不入库）：

```
DASHSCOPE_API_KEY=sk-...
DASHSCOPE_BASE_URL=https://<workspace>.eu-central-1.maas.aliyuncs.com/compatible-mode/v1
```

## 文档

- 设计文档：`docs/superpowers/specs/2026-09-02-beauty-techathon-empathy-agent-design.md`
- M1 实现计划：`docs/superpowers/plans/2026-09-02-m1-data-layer.md`
