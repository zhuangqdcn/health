# health

本仓库用于记录和整理健康相关的技能（skills）与代码（code）。

## Garmin 自动归档

使用 [Garmin Personal API Swagger](https://developer.garmin.com/gc-developer-program/personal/swagger-docs/)
和 [Garmin 官方 Python 客户端](https://github.com/garmin/connect-personal-api-python)，
保存 API 支持且账号有权限读取的数据。程序不会修改或删除 Garmin 账号的数据。

### 时间与范围

- 时区默认 `America/Los_Angeles`，即西雅图时间，自动切换 PST/PDT。
- 每天 **09:30** 下载 **昨天 09:00（含）到今天 09:00（不含）** 的信息。
- 夏令时切换日，窗口实际是 23 或 25 小时，不强行按 24 小时计算。
- 保留健康日汇总、心率、HRV、睡眠、步数、压力、Body Battery、SpO2、体重、
  饮水、营养、Fitness Age、周期健康、目标、个人设置和心率区间等原始 JSON。
- 分页检查全部活动，包括多项运动子活动，保存活动详情、workouts 和原始活动 ZIP。
  ZIP 中是否包含 FIT 或其他文件取决于 Garmin 返回的原始格式，不额外伪造 GPX。
- 营养数据中的图片链接保存在 JSON 中；不会访问第三方图片链接下载额外素材。
- 覆盖当前 Swagger 的全部 **26 个 GET 接口**。不代表 Garmin 账号中所有产品、
  所有网页功能的数据都能从 Personal API 导出，未来新增接口需更新程序。

日汇总、睡眠、原始 FIT 等不能都精确裁剪成 09:00–09:00 的数据。
程序保留完整原始数据，在窗口清单中引用覆盖该窗口的日历日期，并按活动的 UTC 时间标注
所属窗口及跨窗口活动，不重新计算虚假的日汇总。Garmin 日历接口使用账号自己的
时区；若账号时区与配置不一致，程序会警告，并按账号时区获取所有重叠日期，
不会擅自重标记日期。若无法读取账号时区，会明确警告并使用配置时区假设，
在清单中记录该假设。

### 安装

需要 **Python 3.12 或更新版本**。Windows PowerShell 示例：

```powershell
python --version
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

如果电脑默认 Python 版本过旧，且安装了 `uv`：

```powershell
uv sync --locked
```

项目的 [uv.lock](./uv.lock) 固定了依赖版本；`uv` 可以下载缺失的 Python 3.12。
无需激活虚拟环境，下面的命令均显式使用虚拟环境中的解释器。

### 本机配置和 API key

本机应有 `.garmin.sync.settings.json` 和 `.env`。本次初始化已创建这两个文件，
它们均被 [.gitignore](./.gitignore) 忽略。重新克隆仓库时，先复制示例，不覆盖已有配置：

```powershell
if (-not (Test-Path .garmin.sync.settings.json)) {
    Copy-Item garmin.sync.settings.example.json .garmin.sync.settings.json
}
if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
}
```

在 Garmin Connect 的 [Personal API Key 设置](https://connect.garmin.com/app/settings/personalApiToken)
中生成 key，在本机 `.env` 填入：

```dotenv
GARMIN_PERSONAL_API_KEY=你的真实key
```

不要将 key 发到聊天、命令行、任务参数或 Git。环境变量 `GARMIN_PERSONAL_API_KEY`
优先于 `.env`；显式设置为空的环境变量会报错，不会回退使用文件中的 key。
API key 会过期，需要你到 Garmin 重新生成并替换。程序自动交换和更新 access token，
遇到 token 提前失效会重新获取一次；不会自动延长 API key 的有效期。

配置格式参见 [garmin.sync.settings.example.json](./garmin.sync.settings.example.json)：

```json
{
  "download_dir": "E:\\GarminData",
  "timezone": "America/Los_Angeles",
  "window_start": "09:00",
  "schedule_time": "09:30",
  "history": {
    "enabled": true,
    "start_date": "2020-01-01",
    "days_per_run": 30
  },
  "include_original_files": true,
  "refresh_recent_days": 3,
  "request_delay_seconds": 0.25,
  "timeout_seconds": 30,
  "retries": 3
}
```

- `download_dir`：绝对路径或相对配置文件所在目录的路径；默认 `garmin-data`。
- `history.start_date`：你希望健康历史覆盖的最早日期，不确定时可填一个足够早的日期。
  首个历史窗口从该日 09:00 开始，同时保存该日完整日历数据。
- `history.days_per_run`：每次最多补齐的历史窗口数，默认 30；可重复手动运行，
  或等待后续每日任务继续，避免首次请求过多。已完成且文件完整的历史数据会跳过。
- `refresh_recent_days`：每日重新检查最近几个窗口，补充延迟上传/更新的数据。
  同一天重复运行复用已下载的完整文件；活动目录每次都重新分页查询。
- `include_original_files`：是否同时下载活动原始 ZIP，默认开启。

**历史边界限制：** 当前 Swagger 没有查询最早健康记录或账号创建日期的接口。
默认 `start_date: null` 时，程序仍会遍历全部活动历史、同步近期健康数据，
并补齐自首次运行以来的漏跑窗口，但会明确返回“需要健康历史起始日期”，
不会将第一次活动的日期或连续空响应误判为全部健康历史的边界。
填写日期后才会补齐该日期以来的健康历史。服务器不提供的数据无法恢复。

`history.enabled: false` 会关闭首次使用前的历史健康补齐及全历史活动文件下载；
近期数据和自首次运行以来漏跑的健康窗口仍会补齐。

### 运行

```powershell
# 只验证配置，不读取 key、不访问 API
.\.venv\Scripts\python.exe garmin_sync.py --check-config

# 立即同步；首次运行包含活动历史及分批健康历史
.\.venv\Scripts\python.exe garmin_sync.py

# 显式指定配置；默认从该配置旁边读取 .env
.\.venv\Scripts\python.exe garmin_sync.py --settings "E:\Private\garmin.json"

# 指定独立的凭据文件
.\.venv\Scripts\python.exe garmin_sync.py --env "E:\Private\garmin.env"

# 强制重新获取本次选中的文件（历史健康窗口仍受每次批次上限约束）
.\.venv\Scripts\python.exe garmin_sync.py --force

# 只下载 2017 年之前的数据；健康历史从配置的 start_date 开始，活动包含更早记录
.\.venv\Scripts\python.exe garmin_sync.py --before 2017-01-01

# 小批量试跑：最多补齐 3 个尚未下载的健康日历日期
.\.venv\Scripts\python.exe garmin_sync.py --before 2017-01-01 --days-per-run 3

# 逐日下载 2017 年 1–2 月（含 2 月 28 日），同时限制活动范围，不修改本地配置
.\.venv\Scripts\python.exe garmin_sync.py --start-date 2017-01-01 --before 2017-03-01 --days-per-run 59

# 一次连续补齐 2017-01-01 到昨天的历史，不受 days_per_run 批次上限限制
.\.venv\Scripts\python.exe garmin_sync.py --all-history --start-date 2017-01-01
```

`--before` 是独立的历史导出模式，不下载近期健康数据、不更新定时任务的完成状态。
健康数据使用 Garmin 账号的日历日期（上限日期不包含）；活动同时通过接口本地日期
过滤及配置时区的午夜边界检查。`--start-date` 同时覆盖本次健康历史和活动的起点，
不会写回本地配置。未指定时，健康起点来自配置，活动包含上限之前的更早记录。
历史导出也读取当前个人设置及心率区间设置；这两个接口不提供过去某天的配置版本。
每天检查全部 20 个日期型接口，不因前一天某个接口返回 403 而省略当天的请求；
活动列表及查到的每项活动的详情、workouts 和原始文件组成另外 4 个接口。
没有活动时，不能构造活动 ID 调用后面 3 个接口。
活动历史会遍历上限之前的全部记录；`--days-per-run` 仅限制健康日期，不限制活动数。
同一命令可反复执行，跳过已保存且校验完整的历史日期，直到报告中的
`health_history_pending` 为 `false`。导出报告保存在
`reports\history-before-2017-01-01.json`；此模式保留原始日历数据，
不生成正常同步使用的 09:00–09:00 窗口清单。

`--all-history` 使用配置的历史起点，也可通过 `--start-date` 覆盖，结束于昨天
这个完整日历日期。今天尚未完成的数据留给正常日同步。该模式至少间隔 1 秒发起
请求，并使用同样的 429 冷却机制；不会改写配置。
进度文件 `reports\history-before-结束日期-progress.json` 持续更新日期、PID、
阶段、活动处理数量和错误数，可用来检查后台任务是否还在推进。

历史同步恢复时，未变化且曾返回 400 的请求会保留为**失败**，不重复消耗请求额度；
它们仍计入报告错误数，不会登记为成功。使用 `--force` 或修正对应请求参数后可重试。
这种已记录的永久失败不会反复占用历史批次名额、导致下载一直卡在最早几个日期。
500、网络失败和 429 等临时失败不会这样跳过。`weight/all` 的已知 400 与
`weight/range?includeAll=true` 的独立原始数据分别记录，不能把后者宣称为前者成功。

活动列表变化会触发对应活动详情及原始文件刷新；旧活动仅在列表内容变化、
文件缺失/损坏或 `--force` 时重新下载。服务器未在活动列表中反映的旧详情修改，
需要通过 `--force` 主动刷新。

进程退出码：

| 退出码 | 含义 |
| --- | --- |
| 0 | 本次成功，或定时检查当前不需要运行；历史可能仍在分批补齐，见运行报告 |
| 1 | 配置、凭据、网络、文件、接口错误，或窗口归属不完整 |
| 2 | 当前数据已同步，但需要配置 `history.start_date` 才能补齐健康历史 |
| 3 | HTTP 429 重试次数耗尽；已保存服务器冷却期限，稍后重跑可断点续传 |

403（无权限）及 404（资源不存在）会明确记录在清单中，与成功下载、空数据及请求失败区分。
无权限数据不会阻塞其他数据；近期端点会在后续日期重试，检测到权限恢复后清理对应历史
403 状态并重新补齐。仅某个历史资源返回的 404 或局部 403 可用 `--force` 重试。
429 和临时服务错误会有限重试，仍失败则退出，不会假装完成。

429 限流处理：

- 完整遵守 `Retry-After` 秒数或 HTTP 日期，不再将等待时间截断为 120 秒。
  HTTP 日期优先参考服务器 `Date`，并额外等待 1 秒以避开额度窗口取整边界。
- 无效或缺失的 `Retry-After` 使用从 30 秒开始的指数退避；`0` 或过期日期
  也不会立即重试。
- token 获取和数据下载共用请求节奏。遇到 429 后自动降低频率并持久化，
  后续请求不会立刻恢复到触发限流前的速度。
- `.state\rate-limit.json` 保存冷却期限及学习到的请求间隔。即使重试耗尽或进程
  中断，重新运行也会等待剩余冷却时间后再发出 API 请求。
- 重试总次数仍受 `retries` 限制。耗尽后退出码为 `3`，报告状态为 `rate_limited`，
  提供 `retry_at`；该请求不会登记成成功。重跑相同命令会复用已完成的文件。

已知接口兼容性：实测历史日期的 `weight-service/weight/all` 请求可能在按
Swagger 规定传入 ISO 日期时返回 HTTP 400，且正文仅为 `{}`。程序会将其保留为
失败，不把它转换成“无数据”或将其他体重接口冒充该接口的成功结果。
`weight-service/weight/range` 的 `includeAll=true` 请求仍会独立保存；
遇到这种情况，查看运行报告中的失败项目，不能据此宣称全部接口已经导出完成。

### Windows 每日任务

先手动成功运行一次，确认 key 和目录正确，再注册：

```powershell
.\scripts\Register-GarminSyncTask.ps1

# 查看任务状态
Get-ScheduledTaskInfo -TaskName GarminPersonalSync

# 输出任务 XML 供检查，不注册
.\scripts\Register-GarminSyncTask.ps1 -PrintXml

# 删除任务，不删除下载的数据
.\scripts\Register-GarminSyncTask.ps1 -Remove
```

若执行策略禁止运行本地脚本，可以只对这次命令使用：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Register-GarminSyncTask.ps1
```

任务使用绝对路径及当前用户权限，不保存密码或 API key 到任务定义中。
默认使用虚拟环境的 `pythonw.exe`，避免每分钟弹出控制台窗口。
**当前注册方式要求该 Windows 用户保持登录**；可锁屏，不要求 VS Code 保持打开。
关机期间不会下载，重新登录后会检查并补跑。休眠唤醒由 Windows 电源策略决定。

为避免电脑系统时区与西雅图时区不同或夏令时切换引起偏移，Windows 任务每分钟启动
一次轻量检查，由 Python 使用配置时区判断当日 09:30 是否到达。成功后同一个
到期日不会再次访问 API；失败时最多每 30 分钟重试。09:30 之前首次启用任务
只补跑上一到期日，不提前下载今天尚未到期的窗口。
分批健康历史默认每个到期日执行一批，可手动重复运行加速补齐。

### 下载结构与恢复

```text
garmin-data\
  daily\2026-10-21\*.json
  activities\<activity-id>\summary.json
  activities\<activity-id>\workouts.json
  activities\<activity-id>\original.zip
  activity-index\page-*.json
  snapshots\2026-10-22\*.json
  windows\2026-10-22.json
  last-run.json
  .state\sync.sqlite3
  .state\token.json
  .state\sync.log
  .state\sync.lock
```

窗口文件名使用结束日期，例如 `2026-10-22.json` 对应 10 月 21 日 09:00 到
10 月 22 日 09:00。清单包含 UTC/本地边界、原始文件引用、SHA-256、
接口状态及无法确定 UTC 起点的活动。

下载采用临时文件加原子替换，校验 ZIP 和已登记文件的 SHA-256。
中途退出后可以直接重跑；删除或损坏的历史文件会重新下载。
操作系统文件锁避免多个进程同时写入同一目录。运行日志会轮转，不记录 key、
token 或接口错误响应正文。

`.env`、本地配置、默认下载目录和虚拟环境都已忽略。**自定义到仓库内的其他下载目录
需要自行加入 Git 忽略规则**；建议将私有数据放到仓库外。token 缓存和健康数据仍是
本机明文文件，请限制目录访问权限，不要同步到公开位置。
一个下载目录只用于一个 Garmin 账号；切换账号、窗口时间或时区时使用新目录。
为保留断点记录，不要删除 `.state`。目录移动后文件引用仍是相对路径，可以继续使用。

### 离线测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试使用临时目录和模拟接口，不读取真实 key、不访问 Garmin，也不创建实际 Windows
任务。真实下载和任务执行需要本机 key、网络及用户登录，不能由离线测试替代。