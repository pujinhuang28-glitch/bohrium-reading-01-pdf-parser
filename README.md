# pdf-layout-parser · 使用与方法说明

科研 PDF → 结构化结果（正文/章节/表格/公式/图表/参考文献），**每个元素带页码与 bbox**。

平台运行环境：Linux 容器，`/input/case.json` + 论文 PDF，产物写 `/app/output`。
预算上限 64 次模型请求 / 900 秒。**本 Skill 不调用任何外部模型**，纯本地代码，
解析 16 页论文实测 0.9 秒。

## 1. 最短路径

```bash
python scripts/parse_pdf.py --pdf /input/paper.pdf --case-file /input/case.json --out /app/output
python scripts/selfcheck.py --result /app/output/result.json
```

`/input/case.json` 里若有 `case_id` / `id` 会被写进结果；没有则用 PDF 文件名。

## 2. 输出

```
/app/output/
├── result.json         主结果（见 SKILL.md 的字段表）
├── parsed.md           人读版：正文按阅读顺序 + 表格内联
├── tables/tbl-XX.md    每张表独立 Markdown
└── pages/page-XXX.png  仅 --render 且该页无文字层时生成
```

`status` 三态：`success` / `partial`（有降级或扫描页）/ `failed`（读不了，附错误）。
`uncertainty` 每条都有 `item / reason / impact`，写清"哪里不确定、影响什么"。

## 3. 关键方法（为什么这样做）

### 3.1 字符级重建行，而不是按 span 拼

两端对齐的 PDF 里，词间空格常常不落成空格字符，`span.text` 拼起来就是
`feature-basedandﬁne-tuning`。本工具取 `rawdict` 的**逐字符坐标**，按基线聚行，
再按字距（词间距中位数 ×1.5）补空格。

跨行断字也按这个思路处理：`left-to-` + `right` 要保留连字符（复合词），
`representa-` + `tion` 要去掉（排版断字）。判据是断字处的词干——已有连字符、
或在常见复合词头表里、或下一行以大写/数字开头 → 保留。

### 3.2 栏缝用「行覆盖率」判，不用「完全没有字」

学术论文首页常有整宽的标题块或摘要，会把竖缝整条堵死，按"零字符"判就永远找不到栏。
本工具统计**每条竖缝被多少文字行横跨**，比例低于 38% 就算栏缝；并且只认落在页面中部
（33%–67%）的那一条，避免表格页被大量短行切成 4、5 栏。

### 3.3 三线表（booktabs）单独一条路

学术表格大多只有横线、没有竖线，只认网格的解析器会整张丢掉。本工具：
横线分层做行界 → **双栏页上左右并排的两张表横线在 y 上交错，先按栏缝把横线归到各栏**
→ 行内按基线再分子行（单元格内换行）→ 按文字左边界聚类出列。

两级表头（`Dev | Test` 下面再分 `EM | F1`）会被检测出来；能合就合成
`Dev EM / Dev F1 / Test EM / Test F1`，合不干净就把该表 `confidence` 降为 `low` 并在
`notes` 里写明，**不假装没问题**。

### 3.4 页眉页脚与旋转文字

落在外边距内且跨页重复（数字归一化后比对）或形如页码的行，判为页眉页脚，
从正文剔除、单独记进 `running_furniture`。旋转文字（arXiv 侧边水印）按行方向过滤，
只计数不进入正文。

## 4. 实测表现（BERT 论文 arXiv:1810.04805，16 页双栏）

| 指标 | 结果 |
|---|---|
| 页数 / 有文字层 | 16 / 16 |
| 正文块 | 461 |
| 章节 | 12（Abstract、1、2、2.2、3、3.1、4、5、5.1–5.3、6） |
| 表格 | 6 张，标题全部配对正确 |
| 参考文献 | 53 条 |
| 图 | 5（矢量图，按题注锚定区域） |
| 耗时 | 0.87 秒 |

表格质量：Table 1（GLUE）经独立模型逐格核对**完全正确**（列头 10 列对齐、
BERTBASE Average 79.6、BERTLARGE 82.1 均与原文一致），标 `confidence=high`。
其余 5 张贴着 `confidence=low`：Table 2/3/4/6 的两级表头（`Dev|Test` 下再分 `EM|F1`）
尚未做跨列合并，Table 7 的表头混进了数据行。**低置信度的表不作为可信结构化结果**，
`notes` 里写明原因。

## 5. 依赖与降级

```bash
pip install pymupdf        # 首选
# 或
pip install pdfplumber
```

都不装时退化到 `pdftotext`（poppler）；`--render` 需要 PyMuPDF 或 `pdftoppm`。
**任何一个缺失都不会让程序崩**，后端探测失败会继续往下试，最后一个都不行才 `failed`。

## 6. 失败处理

| 情况 | 行为 |
|---|---|
| 加密 / 损坏 PDF | 全部后端失败 → `status=failed` + 错误类型，退出码 2 |
| 扫描件（无文字层） | `status=partial`，列出页码；`--render` 导出 PNG 供 OCR；不猜字 |
| 无章节标题 | 章节为空但正文保留，`uncertainty` 说明 |
| 表格无画线 | 只在"每行段数一致 + 列左边界对齐"时认，否则不报 |
| 公式 | 只定位 + 编号，不还原 LaTeX 语义 |

## 7. 自查

```bash
python scripts/selfcheck.py --result result.json --pdf paper.pdf
```

检查：`case_id` 非空、`status` 合法、section/paragraph/table/figure 都有合法的
`page` 与 `bbox`、`n_rows` 与 `cells` 一致、有降级却没标 `partial` 会报错。

## 8. 文件

```
SKILL.md              技能入口
scripts/pdfio.py      后端探测 + 字符级读取 + 页面渲染
scripts/layout.py     分行 / 栏缝 / 阅读顺序 / 段落 / 页眉页脚
scripts/structure.py  标题 / 摘要 / 章节 / 图表题注 / 公式 / 参考文献
scripts/tables.py     网格表 + 三线表 + 文本对齐表
scripts/parse_pdf.py  入口，组装 result.json 与 parsed.md
scripts/selfcheck.py  结果自检
examples/             最小调用示例（示意输出，非正式案例）
```
