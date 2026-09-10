# TransIt — Mtools 翻译文件 AI 精翻流水线

[![Release](https://img.shields.io/github/v/release/687jsassd/TransIt?color=7c5cff&label=release)](https://github.com/687jsassd/TransIt/releases)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows-38bdf8.svg)](#快速开始推荐webui)
[![Python](https://img.shields.io/badge/python-3.8%2B-3776ab.svg)](#方式二源码运行开发改代码)
[![Dependencies](https://img.shields.io/badge/dependencies-0-brightgreen.svg)](TransIt.spec)

从游戏（如 RPG Maker）导出的翻译文件往往是 `{原文: 译文}` 的 JSON 键值对
（Mtools `ManualTransFile.json` 格式）。本工具读取这类文件，自动完成：

1. **读取**：解析 JSON，识别需要翻译的内容（自动跳过数字/ID/代码类条目）
2. **分析**：采样文本 → 让 LLM 分析世界观与故事，提取角色名、物品名、势力/组织、
   地名、行业术语、特殊口癖等专有名词，建立术语库
3. **精翻**：带世界观上下文 + 术语库约束的批量翻译（OpenAI 兼容 API，任意自定义端点），
   支持并发、断点续传、占位符保留
4. **导出**：输出与输入完全同格式的完整 JSON（未翻译条目原样保留，可继续补齐）

## 特性

- ✅ **WebUI 图形界面**：现代深色主题浏览器界面（双击 `启动TransIt.bat` 即用），
  可视化进度、术语库编辑、逐句译文修正、API 配置，纯标准库零依赖
- ✅ **hash 隔离**：按文件内容 SHA-1 区分不同翻译对象——不同游戏的
  `ManualTransFile.json` 输出互不覆盖、互不串译
- ✅ **行上下文组合翻译**：mtool 按行提取会把原文截断成碎片，翻译时自动按原文顺序
  注入前后邻行作为上下文，让碎片句正确衔接（可选，窗口大小可调）
- ✅ **零第三方依赖**：纯 Python 标准库（urllib / http.server / tkinter），开箱即用
- ✅ **OpenAI 兼容**：任意 base_url + api_key + model（SiliconFlow / OpenAI / DeepSeek / 本地 vLLM 等）
- ✅ **术语库可编辑**：分析后自动生成 `*.glossary.json`，可手动增删改词条
- ✅ **R18 忠实翻译**：不回避、不弱化成人内容，explicit/moderate 两档可选
- ✅ **名词风格可选**：auto（按角色气质）/ kawaii（可爱化）/ simple（简洁）/ transliterate（标准音译），
  禁生僻字、全篇译名一致
- ✅ **精修润色轮**（可选）：长句/诗歌/谜语类文本二次润色，更优美自然
- ✅ **自定义提示词**：附加到每条翻译指令，个性化风格与用词
- ✅ **防错位合并**：键容错匹配 + 占位符交叉校验，杜绝"张冠李戴"的错位译文
- ✅ **占位符保护**：`<SG...>`、`\n`、`\v[1]`、`{数字}` 等控制符原样保留
- ✅ **断点续传**：每批完成即保存进度，中断后重跑自动跳过已完成条目
- ✅ **任务可停止**：WebUI/停止信号在批次边界安全生效，进度不丢失
- ✅ **实时进度**：终端打印百分比+ETA，同时写入 `.translate.log` 日志文件
- ✅ **思考功能默认禁用**：翻译任务关闭模型 reasoning，速度大幅提升（可配置开启）
- ✅ **超长文本单独小批**：防止上下文/输出截断
- ✅ **省 token**：术语库按批次文本子串过滤，只注入本批相关词条
- ✅ **本地接口自带防护**：WebUI 只监听 `127.0.0.1`，并对 `/api/*` 校验 `Host` 白名单
  （挡 DNS 重绑定）与 `Origin` 白名单（挡 CSRF），响应带 `X-Frame-Options: DENY`
  （挡点击劫持）—— 其他网页既发不出能通过校验的请求，也读不到响应。
  默认**不需要**任何令牌，正常用浏览器访问即可；需要额外一层可选用 `--token`
- ✅ **可打包为绿色便携版**：`build.ps1` 一键产出免安装 ZIP（详见「打包与发布」）

## 快速开始（推荐：WebUI）

### 方式一：便携版（普通用户，无需装 Python）

从 release 下载 `TransIt-<版本>-win64.zip`，解压到任意目录，双击 **`TransIt.exe`**。
数据（配置/进度/成品）都保存在解压目录内，整个文件夹拷走即可迁移。
详见包内 `使用说明.txt`。

### 方式二：源码运行（开发/改代码）

**双击 `启动TransIt.bat`** —— 自动启动服务并打开浏览器界面（默认 `http://127.0.0.1:8765`）。

界面包含 4 个页面：

| 页面 | 功能 |
|------|------|
| **工作台** | 选择/浏览文件、统计总览、分析→翻译→精修→导出 流水线按钮、实时进度条（速度/ETA）、运行日志 |
| **术语库** | 世界观/翻译注意编辑、词条增删改（双击单元格）、搜索过滤、自动合法性过滤 |
| **译文修正** | 全部条目浏览/搜索/只看未翻译、双击逐句修正（即时写回进度文件）、分页 |
| **设置** | API（base_url/key/model/温度/max_tokens/超时/思考开关+连接测试）、流水线参数、翻译风格（R18/名词风格/精修/自定义提示词） |

CLI 模式：

```bash
# 1. 修改 config.json 填入你的 API 信息（默认已配 SiliconFlow）
# 2. 分析世界观 + 建立术语库
python transit_cli.py analyze "示例翻译文件\ManualTransFile.json"

# 3. 批量精翻（断点续传；--limit N 可只译前 N 条测试）
python transit_cli.py translate "示例翻译文件\ManualTransFile.json" --limit 100

# 4. 导出最终翻译文件（与输入同格式）
python transit_cli.py export "示例翻译文件\ManualTransFile.json"
```

或一条命令全流程：

```bash
python transit_cli.py run "示例翻译文件\ManualTransFile.json"
```

## GUI 使用（tkinter 备选界面）

```bash
python transit_gui.py
```

界面包含 4 个页签：

| 页签 | 功能 |
|------|------|
| **API 配置** | 修改 base_url / api_key / model / 温度 / 并发 / 批大小 |
| **名词库** | 加载/保存/新增/删除/双击编辑词条（带滚动条）；编辑世界观与翻译注意 |
| **翻译** | 一键运行分析（建库，进度条）→ 开始翻译（实时进度条+日志）→ 导出；可调 R18 风格、精修润色、自定义提示词 |
| **译文修正** | 搜索原文/译文（带滚动条），双击逐句修正，保存回进度 |

### 翻译页附加选项

- **R18 风格**：`explicit`（大胆露骨，还原性张力/挑逗/娇喘）或 `moderate`（忠实但不夸张）
- **名词风格**：`auto` / `kawaii`（可爱化）/ `simple`（简洁化）/ `transliterate`（标准音译）
- **上下文行**：翻译时注入的邻行窗口（默认 3，0=关闭）。mtool 按行截断文本，
  开启后碎片句能结合前后文正确衔接（如"なんでこんな所に / 閉じ込められてるんだ？"
  会译成"为什么会被关在 / 这种地方啊？"而非各行脑补整句）
- **精修润色**：勾选后对超过阈值的长句（剧情/说明）及诗歌/谜语/咒文类文本做第二轮润色，
  使译文更优美有文采（阈值默认 80 字符）
- **自定义提示词**：多行文本框，内容会附加到每条翻译指令末尾（如"风格更文艺"、"××词译作××"等）

## 命令行参数

```
python transit_cli.py {analyze|translate|run|export} <input.json> [选项]

  --config <path>    配置文件（默认 config.json）
  --model <name>     覆盖模型
  --out <dir>        输出目录（默认 output/）
  --batch <n>        批大小（默认 12）
  --workers <n>      并发数（默认 6）
  --glossary <path>  指定术语库文件
  --limit <n>        只处理前 N 条待译文本（测试用）
  --polish           翻译后执行精修润色轮（美化长句/诗歌/谜语）
  --prompt <text>    追加自定义提示词（附加到每条翻译指令）
  --force-analyze    run 时强制重新分析（否则复用已有术语库）
```

## 输出文件（output/ 目录）

| 文件 | 说明 |
|------|------|
| `*_<hash>.glossary.json` | 术语库（世界观 + 分类词条 + 翻译注意），可手工编辑 |
| `*_<hash>.progress.json` | 翻译进度（每批保存，断点续传依据） |
| `*_<hash>.translate.log` | 实时进度日志（后台运行时 tail -f 查看） |
| `*_<hash>.translated.json` | 最终导出文件（与输入同格式） |

> **hash 隔离**：mtool 导出的文件默认都叫 `ManualTransFile.json`，本工具按**文件内容 hash**
> 区分不同游戏/文本，每个文件独立一套 术语库/进度/输出，互不串用。
> 文件名中的 `<hash>` 是该翻译文件内容的 SHA-1 前 10 位，GUI 加载文件时会显示。

## 术语库格式

```json
{
  "worldview": "本作是以日式麻将为核心、融合奇幻与 R18 元素的游戏……",
  "game_context": { "genre": "...", "setting": "...", "story": "...", "style_notes": "..." },
  "terms": {
    "角色名": { "ヴァラクゴール": "瓦拉克戈尔", "サキュバス": "魅魔" },
    "专业术语": { "リーチ": "立直", "ツモ": "自摸", "ドラ": "宝牌" },  # 示例为日麻游戏；其他游戏会是其题材对应的术语
    "势力/组织": { "HOP教団": "HOP教团" }
  },
  "translation_notes": "成人场景保留原文情色氛围……"
}
```

手工修正词条后，再次运行 translate 即生效（GUI 里点「在翻译前更新」）。

## 注意事项

- **数据目录**：配置与输出固定在「数据目录」——源码运行时是项目根目录，
  打包后是 exe 所在目录；可用环境变量 `TRANSIT_DATA_DIR` 覆盖。
  `output_dir` 配成相对路径时锚定该目录，不受进程 CWD 影响
- **本地接口防护**：WebUI 只监听 `127.0.0.1`，`/api/*` 会校验 `Host` 与 `Origin` 必须是本机，
  因此**必须用浏览器打开程序给出的地址**（`http://127.0.0.1:8765/`），
  用 curl 等工具伪造 `Host` 会被 403。
  默认不需要令牌；如需额外一层加固，启动前设 `TRANSIT_API_TOKEN=<自定义值>`，
  此时必须用启动时自动打开的链接（携带 `#token=...`）访问
- **重复启动是安全的**：如果已有本版本实例在运行，再次双击只会打开浏览器指向它，
  不会起第二个服务。若检测到端口上跑着**旧版本**（或别的程序），会改用其他端口并提示你关闭它
- **API Key 安全**：config.json 含明文 key，**请勿提交到公共仓库**（已在 `.gitignore` 中）；
  可用环境变量 `TRANSIT_API_KEY` / `TRANSIT_BASE_URL` / `TRANSIT_MODEL` 覆盖，避免落盘
- **thinking 参数**：SiliconFlow/DeepSeek 系模型默认开启思考（reasoning），
  会大幅拖慢翻译速度。工具默认发送 `thinking: {type: disabled}`；
  如需开启思考，在 config.json 中设 `"thinking": true`
- **模型选择**：`deepseek-ai/DeepSeek-V4-Flash` 性价比高（速度快、质量稳定）；
  `nex-agi/Nex-N2-Pro` 质量略优但无法关闭思考，速度较慢
- **并发与 token**：实测并发 6 最优（SiliconFlow 上 12 并发反而更慢，是服务端
  排队/限流所致），默认 6 即可。token 消耗与并发无关：约 133 tokens/条
  （120 条 ≈ 16k tokens），全量 3600 条 ≈ 50 万 tokens，DeepSeek-V4-Flash 成本约 ¥1 以内
- **占位符**：若译文与原文占位符不一致，条目仍会写入但会在日志记录 warning
- **缺漏条目**：导出时未翻译成功的条目保留原文（值=键），便于人工发现后补译；
  断点续传重跑会自动补齐漏译（多轮收敛机制）
- **漏译自动补**：模型偶尔漏译或微改键（全角/半角/空白），翻译器内置容错匹配
  （归一化 + 顺序对齐）和多轮收敛重试（最多 3 轮），确保不丢条目

## 目录结构

```
TransIt/
├── config.json            # 运行时配置（含 API Key，不入库）
├── config.example.json    # 配置模板（不含密钥，随发布包分发）
├── transit_cli.py         # 命令行入口（打包为 TransIt-CLI.exe）
├── transit_gui.py         # tkinter 界面（开发期备选，不随发布包分发）
├── webui.py               # WebUI 入口（打包为 TransIt.exe）
├── transit/
│   ├── paths.py           # 资源/数据目录解析（源码 vs 冻结双模式）
│   ├── config.py          # 配置加载/保存
│   ├── llm.py             # OpenAI 兼容客户端（重试/JSON mode/thinking 控制）
│   ├── reader.py          # 读取 + 分类
│   ├── analyzer.py        # 世界观分析 + 术语建库
│   ├── translator.py      # 批量精翻（并发/续传/占位符）
│   └── writer.py          # 导出
├── web/index.html         # WebUI 前端（单文件，无外链）
├── assets/transit.ico     # 应用图标（由 tools/make_icon.py 生成）
├── packaging/使用说明.txt  # 面向最终用户的说明（随发布包分发）
├── tools/                 # 构建辅助脚本（图标/版本资源/配置模板/样例校验）
├── TransIt.spec           # PyInstaller 构建配置
├── build.ps1              # 一键构建 + 冒烟测试 + 打包
├── LICENSE                # MIT
└── 示例翻译文件/           # 虚构样例（仅开发用，不入发布包）
```

## 示例文件

`示例翻译文件/ManualTransFile.json` 是一份**完全虚构**的样例（伪游戏《星见之塔》），
不包含任何真实游戏的文本。它刻意覆盖了 `transit/reader.py` 的全部 7 个分类分支：

| 分支 | 样例 | 分类结果 |
|------|------|----------|
| `empty` | `"": ""` | 原样保留 |
| `id_or_code` | `"EV001"`、`"10"`、`"SAVE DATA"` | 原样保留 |
| `code_assignment` | `"_switch = 1"` | 原样保留 |
| `latin_no_cjk` | `"It's a beautiful day."` | 原样保留 |
| `untranslated_jp` | `"星が、落ちる。"` | **待翻译** |
| `has_translation` | `"こんにちは": "你好"` | 沿用旧译 |
| `translated_but_value_is_id` | `"これは翻訳が必要な例です。": "10"` | **待翻译** |

还包含 `<SG1>`、`\v[1]`、`\n` 三类占位符，以及被 mtool 按行截断的碎片句
（用于验证「行上下文组合翻译」）。

改了样例后请跑一次校验，确保覆盖没退化：

```bash
python tools/verify_sample.py
```

## 打包与发布

```powershell
python -m pip install pyinstaller   # 仅构建期需要，运行期零依赖
pwsh -File build.ps1                # 构建 + 冒烟测试 + 压缩 ZIP
```

产出：

| 路径 | 说明 |
|------|------|
| `dist/TransIt/` | 便携版目录，可直接运行 |
| `release/TransIt-<版本>-win64.zip` | 发布包 |
| `release/SHA256SUMS.txt` | 校验和，随发布包一起提供 |

**为什么是 onedir 而不是 onefile**：onefile 每次启动都要把约 15MB 解压到
`%TEMP%\_MEIxxxx`，冷启动 2–5 秒、杀软误报率高，而且解压目录**退出即删**——
本工具把 `config.json`/`output/`/`uploads/` 写在 exe 同目录（便携版语义），
onefile 会导致用户数据丢失。onedir 秒开，数据目录稳定可见。

**两个 exe 共用一个 `_internal`**（体积几乎不增加）：

| 文件 | 子系统 | 用途 |
|------|--------|------|
| `TransIt.exe` | 窗口（无黑框） | 双击即用的 WebUI |
| `TransIt-CLI.exe` | 控制台 | 脚本/批处理，输出可被管道捕获 |

**发布版不含 tkinter**（以 WebUI 为唯一图形界面），顺带省掉 tcl/tk 约 5MB。
注意 `TransIt.spec` 的 `EXCLUDES` 里**不能**加 `email`/`http`/`xml`——
`urllib.request` 与 `http.client` 依赖 `email.parser`，排掉会直接崩。

**`build.ps1` 内置的防线**（任一不通过即中止构建）：

1. `config.example.json` 与 `transit/config.py` 的 `DEFAULT_CONFIG` 必须一致且不含密钥
2. 发布物中不得出现 `config.json` / `output` / `uploads` / `_t_edge_profile` / `示例翻译文件`
3. 冻结后**数据目录必须落在 exe 同目录**（验证 `transit/paths.py` 的冻结分支）
4. WebUI 冒烟测试：静态页与完整 UI 字节数、默认无令牌放行、跨站 `Origin` 403、
   伪造 `Host` 403、防点击劫持响应头；再以 `TRANSIT_API_TOKEN` 重启验证可选令牌路径

**版本号单一来源**：改 `transit/__init__.py` 的 `__version__`，
Windows 文件属性由 `tools/make_version_info.py` 自动生成。

> 未签名的 exe 首次运行会触发 SmartScreen「Windows 已保护你的电脑」。
> 让用户点「更多信息 → 仍要运行」即可；代码签名证书（每年数百元）能消除该提示，
> 但个人项目通常不值得，**不要**用自签名证书（无效且更可疑）。

## 免责声明

本工具只是一个**文本翻译辅助程序**，本身不包含、不附带、不分发任何游戏内容或素材。

- 请仅对你**合法拥有**的游戏文件使用本工具，并遵守该游戏的用户协议与当地法律。
- 译文的用途与后果由使用者自行承担；请勿将译文用于商业发行或二次分发原游戏资源。
- 本工具调用第三方大模型 API，你的待译文本会发送给该服务商，请注意其隐私政策与内容条款。
- 仓库内的示例文件为**虚构内容**，与任何真实作品无关。

## 许可证

[MIT](LICENSE) © 2026 687jsassd

