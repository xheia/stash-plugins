#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""插件清单校验（对齐 Stash v0.31.1 的解析规则）。

为什么需要这个：Stash 用 yaml.v2 的严格模式解析插件 yml，出现一个未知字段
就会整份加载失败；而失败的插件不会报错到界面上，只是"消失"，很难排查。
本脚本把源码里的约束固化下来：

  * 顶层 / 任务 / 钩子 / UI / 设置只允许 v0.31.1 认识的键
  * interface 只能是 raw / rpc / js
  * defaultArgs 的值必须是字符串（Go 侧是 map[string]string，写布尔会解析失败）
  * triggeredBy 必须是 hook/hooks.go 里 IsValid() 认可的触发器
  * 设置项 type 只能是 STRING / NUMBER / BOOLEAN
  * 插件 ID（yml 文件名）要跟目录名、索引里的 id 一致

用法：
    python3 tests/test_manifest.py
"""
from __future__ import annotations

import os
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PLUGIN_ID = "translateMetadata"
MANIFEST = os.path.join(ROOT, "src", PLUGIN_ID, PLUGIN_ID + ".yml")
INDEX = os.path.join(ROOT, "plugins", "main", "index.yml")

# ---- 来自 pkg/plugin/config.go 的字段白名单 ---------------------------------- #
TOP_KEYS = {"name", "description", "url", "version", "interface", "exec",
            "errLog", "tasks", "hooks", "ui", "settings"}
OPERATION_KEYS = {"name", "description", "execArgs", "defaultArgs"}
HOOK_EXTRA_KEYS = {"triggeredBy"}
UI_KEYS = {"requires", "csp", "javascript", "css", "assets"}
CSP_KEYS = {"script-src", "style-src", "connect-src"}
SETTING_KEYS = {"type", "displayName", "description"}
INTERFACES = {"raw", "rpc", "js"}
SETTING_TYPES = {"STRING", "NUMBER", "BOOLEAN"}

# ---- 来自 pkg/plugin/hook/hooks.go 的 IsValid() 白名单 ----------------------- #
VALID_TRIGGERS = set()
for _prefix in ("SceneMarker", "Scene", "Image", "Gallery", "GalleryChapter",
                "Movie", "Group", "Performer", "Studio", "Tag"):
    for _suffix in ("Create.Post", "Update.Post", "Destroy.Post"):
        VALID_TRIGGERS.add("%s.%s" % (_prefix, _suffix))

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print("  [PASS] %s" % name)
    else:
        FAILED.append(name)
        print("  [FAIL] %s  %s" % (name, detail))


def unknown_keys(mapping, allowed, where):
    return sorted(set(mapping or {}) - allowed)


def main():
    with open(MANIFEST, "r", encoding="utf-8") as handle:
        manifest = yaml.safe_load(handle)

    print("清单: %s" % os.path.relpath(MANIFEST, ROOT))

    print("\n1) 顶层字段")
    unknown = unknown_keys(manifest, TOP_KEYS, "顶层")
    check("无未知顶层字段", not unknown, "未知: %s" % unknown)
    check("插件 ID 与文件名一致", os.path.basename(MANIFEST) == PLUGIN_ID + ".yml")
    check("interface 合法",
          manifest.get("interface", "raw") in INTERFACES,
          str(manifest.get("interface")))
    check("有 exec 且第一项是 python 命令",
          isinstance(manifest.get("exec"), list) and manifest["exec"]
          and manifest["exec"][0] in ("python", "python3"),
          str(manifest.get("exec")))
    check("exec 引用了 {pluginDir}",
          any("{pluginDir}" in str(a) for a in manifest.get("exec") or []))
    check("name / description / version 齐备",
          all(manifest.get(k) for k in ("name", "description", "version")))

    print("\n2) 任务（tasks）")
    tasks = manifest.get("tasks") or []
    check("至少有一个任务", len(tasks) > 0, str(len(tasks)))
    task_names = []
    for task in tasks:
        where = "task %r" % task.get("name")
        check("%s 无未知字段" % where, not unknown_keys(task, OPERATION_KEYS, where),
              str(unknown_keys(task, OPERATION_KEYS, where)))
        check("%s 有 name" % where, bool(task.get("name")))
        args = task.get("defaultArgs") or {}
        bad = [k for k, v in args.items() if not isinstance(v, str)]
        check("%s 的 defaultArgs 全是字符串" % where, not bad, "非字符串: %s" % bad)
        task_names.append(task.get("name"))
    check("任务名不重复", len(task_names) == len(set(task_names)))

    modes = {t.get("defaultArgs", {}).get("mode") for t in tasks}
    required_modes = {"all", "scene", "performer", "studio", "tag",
                      "selftest", "rollback", "clearcache"}
    check("覆盖全部必需的任务模式", required_modes <= modes,
          "缺少: %s" % sorted(required_modes - modes))

    print("\n3) 钩子（hooks）")
    hooks = manifest.get("hooks") or []
    check("至少有一个钩子", len(hooks) > 0)
    for hook in hooks:
        where = "hook %r" % hook.get("name")
        allowed = OPERATION_KEYS | HOOK_EXTRA_KEYS
        check("%s 无未知字段" % where, not unknown_keys(hook, allowed, where),
              str(unknown_keys(hook, allowed, where)))
        triggers = hook.get("triggeredBy") or []
        check("%s 声明了 triggeredBy" % where, bool(triggers))
        invalid = [t for t in triggers if t not in VALID_TRIGGERS]
        check("%s 触发器全部有效" % where, not invalid, "非法: %s" % invalid)
        args = hook.get("defaultArgs") or {}
        bad = [k for k, v in args.items() if not isinstance(v, str)]
        check("%s 的 defaultArgs 全是字符串" % where, not bad, "非字符串: %s" % bad)

    all_triggers = {t for h in hooks for t in (h.get("triggeredBy") or [])}
    expected = {
        "Scene.Create.Post", "Scene.Update.Post",
        "Performer.Create.Post", "Performer.Update.Post",
        "Studio.Create.Post", "Studio.Update.Post",
        "Tag.Create.Post", "Tag.Update.Post",
    }
    check("四个实体的创建/更新钩子都已挂上", expected <= all_triggers,
          "缺少: %s" % sorted(expected - all_triggers))

    print("\n4) 前端脚本（ui）")
    ui = manifest.get("ui") or {}
    check("ui 无未知字段", not unknown_keys(ui, UI_KEYS, "ui"),
          str(unknown_keys(ui, UI_KEYS, "ui")))
    if ui.get("csp"):
        check("csp 无未知字段", not unknown_keys(ui["csp"], CSP_KEYS, "csp"),
              str(unknown_keys(ui["csp"], CSP_KEYS, "csp")))
    for rel in (ui.get("javascript") or []) + (ui.get("css") or []):
        if str(rel).startswith("http"):
            continue
        check("ui 引用的文件存在: %s" % rel,
              os.path.exists(os.path.join(ROOT, "src", PLUGIN_ID, rel)))

    print("\n5) 设置项（settings）")
    settings = manifest.get("settings") or {}
    check("有设置项", len(settings) > 0)
    for key, spec in settings.items():
        spec = spec or {}
        check("setting %s 无未知字段" % key, not unknown_keys(spec, SETTING_KEYS, key),
              str(unknown_keys(spec, SETTING_KEYS, key)))
        kind = spec.get("type", "STRING")
        check("setting %s 的 type 合法" % key, kind in SETTING_TYPES, str(kind))
        check("setting %s 有 displayName" % key, bool(spec.get("displayName")))

    print("\n6) 与 Python 侧的默认值对齐")
    sys.path.insert(0, os.path.join(ROOT, "src", PLUGIN_ID))
    from config import DEFAULTS  # noqa: E402
    missing = sorted(set(DEFAULTS) - set(settings))
    check("DEFAULTS 里的键都在清单里声明", not missing, "未声明: %s" % missing)
    extra = sorted(set(settings) - set(DEFAULTS))
    check("清单里没有 DEFAULTS 之外的键", not extra, "多余: %s" % extra)

    print("\n7) 插件索引（plugins/main/index.yml）")
    if os.path.exists(INDEX):
        with open(INDEX, "r", encoding="utf-8") as handle:
            index = yaml.safe_load(handle) or []
        check("索引是列表", isinstance(index, list))
        entry = next((e for e in index if e.get("id") == PLUGIN_ID), None)
        check("索引里有 %s" % PLUGIN_ID, entry is not None)
        if entry:
            for field in ("id", "name", "version", "date", "path", "sha256"):
                check("索引条目含 %s" % field, bool(entry.get(field)), str(entry))
            check("索引版本与清单一致",
                  str(entry.get("version")) == str(manifest.get("version")),
                  "%s vs %s" % (entry.get("version"), manifest.get("version")))
            check("索引元数据含描述", bool((entry.get("metadata") or {}).get("description")))

    print("\n" + "=" * 60)
    print("通过 %d 项，失败 %d 项" % (len(PASSED), len(FAILED)))
    if FAILED:
        for name in FAILED:
            print("  失败: %s" % name)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
