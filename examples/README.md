# 最小调用示例（示意输出，非正式案例结果）

## 输入

一篇论文 PDF，例如 `paper.pdf`；评测时另有 `/input/case.json`：

```json
{"case_id": "case-001", "task": "parse literature pdf"}
```

## 调用

```bash
python scripts/parse_pdf.py --pdf paper.pdf --case-file case.json --out ./output
python scripts/selfcheck.py --result ./output/result.json --pdf paper.pdf
```

## 输出（节选）

见 `sample-result.json`。要点：

- `case_id` 来自 case.json，缺省用文件名
- 每个 `section` / `paragraph` / `table` / `figure` 都带 `page`（1 起）与 `bbox`（`[x0,y0,x1,y1]`，PDF point）
- `tables[].confidence` 为 `high` 时表示列对齐可靠；`low` 时 `notes` 里写明原因
- `uncertainty` 列出所有降级与无法判断的地方

> 本目录的 JSON 是**示意输出**：由真实运行结果截断而来，只用于说明字段形状，
> 不代表任何正式案例的答案。
