# stash-plugins

自用的 Stash 插件集合。AI开发，目前包含一个插件：

## Metadata Translator

把 Stash 里的**标题与简介**自动翻译成中文。支持 **AI 翻译（OpenAI 兼容，可接 DeepSeek / Ollama / 智谱等）、Google、DeepL、腾讯云、阿里云、百度、Lingva、MyMemory、LibreTranslate、EDGE 免认证接口** 十种引擎，可配置优先级与失败自动回退。

覆盖四个实体：

| 实体           | 标题类字段   | 简介类字段         |
| ------------ | ------- | ------------- |
| 场景 Scene     | `title` | `details`     |
| 演员 Performer | `name`  | `details`     |
| 工作室 Studio   | `name`  | `details`     |
| 标签 Tag       | `name`  | `description` |

三种触发方式：

1. **自动**——刮削器把元数据写进数据库时（`Scene.Create.Post` / `Scene.Update.Post` 等 8 个钩子）自动翻译
2. **手动任务**——设置 → 任务 → 插件任务，9 个任务（批量翻译、干跑预览、测试引擎、回滚、清缓存等）
3. **页面按钮**——顶部导航栏的「译」按钮，一键翻译当前打开页面的实体

---

## 目录结构

```
.
├── src/translateMetadata/          插件源码（就是会被安装的内容）
│   ├── translateMetadata.yml       插件清单：钩子 / 任务 / 设置项声明
│   ├── translateMetadata.py        入口：stdin 分发钩子与任务
│   ├── engines.py                  十个翻译引擎 + 优先级回退路由（含熔断与失败诊断）
│   ├── detect.py                   中 / 日 / 韩文语言判定 + 退化译文识别
│   ├── cache.py                    SQLite 翻译缓存
│   ├── config.py                   插件设置读取与类型规范化
│   ├── fields.py                   实体字段映射、查询与写回构造
│   ├── stash_api.py                Stash GraphQL 客户端
│   ├── log.py                      Stash 日志 / 进度协议
│   └── ui/translateMetadata.js     注入前端的翻译按钮
├── plugins/main/index.yml          插件源索引（给 Stash「添加源」用）
├── tests/
│   ├── _schema.py                  Stash schema 契约（名字白名单 + 请求校验器）
│   ├── test_engines.py             引擎单元测试（含阿里云签名的官方向量校验，不联网）
│   ├── test_manifest.py            清单 / 索引与 Stash v0.31.1 解析规则的静态校验
│   ├── test_schema_contract.py     插件用的 GraphQL 名字必须真实存在于 schema
│   └── test_pipeline.py            端到端流程测试（假 Stash 服务 + 桩引擎，不联网）
└── build.py                        打包 + 刷新索引
```

跑测试（需要 PyYAML，只用标准库跑插件本体则不需要）：

```bash
python3 tests/test_engines.py
python3 tests/test_manifest.py
python3 tests/test_schema_contract.py
python3 tests/test_pipeline.py
```

---

## 部署环境

### 环境要求

| 项目     | 要求                                                                   |
| ------ | -------------------------------------------------------------------- |
| Stash  | v0.31.x（在 v0.31.1 上开发与验证）                                            |
| Python | 3.8+，**只需要标准库**，不需要 `pip install` 任何东西                               |
| 网络     | 看所选引擎：国产云（腾讯 / 阿里 / 百度）与 lingva / mymemory 国内直连即可；google / deepl 需代理 |

### 各部署环境对照

| 环境                         | 自带 Python                   | 本插件         | 说明                                                                                                                                           |
| -------------------------- | --------------------------- | ----------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `stashapp/stash`（**官方镜像**） | ✅ 2025-11 起的构建已自带 `python3` | **开箱即用**    | Alpine 基础，解释器名就叫 `python3`，与本插件清单的 `exec` 写法一致；镜像预装 `stashapp-tools`、`cloudscraper`、`mechanicalsoup`、`py3-requests` 等社区插件常用包（本插件零依赖，与它们互不影响） |
| `nerethos/stash`           | ✅ 且带 venv                   | 开箱即用        | venv 解释器：`/pip-install/venv/bin/python3`                                                                                                     |
| `feederbox826/stash-s6`    | ✅ uv 管理                     | 开箱即用        | 启动时自动扫描插件目录的 `requirements.txt`；本插件没有这个文件，不会触发任何安装                                                                                           |
| Windows 原生                 | 自行安装                        | 填 Python 路径 | 装好 Python 3 后在 Stash 设置里填绝对路径                                                                                                                |
| QNAP / 群晖等 NAS 自建 venv     | 自行创建                        | 填 venv 绝对路径 | 见下文「确认 Python 可用」                                                                                                                            |

### 确认 Python 可用

```bash
# 官方镜像 / nerethos / stash-s6 通用：进容器看版本
docker exec -it stash python3 --version

# nerethos/stash 的 venv
docker exec -it stash /pip-install/venv/bin/python3 --version

# QNAP 自建 venv 示例（路径按自己的来）
/share/CACHEDEV1_DATA/Public/stash/py/venv/bin/python3 --version
```

版本 ≥ 3.8 即可。能在容器里跑通，插件就能跑通——本插件不 import 任何第三方包。

### Python 路径配置

Stash 找不到 Python 时（日志报 `no such file or directory: python3`），到  
**设置 → 系统 → 应用程序路径 → Python 可执行文件路径** 填绝对路径，例如：

| 环境             | 填什么                                                      |
| -------------- | -------------------------------------------------------- |
| 官方镜像           | `python3`（一般在 PATH 里，通常不用填）                              |
| nerethos/stash | `/pip-install/venv/bin/python3`                          |
| QNAP 自建 venv   | `/share/CACHEDEV1_DATA/Public/stash/py/venv/bin/python3` |
| Windows        | `C:\Python312\python.exe`（按实际安装路径）                       |

改完 **重载插件** 生效。

## 安装

### 方式一：添加插件源（推荐，可在线更新）

1. Stash → **设置 → 插件 → 可用插件 → 添加源**
2. 填入索引地址：
   ```
   https://raw.githubusercontent.com/xheia/stash-plugins/main/plugins/main/index.yml
   ```
   > 如果 raw 地址访问不畅（国内常见），换成 jsDelivr 镜像：  
   > `https://cdn.jsdelivr.net/gh/xheia/stash-plugins@main/plugins/main/index.yml`
3. 在列表里找到 **Metadata Translator** 安装
4. 点 **重载插件**，确认插件出现在列表中

### 方式二：手动安装

1. 从 [Releases](https://github.com/xheia/stash-plugins/releases) 下载最新的 `translateMetadata-vX.Y.Z.zip`
2. 解压后把 `translateMetadata/` 整个文件夹放进 Stash 的 plugins 目录
   ```
   Docker（nerethos / 官方）：宿主机上映射到 /root/.stash 的那个目录 → plugins/translateMetadata/
   Windows：%USERPROFILE%\.stash\plugins\translateMetadata\
   ```
3. **设置 → 插件 → 重载插件**

### 本地打包

```bash
python3 build.py            # 生成 dist/*.zip 并刷新 plugins/main/index.yml
python3 build.py --check    # 只预览会打包哪些文件
```

打包用的是固定时间戳，内容不变则 sha256 不变，便于校验。

---

## 引擎配置

在 **设置 → 插件 → Metadata Translator** 里填。所有设置改完立即生效，不用重载插件。

### 引擎可用性与取舍

| 引擎               | 需要凭证                          | 免费额度                         | 国内直连                     | 中文质量                |
| ---------------- | ----------------------------- | ---------------------------- | ------------------------ | ------------------- |
| `openai`         | 接口地址（本地可不填 Key）               | 取决于所用服务                      | DeepSeek / 智谱 / Ollama 通 | **最好**（LLM，能理解上下文）  |
| `tencent`        | SecretId + SecretKey          | 每月 500 万字符（新用户试用额度，以官网为准）    | 通                        | 好                   |
| `alibaba`        | AccessKeyId + AccessKeySecret | 通用版每月 100 万字符（新用户试用额度，以官网为准） | 通                        | 好                   |
| `baidu`          | AppID + 密钥                    | 标准版 5 万字符/月                  | 通                        | 好                   |
| `deepl`          | API Key（免费档以 `:fx` 结尾）        | 每月 50 万字符                    | **不通**（需代理）              | 好                   |
| `libretranslate` | 视实例而定                         | 取决于自托管                       | 通                        | **一般**（可能吐出退化内容，见下） |
| `google`         | 不需要                           | 无明确限制                        | **不通**（需代理）              | 好                   |
| `lingva`         | 不需要                           | —                              | ❌ **公共实例基本全死**（见下）        | 好（Google 同源）        |
| `mymemory`       | 不需要（可填邮箱提额）                   | 匿名约 1000 词/天；填邮箱约 5 万词/天     | 通                        | 一般；单次上限 500 字节      |
| `edge`           | 不需要                           | —                            | **常被重置**                 | 好（微软同源）             |

> ### EDGE 免认证端点（2026-09 复核）
>
> 早先版本用的「先取 JWT 再翻译」两步接口已被微软下线（`/translate/auth` 返回 404）。  
> 现改用社区实测仍存活的**免认证单步端点**：
>
> ```
> POST https://edge.microsoft.com/translate/translatetext?from=en&to=zh-CHS&api-version=3.0
> Origin: https://www.microsoft.com
> Referer:  https://www.microsoft.com/
> ```
>
> 无需任何 token。但在大陆网络下该域名**直连常被 RST**，能否使用完全取决于部署机的  
> 代理路径（Clash 类工具若把微软域名放直连规则，同样会失败）。装好后跑一次  
> 「测试翻译引擎」即可确认。不通就换 `google` / `openai`。

> **国内直连环境下真正免注册可用的是 `mymemory`**，配 `openai`（接 DeepSeek / 智谱等国产 API）质量最好。  
> Google 的免费端点 `translate.googleapis.com` 在国内不通，且对出口 IP 限流很凶（HTTP 429）。  
> 代理无需在插件里配置——不手动设置时自动使用**系统代理**（环境变量 `HTTP_PROXY`/`HTTPS_PROXY`，Docker 容器里即容器环境变量）。
>
> **公共 LibreTranslate 实例基本都已要求 API Key**，建议自托管：
>
> ```bash
> docker run -d --name libretranslate -p 5000:5000 libretranslate/libretranslate:latest
> ```
>
> 然后把 `libretranslate_url` 填成 `http://<宿主机IP>:5000`（注意 Stash 在容器里，写 `localhost` 指的是容器自己）。

### 推荐配置

**有 AI API（质量首选）** → 主引擎 `openai`，回退云厂商

```
engine:          openai
engine_fallback: tencent,mymemory
openai_base_url: https://api.deepseek.com        # 或 Ollama http://localhost:11434
openai_api_key:  sk-xxx                          # Ollama / LM Studio 留空
openai_model:    deepseek-chat                   # 或 glm-4-flash / qwen2.5:7b 等
timeout_s:       120                             # LLM 比机翻慢，超时给够
```

LLM 翻译对这类内容优势明显：能理解上下文、保留 `#456` 这类编号、人名处理更自然。

**国内直连（无 AI）** → 主引擎 `tencent`，回退 `alibaba,mymemory`

```
engine:          tencent
engine_fallback: alibaba,mymemory
```

**有代理** → 主引擎 `google`，回退 `tencent,alibaba`

```
engine:          google
engine_fallback: tencent,alibaba
```
> 代理走系统配置：给容器加环境变量 `HTTPS_PROXY=http://192.168.3.2:7890` 即可，无需在插件里填。

**完全零成本** → 主引擎 `mymemory`，回退 `google`（需代理）

```
engine:          mymemory
engine_fallback: google
```

> ⚠️ **Lingva 公共实例基本全死（2026-09 实测）**：`lingva.ml` 被 Cloudflare
> 人机验证拦截（403 Just a moment），`lunar.icu` / `esmailelbob.xyz` 已下线，
> `plausibility.cloud` 返回 500。只有自托管 Lingva 才建议启用该引擎；
> 放在回退链里也无妨——连续失败 3 次会被熔断跳过，不会拖慢任务。

**只有自托管 LibreTranslate（零成本）** → 主引擎 `libretranslate`，回退留空

```
engine:          libretranslate
libretranslate_url: http://192.168.x.x:5000
```

### 百度翻译

1. 到 <https://fanyi-api.baidu.com/> 注册并开通「通用文本翻译」
2. 在控制台拿到 **AppID** 和 **密钥**
3. 填进 `baidu_appid` / `baidu_key`

### 腾讯云 TMT

1. 到 <https://console.cloud.tencent.com/tmt> 开通机器翻译
2. 在 <https://console.cloud.tencent.com/cam/capi> 新建密钥，拿到 **SecretId** / **SecretKey**
3. 填进 `tencent_secret_id` / `tencent_secret_key`

`tencent_region` 三种写法都认，默认 `ap-guangzhou`：

| 你填的                                   | 实际使用                                                              |
| ------------------------------------- | ----------------------------------------------------------------- |
| `ap-guangzhou`                        | host `tmt.ap-guangzhou.tencentcloudapi.com`，region `ap-guangzhou` |
| `https://tmt.tencentcloudapi.com`     | host `tmt.tencentcloudapi.com`，region 回落 `ap-guangzhou`           |
| `tmt.ap-shanghai.tencentcloudapi.com` | host 同上，region `ap-shanghai`                                      |

### 阿里云机器翻译

1. 到 <https://mt.console.aliyun.com/> 开通「机器翻译」
2. 在 <https://ram.console.aliyun.com/manage/ak> 创建 **AccessKeyId** / **AccessKeySecret**  
   （建议用 RAM 子账号，只授予 `AliyunMTFullAccess`；主账号 AK 权限过大，拿到后务必妥善保管）
3. 填进 `alibaba_access_key` / `alibaba_access_secret`
4. `alibaba_region` 留空即用中心接入点 `mt.aliyuncs.com`；要指定地域就填 `cn-hangzhou`  
   或完整地址 `https://mt.cn-hangzhou.aliyuncs.com`，三种写法都认

---

## 设置项

| 设置                  | 默认       | 说明                                                               |
| ------------------- | -------- | ---------------------------------------------------------------- |
| `engine`            | `google` | 主引擎，见上表                                                          |
| `engine_fallback`   | 空        | 回退链，逗号分隔，前一个失败自动试下一个。留空表示不回退                                     |
| `target_lang`       | `zh-CN`  | 目标语言，也支持 `zh-TW` / `en` / `ja` / `ko` / `ru`                     |
| `source_lang`       | `auto`   | 源语言，`auto` 自动检测                                                  |
| `auto_scene`        | 开        | 场景自动翻译                                                           |
| `auto_performer`    | 开        | 演员自动翻译                                                           |
| `auto_studio`       | 开        | 工作室自动翻译                                                          |
| `auto_tag`          | 开        | 标签自动翻译                                                           |
| `translate_title`   | 开        | 翻译标题 / 名称类字段                                                     |
| `translate_details` | 开        | 翻译简介类字段                                                          |
| `translate_names`   | **关**    | 是否翻译**演员姓名**（专有名词，默认不动）                                          |
| `tag_name_mode`     | `alias`  | `alias` 保留标签原名并追加中文别名；`rename` 直接改名                              |
| `skip_existing`     | 开        | 已是中文的文本跳过，这是自动翻译不死循环的关键                                          |
| `ambiguous_cjk`     | `skip`   | 纯汉字文本（可能是日文）如何处理，见「已知限制」                                         |
| `keep_original`     | 开        | 把原文存进 `custom_fields`，可一键回滚                                      |
| `rate_limit_ms`     | `300`    | 两次请求的最小间隔，防止触发限流                                                 |
| `timeout_s`         | `20`     | 单个请求超时                                                           |
| `retry_times`       | `1`      | 瞬时错误（429 限流 / 5xx）重试次数，0 = 不重试                                   |
| `retry_backoff_ms`  | `800`    | 重试等待，按次数递增                                                       |
| `engine_skip_after` | `3`      | 某引擎连续失败这么多次后，本轮不再试它（熔断）。0 = 关闭                                   |
| `batch_size`        | `100`    | 批量任务每页条数                                                         |
| `max_items`         | `0`      | 单次任务**实际翻译**上限；已翻译/已是中文的记录跳过不占名额，0 = 不限         |
| `cache_enabled`     | 开        | 相同文本只请求一次接口                                                      |
| `http_proxy`        | 空        | **已从设置页收起**：留空即使用系统代理（环境变量 `HTTP_PROXY`/`HTTPS_PROXY`）；如需临时手动指定仍可经任务参数传入 |


凭证类（不填则对应引擎不可用，会自动从引擎链里剔除）：

| 设置                                              | 说明                                                       |
| ----------------------------------------------- | -------------------------------------------------------- |
| `baidu_appid` / `baidu_key`                     | 百度翻译的 AppID 与密钥                                          |
| `tencent_secret_id` / `tencent_secret_key`      | 腾讯云密钥；`tencent_region` 默认 `ap-guangzhou`                 |
| `alibaba_access_key` / `alibaba_access_secret`  | 阿里云 AccessKey；`alibaba_region` 默认 `mt.aliyuncs.com`      |
| `libretranslate_url` / `libretranslate_api_key` | LibreTranslate 地址与 Key；`url` 填根地址或带 `/translate` 的接口地址都行 |

> **`tag_name_mode` 请重点看一下。** 标签是**全局共享**的，`rename` 会把标签直接改名，影响所有引用它的场景；`alias`（默认）只追加一个中文别名，原名不变、可逆，搜索时中英文都能命中。想要"全站标签都是中文"再改成 `rename`。

---

## 使用

### 自动翻译

安装后即生效。刮削场景（或手动改元数据）时，写入完成后自动翻译对应的标题与简介。

日志在 **设置 → 日志**（日志级别调到 Debug 更详细），关键字 `[Plugin / Metadata Translator]`。

### 手动任务

**设置 → 任务 → 插件任务**：

| 任务                       | 用途                            |
| ------------------------ | ----------------------------- |
| 翻译 - 全部                  | 全库扫一遍四个实体                     |
| 翻译 - 仅场景 / 演员 / 工作室 / 标签 | 只处理某一类                        |
| 翻译 - 干跑预览（不写回）           | 先看译文效果，不碰数据库。**首次建议先跑这个**     |
| 测试翻译引擎                   | 逐个测连通性与耗时，排查凭证问题              |
| 回滚翻译（恢复原文）               | 从 `custom_fields` 取回原文写回，撤销翻译 |
| 清空翻译缓存                   | 删掉本地缓存，下次重新请求                 |

### 页面按钮

顶部导航栏会出现一个「译」图标：打开某个场景 / 演员 / 工作室 / 标签的详情页，点它即可翻译当前这个实体，结果显示在右下角提示里，**翻译完成后页面会自动刷新**，无需手动重载。

旁边的列表图标直接跳到任务页。

---

## 已知限制

- **纯汉字且无假名的日文**（例如「痴漢電車」）会被当成中文跳过。绝大多数日文标题都带假名，所以影响有限。要处理这类，把 `ambiguous_cjk` 改成 `detect`——它会调用引擎判断语言，代价是每段纯汉字文本多花一次请求（有缓存兜底）。反过来，这会让你库里大量真正的中文内容也各多花一次请求，请按需取舍。
- **演员姓名默认不翻译**。英文人名翻成中文未必是你想要的，需要时打开 `translate_names`。
- **标签改名不可逆**（`rename` 模式）。默认的 `alias` 模式是安全的。
- **回滚只处理被覆盖写的字段**。`alias` 模式追加的别名不会被回滚任务移除（它没有覆盖任何东西，无害）。
- **扫描/生成钩子不存在**。Stash v0.31.1 尚未接入 scan 相关钩子，所以"翻译"只能在实体写入后触发，不能挂在"扫描完成"上。

---

## 排错

**插件列表里看不到插件**  
清单 YAML 有任何未知字段都会导致整份加载失败且不报错。跑一下 `python3 tests/test_manifest.py` 定位问题。

**日志里报 `field xxx already set in type plugin.SettingConfig`**  
清单里同一个设置项写了重复的键（v1.2.4 的 `libretranslate_api_key` 就多写了一行 `type`），  
Stash 的严格解析会因此整份拒绝加载 —— 插件不会报错，只是在列表里消失。删掉多出来的那行即可。  
`python3 tests/test_manifest.py` 的「严格解析」一节会把重复键和行号直接报出来。

**日志里报 `no such file or directory: python3`**  
Stash 找不到 Python。设置 → 系统 → 应用程序路径 → Python 可执行文件路径，填绝对路径。

**钩子没反应**

1. 确认插件是启用状态（设置 → 插件）
2. 确认对应实体的 `auto_*` 开关是开的
3. 把日志级别调到 Debug 看是否有 `[Plugin / Metadata Translator]` 输出
4. 注意只有场景 / 演员 / 工作室 / 标签四类实体会触发；图片、合集、系列不在范围内

**翻译失败 / 全部引擎均失败**  
跑「测试翻译引擎」任务 —— 它会先把**实际生效的配置**打出来（设置从哪来的、引擎链、代理值、  
凭证是否读到），再逐个引擎试翻译并列出成功或失败原因。绝大多数问题在这一屏就能看出答案。

各云厂商的报错含义（都遇到过，直接照着查）：

| 报错                                                  | 含义                              | 怎么办                                                                                  |
| --------------------------------------------------- | ------------------------------- | ------------------------------------------------------------------------------------ |
| 腾讯云 `AuthFailure.SecretIdNotFound`                  | SecretId 不存在                    | 核对 `tencent_secret_id`，注意别把 SecretKey 填串了                                            |
| 腾讯云 `AuthFailure.SignatureFailure`                  | 签名不对                            | 检查系统时间是否偏差过大（签名带时间戳）                                                                 |
| 腾讯云 `InvalidParameterValue: X-TC-Region is invalid` | `tencent_region` 填的是接口地址        | 1.1.2 起已自动兼容；升级即可，或改填 `ap-guangzhou`                                                 |
| 阿里云 `InvalidAccessKeyId.NotFound`                   | AccessKeyId 不存在                 | 核对 `alibaba_access_key`                                                              |
| 阿里云 `InvalidAccessKeyId.Inactive`                   | **AccessKey 已被禁用**              | 到 RAM 控制台把该 AK 重新启用，或换一个                                                             |
| 阿里云 `SignatureDoesNotMatch`                         | 签名不对                            | 核对 `alibaba_access_secret`；这个报错说明 AK 本身是有效的                                          |
| 阿里云 `InvalidAccountStatus: 账号没有开通服务`                | 没开通机器翻译                         | 到 <https://mt.console.aliyun.com/> 开通；也可能是因为请求缺必填参数                                  |
| 阿里云 `FormatType is mandatory for this action`       | 请求缺 `FormatType`                | 1.1.2 起已补上；升级即可                                                                      |
| 阿里云 `NotSupported` / 语言不支持                          | 目标语言码不在该账号可用列表                  | 换 `target_lang`（如 `zh-CN` → `zh-TW`）                                                 |
| 百度 `52003: UNAUTHORIZED USER`                       | AppID 无效                        | 核对 `baidu_appid`，确认已开通「通用文本翻译」                                                       |
| Google `HTTP 429`                                   | 出口 IP 被限流                       | 换引擎，或把 `rate_limit_ms` 调大；免费端点对共享 IP 限制很凶                                            |
| EDGE 连接被重置 / `10054` / `Connection reset`           | 大陆网络下 `edge.microsoft.com` 常被重置 | 换 `google` / `openai`，或调整代理规则让该域名走代理                                      |
| DeepL `HTTP 403`                                    | Key 无效或档位填错                     | 免费 Key（`:fx` 结尾）必须走 `api-free.deepl.com`；地址留空即自动选择                                   |
| DeepL `HTTP 456`                                    | 本月免费额度耗尽                        | 下月恢复，或改用其它引擎                                                                         |
| MyMemory `MYMEMORY WARNING`                         | 当日免费额度用尽                        | 填 `mymemory_email` 提额到约 5 万词/天，或换引擎                                                  |
| AI 翻译 `Incorrect API key` / 401                     | Key 不对或服务不匹配                    | 核对 `openai_api_key`；注意模型名要与所用服务匹配（DeepSeek 没有 `gpt-*`）                               |
| AI 翻译 `ModuleNotFoundError` / 404                   | 接口地址不对                          | 确认 `openai_base_url` 是否为该服务的 OpenAI 兼容端点；Ollama 需 `Ollama serve` 且已 `ollama pull` 模型 |
| **识别（Identify）后没有自动翻译**                         | 钩子只在识别**实际改动了字段**时触发            | Stash 源码：识别结果与现有数据完全一致时（updater 为空）不触发 `Scene.Update.Post` 钩子。确认该场景的标题/简介确实被识别改写过；v1.2.3 起钩子触发与跳过原因都会打 Info 日志，跑一次识别看日志即可定位 |

> 注意报错顺序：阿里云会先校验 AccessKey 再校验签名，所以 AK 有问题时不会出现  
> `SignatureDoesNotMatch`。换句话说，看到 `InvalidAccessKeyId.*` 时无法据此判断签名是否正确。
>
> 同理，阿里云在请求缺少必填参数（如 `FormatType`）时可能回一个与参数无关的  
> `InvalidAccountStatus`，让人误以为是账号没开通。**遇到含义可疑的报错时，  
> 先确认请求参数齐全** —— 这是踩过一次坑的结论。

**批量任务跑到一半提示失败**  
多半是免费额度用尽或被限流。把 `rate_limit_ms` 调大（比如 1000），用 `max_items` 限制单次实际翻译量（跳过的已翻译记录不占名额，任务会自动向后扫描），分几次跑完。

---

### 一些踩过的坑（都固化成测试了）

| 坑                         | 症状                                                | 现在怎么防                                          |
| ------------------------- | ------------------------------------------------- | ---------------------------------------------- |
| `http_proxy` 少写 `//`      | **每个**引擎都报 `Name or service not known`，像是所有翻译接口挂了 | `normalize_proxy()` 自动补齐 + 单测覆盖 13 种写法         |
| `tencent_region` 填接口地址    | `X-TC-Region is invalid`                          | `normalize_tencent_endpoint()` 自动拆 host/region |
| 阿里云漏传 `FormatType`        | 报 `InvalidAccountStatus: 账号没有开通服务`（**与真实原因无关**）   | 参数齐全性单测                                        |
| LibreTranslate 输出 `相相相相…` | 垃圾译文被当成成功写进库                                      | `looks_degenerate()` 拦截并降级                     |
| 链首引擎死掉                    | 上百条数据每条都白等一次超时                                    | Router 熔断（连续失败 3 次即跳过）                         |
| GraphQL 类型名拼错             | `Unknown type "SUpdateInputcene"`，写回全线失效          | schema 契约测试 + 假服务入口校验                          |
| 设置项里重复写同一个键（v1.2.4）       | 插件在列表里凭空消失，日志只有 `field type already set in type plugin.SettingConfig` | 清单/索引改走「重复键即报错」的严格 loader（PyYAML 默认会静默覆盖，才让它在本地全绿） |



## License

MIT
