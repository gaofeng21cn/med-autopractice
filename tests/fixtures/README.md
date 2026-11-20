# 验证样例

本目录的采集包使用虚构患者、站点、文书和时间线。测试覆盖资料冲突、分页遗漏、重复保存和人工接管等场景。新增样例从零构造并标记为合成数据。

验收面见 [实施顺序](../../docs/roadmap.md)。不得将骨架检查代替真实系统验收。

## 运行合成示例

在仓库根目录执行：

```sh
map_demo_dir=$(mktemp -d)
cp tests/fixtures/synthetic-history-collection.json "$map_demo_dir/collection.json"
python3 scripts/structure_record.py \
  --collection "$map_demo_dir/collection.json" \
  --out-dir "$map_demo_dir/output" \
  --record-id synthetic-reference
```

输出目录包含结构化病历、Markdown 审阅稿、研究阅读版和九张 CSV 长表。
