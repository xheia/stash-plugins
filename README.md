# stash-plugins

自用的 Stash 插件集合。目前包含一个插件：

## Metadata Translator

把 Stash 里的**标题与简介**自动翻译成中文。支持 **EDGE、Google、百度、腾讯云、LibreTranslate** 五种引擎，可配置优先级与失败自动回退。

覆盖四个实体：

| 实体 | 标题类字段 | 简介类字段 |
| --- | --- | --- |
| 场景 Scene | `title` | `details` |
| 演员 Performer | `name` | `details` |
| 工作室 Studio | `name` | `details` |
| 标签 Tag | `name` | `description` |

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
│   ├── engines.py                  五个翻译引擎 + 优先级回退路由
│   ├── detect.py                   中 / 日 / 韩文语言判定
│   ├── cache.py                    SQLite 翻译缓存
│   ├── config.py                   插件设置读取与类型规范化
│   ├── fields.py                   实体字段映射、查询与写回构造
│   ├── stash_api.py                Stash GraphQL 客户端
│   ├── log.py                      Stash 日志 / 进度协议
│   └── ui/translateMetadata.js     注入前端的翻译按钮
├── plugins/main/index.yml          插件源索引（给 Stash「添加源」用）
├── tests/                          校验脚本
└── build.py                        打包 + 刷新索引
```

---

## 环境要求

| 项目 | 要求 |
| --- | --- |
| Stash | v0.31.x（在 v0.31.1 上开发与验证） |
| Python | 3.8+，**只需要标准库**，不需要 `pip install` 任何东西 |
| Stash 镜像 | 需要有 python3。官方的 `stashapp/stash` **不含 Python**；`nerethos/stash`、`feederbox826/stash-s6` 等社区镜像自带 |

> **为什么插件是 Python 而不是 JS？**
> Stash v0.31 的 JS 插件运行时（内嵌 goja 引擎）只暴露 `input`、`log`、`util`、`gql` 四样东西，**没有 HTTP 能力**，无法直接请求翻译接口。所以走 Stash 官方支持的 `interface: raw` 外部进程方式。
>
> 另外 Stash v0.31.1 **没有「扫描完成」钩子**（源码里明确标注 scan 相关钩子尚未接入），所以自动翻译挂在实体写入之后——刮削器把数据写库的那一刻即触发，效果等价。

### 确认 Python 可用

```bash
# 进容器看一下
docker exec -it stash python3 --version
```

如果用的是 `nerethos/stash`，镜像里还带了一个装了依赖的 venv，可以用它：

```
/pip-install/venv/bin/python3
```

若 Stash 找不到 Python，在 **设置 → 系统 → 应用程序路径 → Python 可执行文件路径** 里填上面这个绝对路径。

---

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

| 引擎 | 需要凭证 | 免费额度 | 国内直连 | 中文质量 |
| --- | --- | --- | --- | --- |
| `edge` | 不需要 | 无明确限制 | **不通**（需代理） | 好 |
| `google` | 不需要 | 无明确限制 | **不通**（需代理） | 好 |
| `baidu` | AppID + 密钥 | 标准版 5 万字符/月 | 通 | 好 |
| `tencent` | SecretId + SecretKey | 每月 500 万字符（新用户试用额度，以官网为准） | 通 | 好 |
| `libretranslate` | 视实例而定 | 取决于自托管 | 通 | 一般 |

> 上表的"国内直连"是实测结论；EDGE 与 Google 的翻译端点在国内网络下无法直连。如果你有代理，填进 `http_proxy` 设置即可（例如 `http://192.168.3.2:7890`）。
>
> **公共 LibreTranslate 实例基本都已要求 API Key**，建议自托管：
> ```bash
> docker run -d --name libretranslate -p 5000:5000 libretranslate/libretranslate:latest
> ```
> 然后把 `libretranslate_url` 填成 `http://<宿主机IP>:5000`（注意 Stash 在容器里，写 `localhost` 指的是容器自己）。

### 推荐配置

**国内直连、开箱即用** → 主引擎 `baidu`，回退 `tencent`

**有代理** → 主引擎 `edge`，回退 `baidu`（EDGE 免费且质量好，百度兜底）

### 百度翻译

1. 到 https://fanyi-api.baidu.com/ 注册并开通「通用文本翻译」
2. 在控制台拿到 **AppID** 和 **密钥**
3. 填进 `baidu_appid` / `baidu_key`

### 腾讯云 TMT

1. 到 https://console.cloud.tencent.com/tmt 开通机器翻译
2. 在 https://console.cloud.tencent.com/cam/capi 新建密钥，拿到 **SecretId** / **SecretKey**
3. 填进 `tencent_secret_id` / `tencent_secret_key`；地域默认 `ap-guangzhou` 一般不用改

---

## 设置项

| 设置 | 默认 | 说明 |
| --- | --- | --- |
| `engine` | `edge` | 主引擎，见上表 |
| `engine_fallback` | `google` | 回退链，逗号分隔，前一个失败自动试下一个 |
| `target_lang` | `zh-CN` | 目标语言，也支持 `zh-TW` / `en` / `ja` / `ko` / `ru` |
| `source_lang` | `auto` | 源语言，`auto` 自动检测 |
| `auto_scene` | 开 | 场景自动翻译 |
| `auto_performer` | 开 | 演员自动翻译 |
| `auto_studio` | 开 | 工作室自动翻译 |
| `auto_tag` | 开 | 标签自动翻译 |
| `translate_title` | 开 | 翻译标题 / 名称类字段 |
| `translate_details` | 开 | 翻译简介类字段 |
| `translate_names` | **关** | 是否翻译**演员姓名**（专有名词，默认不动） |
| `tag_name_mode` | `alias` | `alias` 保留标签原名并追加中文别名；`rename` 直接改名 |
| `skip_existing` | 开 | 已是中文的文本跳过，这是自动翻译不死循环的关键 |
| `ambiguous_cjk` | `skip` | 纯汉字文本（可能是日文）如何处理，见「已知限制」 |
| `keep_original` | 开 | 把原文存进 `custom_fields`，可一键回滚 |
| `rate_limit_ms` | `300` | 两次请求的最小间隔，防止触发限流 |
| `timeout_s` | `20` | 单个请求超时 |
| `batch_size` | `100` | 批量任务每页条数 |
| `max_items` | `0` | 单次任务处理上限，0 = 不限 |
| `cache_enabled` | 开 | 相同文本只请求一次接口 |
| `http_proxy` | 空 | 走 EDGE / Google 时可能需要 |

> **`tag_name_mode` 请重点看一下。** 标签是**全局共享**的，`rename` 会把标签直接改名，影响所有引用它的场景；`alias`（默认）只追加一个中文别名，原名不变、可逆，搜索时中英文都能命中。想要"全站标签都是中文"再改成 `rename`。

---

## 使用

### 自动翻译

安装后即生效。刮削场景（或手动改元数据）时，写入完成后自动翻译对应的标题与简介。

日志在 **设置 → 日志**（日志级别调到 Debug 更详细），关键字 `[Plugin / Metadata Translator]`。

### 手动任务

**设置 → 任务 → 插件任务**：

| 任务 | 用途 |
| --- | --- |
| 翻译 - 全部 | 全库扫一遍四个实体 |
| 翻译 - 仅场景 / 演员 / 工作室 / 标签 | 只处理某一类 |
| 翻译 - 干跑预览（不写回） | 先看译文效果，不碰数据库。**首次建议先跑这个** |
| 测试翻译引擎 | 逐个测连通性与耗时，排查凭证问题 |
| 回滚翻译（恢复原文） | 从 `custom_fields` 取回原文写回，撤销翻译 |
| 清空翻译缓存 | 删掉本地缓存，下次重新请求 |

### 页面按钮

顶部导航栏会出现一个「译」图标：打开某个场景 / 演员 / 工作室 / 标签的详情页，点它即可翻译当前这个实体，结果显示在右下角提示里，刷新页面即可看到效果。

旁边的列表图标直接跳到任务页。

---

## 重复翻译是如何被避免的

同时开了自动钩子和批量任务，最怕的是"翻译 -> 触发钩子 -> 再翻译"死循环。这里有三重保护：

1. **语言判定**——译文是中文，第二次进来就判定"已是中文"直接跳过
2. **原文标记**——开了 `keep_original` 后，写回时会把原文记在 `custom_fields` 的 `tr_src_<字段>`。只要当前值和记录的原文不一致，就说明这是本插件翻译过的结果（或用户手工改过），不再重复处理。这能兜住"译文里夹带英文导致语言判定失效"的情况
3. **Stash 自带的循环保护**——同一个插件 + 同一个钩子最多连锁 10 次

缓存放在插件目录下的 `translate-cache.sqlite3`，键包含引擎链、源语言、目标语言和文本。

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

**日志里报 `no such file or directory: python3`**
Stash 找不到 Python。设置 → 系统 → 应用程序路径 → Python 可执行文件路径，填绝对路径。

**钩子没反应**
1. 确认插件是启用状态（设置 → 插件）
2. 确认对应实体的 `auto_*` 开关是开的
3. 把日志级别调到 Debug 看是否有 `[Plugin / Metadata Translator]` 输出
4. 注意只有场景 / 演员 / 工作室 / 标签四类实体会触发；图片、合集、系列不在范围内

**翻译失败 / 全部引擎均失败**
跑「测试翻译引擎」任务，日志里会逐个列出各引擎的成功或失败原因。常见原因：凭证没填、余额/额度用尽、国内直连不到 EDGE / Google（填 `http_proxy`）、LibreTranslate 实例要求 API Key。

**批量任务跑到一半提示失败**
多半是免费额度用尽或被限流。把 `rate_limit_ms` 调大（比如 1000），用 `max_items` 限制单次处理量，分几次跑完。

---

## 开发

```bash
# 1) 清单与结构校验（不需要网络）
python3 tests/test_manifest.py

# 2) 端到端流程测试：内置一个假 Stash 服务 + 桩引擎，不需要外网
python3 tests/test_pipeline.py

# 3) 打包
python3 build.py
```

测试覆盖：清单严格字段校验、语言判定、干跑、写回、`custom_fields` 原文留存、幂等、钩子分发、标签别名模式、缓存命中、回滚、引擎全挂时的降级。

发布：打 tag 推上去即可，GitHub Actions 会跑测试、构建 zip、发 Release 并刷新索引。

```bash
git tag v1.0.1 && git push origin v1.0.1
```

## License

MIT
