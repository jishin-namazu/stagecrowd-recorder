# stagecrowd-recorder

容器内归档 Widevine 加密的 HLS 直播流。取密钥、下载、解密、封装。

## 功能

| 能力 | 说明 |
|---|---|
| 密钥获取 | 用本地 CDM 重放 license 请求，取出 content key |
| 覆盖校验 | 校验 content key 是否覆盖实际录制的轨道 |
| 轮换监视 | 录制期间定期复查，密钥轮换时告警 |
| 下载解密 | 交给 N_m3u8DL-RE + shaka-packager |
| 边录边播 | 混流文件按播放速率写入，录制中即可播放 |
| 本地 HLS | 同一 ffmpeg 进程边归档边生成滑动窗口 m3u8，可从 127.0.0.1 实时播放 |
| 分片保留 | 默认保留已解密分片，中断后可重建 |
| 同链共享 | 同一播放 URL 跨会话复用首次录制的分片目录，各次输出独立命名 |
| CDN 补片 | 自动探测连续分片索引，仅下载并解密缺失分片 |
| 重建 | 从分片重建可播放文件，逐轨报告缺失 |

## 部署

需要 Docker 和 Docker Compose。

### 1. 准备配置文件

项目根目录放一个 `.env`（compose 挂载后在容器内成为 `/config/.stagecrowd`）：

```ini
m3u8=https://fastly.live.brightcove.com/.../playlist-hls.m3u8
wv_token=eyJhbGciOiJIUzI1NiIs...
```

`m3u8` 是流地址，建议指向 master 播放列表。`wv_token` 是从播放页面取到的 license session token。

### 2. 准备 CDM

把 Widevine 设备文件放在项目根目录，命名 `device.wvd`。

没有 CDM 时可以跳过这步，改用 `--key` 直接提供密钥。此时需要注释掉 `compose.yaml` 里这一行：

```yaml
# - ./device.wvd:/config/device.wvd:ro
```

### 3. 构建

```powershell
docker compose build
```

可选的自检：

```powershell
docker compose run --rm recorder probe
```

### 4. 开始录制

```powershell
docker compose up
```

按 `Ctrl+C` 停止。

建议在直播**开始之前**运行：HLS 播放列表为滑动窗口，不含历史分片；CDN 仍保留对应
对象时，结束后可用 `backfill` 补齐。

## 命令

| 命令 | 作用 |
|---|---|
| `capture` | 完整流程：解析流 → 取密钥 → 覆盖校验 → 录制 |
| `plan` | 同上，仅打印将执行的命令（dry run） |
| `keys` | 只取密钥并打印覆盖情况 |
| `backfill` | 探测 CDN 可访问索引并补齐本地缺片 |
| `rebuild` | 从保留的分片重建可播放文件 |
| `probe` | 环境自检，不需要流地址和 token |

## 参数

`capture` / `plan` / `keys` 共用：

| 参数 | 说明 |
|---|---|
| `--url URL` | m3u8 地址 |
| `--out DIR` | 输出目录，默认 `run_<时间戳>.out` |
| `--token TOK` | license session token |
| `--license-url URL` | 完整 license 地址，优先于 `--token` |
| `--headers-file FILE` | `--license-url` 用的请求头，每行 `Name: value` |
| `--key KID:KEY` | 直接提供密钥，可重复或逗号分隔。跳过 license 步骤 |
| `--cdm PATH` | 设备文件路径，默认 `/config/device.wvd` |
| `--decryptor {SHAKA_PACKAGER,MP4DECRYPT}` | 解密引擎，默认 `SHAKA_PACKAGER` |
| `--allow-partial-keys` | 允许部分轨道无密钥；无密钥轨道不解密 |
| `--discard-shards` | 不保留分片，节省磁盘；无法 `rebuild` |
| `--burst-output` | 立即落盘，不随播放速率限速；录制中的文件不可播放 |
| `--hls [HOST:PORT]` | 提供滑动窗口 `live.m3u8`；不写地址时监听 `127.0.0.1:8080` |
| `--quiet-shards` | 不在控制台打印逐分片进度 |
| `--no-shard-log` | 不写逐分片进度日志 |
| `--verbose-downloader` | 显示下载器自身的日志 |
| `--guard-interval S` | 密钥轮换复查间隔，默认 240 秒 |
| `--settings FILE` | 配置文件路径。未指定时：设了 `$STC_SETTINGS` 就只用它，否则找 `./.stagecrowd` 或 `./.env` |

`backfill` / `rebuild` / `probe` 不接受 `--settings`，要指定配置文件请写在子命令**之前**
（`stagecrowd-recorder --settings FILE probe`）或用 `$STC_SETTINGS`。配置文件对所有命令
生效；`rebuild` 不读取任何配置项，`probe` 的 `--cdm` 默认值取自配置文件注入的环境变量。

退出码：`0` 成功，`1` 环境/工具链检查未通过，`2` 运行时错误，`130` 用户中断。
参数写错（含对 `backfill` / `rebuild` / `probe` 用 `--settings`）由 argparse 报告，同样是 `2`。

`backfill` 参数：

| 参数 | 说明 |
|---|---|
| `TARGET` | 运行的 `.out` 目录，或对应分片目录 |
| `--rate-limit RATE` | 顺序下载限速，默认 `3M`；可写 `512K`、`3M`，`0` 表示不限速 |
| `--scan-only` | 仅报告 CDN 索引范围与本地缺片数量，不下载 |
| `--include-live-tail` | 录制仍在进行时也补最新尾部；默认关闭 |

## 自动补齐 CDN 切片

推荐把运行的 `.out` 目录作为 `TARGET`。程序会从 `run.json` 找到共享分片目录，读取
`meta_selected.json` 中的 CDN URL 模板，再自动完成以下操作：

1. 以 HEAD 请求探测 CDN 可访问索引的上下界。
2. 把共享目录中的本地时间戳映射回 CDN 索引。
3. 仅下载缺失索引，跳过已有分片。
4. 用 `keys.txt` 和对应 init 解密，写回同一播放 URL 的共享目录。
5. 从分片内部 `tfdt` 校准文件名，保留音频的毫秒级边界。

### 1. 先扫描

```powershell
docker compose run --rm recorder backfill /archive/run_20260823_091907.out --scan-only
```

输出分别列出音视频的 CDN 索引范围及本地、缺失、已恢复数量。例如：

```text
video: CDN 297910143..297914449; local 4196; missing 111; recovered 0
audio: CDN 297910143..297914449; local 4195; missing 112; recovered 0
```

### 2. 限速补齐

以下命令把顺序下载总速度限制为 `3 MiB/s`：

```powershell
docker compose run --rm recorder backfill /archive/run_20260823_091907.out --rate-limit 3M
```

命令幂等，可重复执行：已落盘索引跳过，中断后重跑续传。任一索引最终失败时退出码为
`2`，并打印失败索引。

录制仍在进行时，默认仅补齐至本地最新索引。需要同时补齐 CDN 最新尾部时：

```powershell
docker compose run --rm recorder backfill /archive/run_20260823_091907.out --rate-limit 3M --include-live-tail
```

### 同一链接的共享目录

同一播放 URL 再次录制时，下载器仍使用新的 `run_<时间>` 会话目录；init 与解密分片
以硬链接同步至该 URL 首次录制的分片目录。各次 `.ts` 输出独立，`run.json`、`backfill`
和 `rebuild` 均指向共享目录。硬链接位于同一归档卷时不重复占用媒体数据空间；文件系统
不支持硬链接时退化为复制。

补片要求共享目录仍保留 init 和 `meta_selected.json`，对应 `.out` 目录仍保留 `run.json`
与 `keys.txt`。CDN URL 的分片文件名必须以连续数字结尾，例如
`media_hls1080p_297912786.mp4`。

补齐完成后可直接从共享分片重建：

```powershell
docker compose run --rm recorder rebuild /archive/run_20260823_091907.out
```

## 常见用法

```powershell
# 默认录制
docker compose up

# 已有密钥的离线录制，不需要 CDM
docker compose run --rm recorder capture --url "<m3u8>" --key kid1:key1 --key kid2:key2

# 只取密钥并检查覆盖，不录制
docker compose run --rm recorder keys --url "<m3u8>" --token "<token>"

# 先扫描，不下载
docker compose run --rm recorder backfill /archive/run_20260823_091907.out --scan-only

# 以 3 MiB/s 补齐所有历史缺片；录制中的最新尾部默认跳过
docker compose run --rm recorder backfill /archive/run_20260823_091907.out --rate-limit 3M

# 从中断的录制重建
docker compose run --rm recorder rebuild /archive/run_20260803_091917.out
```

## 通过 m3u8 实时播放

`docker compose up` 默认开启本地 HLS。录制开始并产生第一个分片后，播放器打开：

```text
http://127.0.0.1:8080/live.m3u8
```

例如使用 ffplay：

```powershell
ffplay -fflags nobuffer -flags low_delay "http://127.0.0.1:8080/live.m3u8"
```

播放列表为滑动窗口，保留最近 6 个分片；完整内容归档到 `.ts` 文件。停止录制时服务
一同停止。

不使用 Compose、直接在宿主机运行时，加一个不带值的 `--hls`：

```powershell
stagecrowd-recorder capture --url "<m3u8>" --key "<kid:key>" --hls
```

可以用 `--hls 127.0.0.1:9090` 更换端口。`--hls` 与 `--burst-output` 互斥。

## 配置

优先级：命令行参数 > 环境变量 > 配置文件 > 默认值。

配置文件只认 `KEY=value`，支持短别名：

| 别名 | 规范变量 |
|---|---|
| `m3u8` / `url` / `stream` | `STC_URL` |
| `wv_token` / `token` | `STC_TOKEN` |
| `license` / `license_url` | `STC_LICENSE_URL` |
| `key` / `keys` | `STC_KEYS` |
| `cdm` / `device_wvd` | `STC_CDM` |
| `out` / `output` | `STC_OUT` |
| `headers` | `STC_HEADERS` |
| `hls` / `hls_address` | `STC_HLS` |

其余变量：`STC_SETTINGS`、`STC_DOWNLOADER`、`STC_FFMPEG`、`STC_SHAKA`、
`STC_MP4DECRYPT`、`STC_HLS`、`HTTPS_PROXY`、`NO_COLOR`。
