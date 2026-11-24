---
name: pdf-layout-parser
description: 把科研 PDF 解析成带页级证据的结构化结果。当任务要求从论文 PDF 中提取正文、章节层级、表格、公式、图表题注与参考文献，并且每个结果都要能指回原文页码与版面位置时使用。解决双栏串栏、整宽元素打断分栏、词间空格丢失、三线表无竖线、扫描件无文字层这些常见失败。不用于论文精读结论生成、科研绘图美化或语言润色。
---

# 科研 PDF 结构化解析

把一篇 PDF 变成**可复核**的结构化结果：正文按正确阅读顺序、章节层级清楚、表格还原成行列、
每个元素带页码与 bbox。核心不是"抽出一段看起来像全文的文本"，而是**每条结果都能回溯到原文位置**。

## 什么时候用

- 输入是一篇论文 PDF，要求输出正文/章节/表格/公式/图表/参考文献的结构化数据
- 要求给出依据：页码、段落、图表、表格位置
- 要求说明哪里没读懂、哪里不确定，而不是补全

**不适用**：纯写作润色、图表美化、从解析结果推断科学结论。

## 五分钟上手

```bash
python scripts/parse_pdf.py --pdf /input/paper.pdf --out /app/output
```

在评测容器里，输入通常在 `/input/case.json` + 论文 PDF：

```bash
python scripts/parse_pdf.py --pdf /input/paper.pdf --case-file /input/case.json --out /app/output
```

产出：

| 文件 | 内容 |
|---|---|
| `result.json` | 主结果：`case_id`、`status`、文档元信息、章节树、表格、图、公式、参考文献、页级证据、不确定性 |
| `parsed.md` | 人读版本，正文按阅读顺序 + 表格内联 |
| `tables/tbl-XX.md` | 每张表单独的 Markdown |
| `pages/page-XXX.png` | 仅当加 `--render` 且该页无文字层时渲染，供 OCR 留证 |

退出码：`0` 成功，`1` 部分完成（有不确定性），`2` 读不了 PDF。

## 它到底做了什么

五层，逐层都可单独验证：

1. **读取（pdfio.py）** — 依次探测 PyMuPDF → pdfplumber → pdftotext → pypdf，用哪个写进
   `input.backend`。字符级坐标优先；只有纯文本时降级并在 `uncertainty` 里说明。
2. **版面（layout.py）** — 字符按基线聚行，**按行覆盖率找栏缝**（不是"完全没字"，
   否则整宽的摘要/大图会把栏缝堵死），整宽行当分隔带，带内先左栏后右栏。
   页眉页脚按"外边距 + 跨页重复/页码模式"剔除，只记录不混入正文。
3. **结构（structure.py）** — 标题取首页最大字号；章节靠编号与字号/加粗双线索，并排除表格区域内的
   数字串；参考文献按"作者开头 + 年份"分条。
4. **表格（tables.py）** — 两种模式：有竖线的走单元格网格；**只有横线的三线表（booktabs）**
   先按栏缝把横线归到各栏（双栏页上左右两张表横线在 y 上交错），再在栏内分带、按基线切行、
   按文字左边界聚类出列。没有画线的表只在"每行段数一致 + 列左边界跨行对齐"时才认，否则不报。
5. **证据（全程）** — 每个 section/paragraph/table/figure/formula/reference 都带 `page` 与 `bbox`。

## 三个容易踩的坑，本 Skill 的处理

| 坑 | 表现 | 处理 |
|---|---|---|
| **双栏串栏** | 左右两栏同一基线的文字被并成一句 | 字符级分行 + 栏缝切分；栏缝用行覆盖率判定 |
| **词间空格丢失** | `feature-basedandﬁne-tuning`：两端对齐的 PDF 里空格不落成字符 | 逐字重建，按字距补空格；跨行断字只在确认是排版断字时才去连字符（`left-to-` + `right` 保留连字符） |
| **三线表没有竖线** | 只认网格的解析器会整张表丢掉 | booktabs 模式：横线定行、文字位置定列 |

## 输出契约

`result.json` 顶层字段：

```jsonc
{
  "case_id": "...",              // 来自 /input/case.json 的 case_id / id，缺省用文件名
  "status": "success|partial|failed",
  "input":   { "path","pages","backend","backend_chain","backends_available" },
  "document":{ "title","authors","abstract","abstract_ref","body_font_size" },
  "sections":[ { "id","title","level","page","bbox","paragraphs":[{"id","text","page","bbox"}] } ],
  "tables":  [ { "id","page","bbox","caption","n_rows","n_cols","cells","markdown","html","confidence","detection" } ],
  "figures": [ { "id","page","bbox","caption","graphic","located_by" } ],
  "formulas":[ { "text","equation_number","page","bbox","signal" } ],
  "references":[ { "id","number","raw","year","page" } ],
  "coverage":{ "pages","blocks","sections","tables","figures","formulas","references" },
  "uncertainty":[ { "item","reason","impact" } ],
  "changes":  [ ... ]
}
```

**每个元素都有 `page` 与 `bbox`**，这是"可核验"的全部依据：拿 bbox 回原文一框就能对。

## 异常与边界

| 情况 | 行为 |
|---|---|
| 扫描件 / 无文字层 | `status=partial`，`uncertainty` 写明哪些页；`--render` 可导出 PNG 供 OCR；不凭空补字 |
| PDF 加密 / 损坏 | 各后端全部失败 → `status=failed`，返回错误类型与信息，退出码 2 |
| 只有纯文本后端 | `uncertainty` 标 `layout: degraded`，分栏与表格置信度下调 |
| 识别不到章节 | 章节为空但正文保留在 `parsed.md`，`uncertainty` 说明 |
| 表格认不准 | 宁可不报也不猜：无画线的表要求列对齐才认，`confidence=low` |
| 公式 | 只按版面定位 + 编号识别，**不还原 LaTeX 语义**，并在 `uncertainty` 声明 |

## 已知限制

- **两级表头（`Dev|Test` 下再分 `EM|F1`）尚未做跨列（colspan）合并**：这类表会标 `confidence=low`，
  列标签可能错位。已尝试按"文字横向覆盖范围"推断跨列，但列聚类在 SQuAD 那类表上仍会并错，
  故当前**默认关闭**该路径，宁可标 low 也不虚报 high
- 三线表若数据行之间没有横线、且单元格文字换行，可能把换行当成新行
- 表头若混入数据行（如 Table 7 的情况），会通过"表头含小数"检出并降为 `low`
- 旋转文字（如 arXiv 侧边水印）只计入 `page_layout.rotated_chars`，不进入正文
- 图表只做**位置与题注**，不理解图形内容
- 不处理跨页续表合并（两页各自成表，`page` 字段如实标出）

## 依赖

- Python 3.10+
- **必需**：PyMuPDF（首选）或 pdfplumber；都没有时退化到 `pdftotext`（poppler）
- 可选：`--render` 需要 PyMuPDF 或 `pdftoppm`
- `requirements.txt` 只列可选依赖，缺失不会导致崩溃——后端探测会自己降级

## 自检

```bash
python scripts/selfcheck.py --result /app/output/result.json
```

检查：`case_id` 非空、`status` 合法、每个 section/paragraph/table 都有 `page` 与 `bbox`、
bbox 在页内、`uncertainty` 与 `status` 一致（有降级却没标 partial 会报错）。
