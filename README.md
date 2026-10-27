# ScienceDirect 期刊卷抓取

脚本内置 Chrome 启动和配置逻辑，并通过 Playwright 抓取某卷内所有文章的标题、作者和 DOI，不依赖 `chrome_launcher.py`。

```bash
cd /home/zhiping/knowledge_base_tools/fetch_sciencedirect_journal
../Download_paper/.venv/bin/python fetch_sciencedirect_journal.py \
  computer-physics-communications --vol 317
```

默认输出到当前脚本目录下的 `computer-physics-communications_vol_317.json`。也可指定路径：

```bash
../Download_paper/.venv/bin/python fetch_sciencedirect_journal.py \
  computer-physics-communications --vol 317 -o result.json
```

如果某卷分期，可以在卷号后传入期号：

```bash
../Download_paper/.venv/bin/python fetch_sciencedirect_journal.py \
  computer-physics-communications --vol 185 --issue 12
```

此时访问地址包含 `/vol/185/issue/12`，JSON 中会增加 `"issue": "12"`，默认输出文件名为 `computer-physics-communications_vol_185_issue_12.json`。

如果请求的 issue 不存在而 ScienceDirect 跳转到了其他 issue，脚本会以浏览器最终 URL 中的实际 issue 为准。例如请求 `vol/42/issue/4` 后跳转到 `vol/42/issue/3`，JSON 和默认文件名都会记录 issue 3。

如果传入了 issue，但最终跳转到 `/vol/.../suppl/...` 或其他不含 `/issue/` 的地址，JSON 会写入 `"issue": null`，默认文件名不包含 issue 后缀。完全不传 issue 时则仍然省略该字段。

part 是可选的字符串参数，默认不传。例如：

```bash
../Download_paper/.venv/bin/python fetch_sciencedirect_journal.py \
  journal-of-computational-physics --vol 521 --issue 2 --part P2
```

请求 URL 为 `/vol/521/issue/2/part/P2`。如果最终跳转到 `/vol/521/part/P2`，JSON 会记录 `"issue": null` 和 `"part": "P2"`，默认文件名为 `journal-of-computational-physics_vol_521_part_P2.json`。只传 part 时可直接使用 `journal-of-computational-physics --vol 257 --part PB`，无需传空 issue。

Chrome 会保持打开。如果已有监听 9222 端口的 Chrome，脚本会直接复用它。遇到验证码时不要使用 `--headless`，在打开的浏览器中完成验证即可。

打开卷或期页面时，导航请求会携带期刊主页作为 `Referer`，例如 `Referer: https://www.sciencedirect.com/journal/computer-physics-communications`。

## 批量抓取

`batch_fetch_sciencedirect_journal.py` 支持闭区间形式的 volume 和 issue 范围：

```bash
/home/zhiping/miniconda3/envs/dev/bin/python batch_fetch_sciencedirect_journal.py 36-173 1-5
```

默认成功后等待 60 秒，失败后等待 300 秒；每实际访问 30 个页面后长休眠 1800 秒（替代该次普通间隔）。可用 `--delay`、`--error-delay`、`--long-break-every`、`--long-break`、`--skip-existing` 和 `--stop-on-error` 调整批处理行为。

批处理中 part 默认为空；如需为每次请求添加同一个 part，可使用 `--part P2`。

suppl 与 part 的处理方式相同，默认空且不写入 URL。例如：

```bash
../Download_paper/.venv/bin/python fetch_sciencedirect_journal.py \
  computer-physics-communications --vol 3 --suppl S1
```

将访问 `/vol/3/suppl/S1`，JSON 增加 `"suppl": "S1"`，默认文件名为 `computer-physics-communications_vol_3_suppl_S1.json`。批处理可用 `--suppl S1` 为每次请求添加同一个 suppl。
