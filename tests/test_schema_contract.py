#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""插件与 Stash GraphQL schema 的接口契约测试。

为什么需要这个文件：v1.1.0 的 `update_mutation` 里写了一句

    input_type = "%sUpdateInput" % entity[0].upper() + entity[1:]

Python 里 `%` 的优先级高于 `+`，所以它等价于
`("S" + "UpdateInput") + "cene"` = `SUpdateInputcene`。
Stash 回报 `GRAPHQL_VALIDATION_FAILED: Unknown type "SUpdateInputcene"`，
四个实体的写回全线失效 —— 而当时 277 项测试全绿，因为假 Stash 服务
只按正则匹配 mutation 名字（`sceneUpdate(`），根本不看变量类型名。

本文件把"插件用的 GraphQL 名字必须真实存在于 schema"变成硬断言：
  * 插件里的每个名字都与 tests/_schema.py 的契约逐字对比
  * 生成的 GraphQL 文档交给契约校验器过一遍（模拟 Stash 的校验）
  * 把当年那个畸形表达式作为回归锚点，确认它现在必然被拦下

用法：
    python3 tests/test_schema_contract.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "src", "translateMetadata"))

import _schema  # noqa: E402
import fields  # noqa: E402

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print("  [PASS] %s" % name)
    else:
        FAILED.append(name)
        print("  [FAIL] %s  %s" % (name, detail))


def validate_detail(doc):
    """跑一遍契约校验器，返回 (是否通过, 报错文本)。"""
    try:
        _schema.validate(doc)
        return True, ""
    except _schema.SchemaViolation as exc:
        return False, str(exc)


class FakeSettings:
    """够用的设置桩：build_update_input 只依赖这两处。"""

    def __init__(self, keep_original=True, tag_name_mode="rename"):
        self._keep = keep_original
        self.tag_name_mode = tag_name_mode

    def __getitem__(self, key):
        if key == "keep_original":
            return self._keep
        raise KeyError(key)


def main():
    print("契约来源: tests/_schema.py（对照 stash v0.31.1 源码的 schema 定义）")

    print("\n1) 插件里的名字与 schema 契约逐字一致")
    check("插件实体集合与契约相同",
          set(fields.ENTITY_SPECS) == set(_schema.ENTITY_CONTRACT),
          "插件 %s / 契约 %s" % (sorted(fields.ENTITY_SPECS), sorted(_schema.ENTITY_CONTRACT)))
    check("ALL_ENTITIES 与契约相同",
          set(fields.ALL_ENTITIES) == set(_schema.ENTITY_CONTRACT),
          str(fields.ALL_ENTITIES))

    name_pairs = [
        ("update", 0), ("update_input", 1), ("find_one", 2), ("find_list", 3), ("list_key", 4),
    ]
    for entity, contract in _schema.ENTITY_CONTRACT.items():
        spec = fields.ENTITY_SPECS.get(entity) or {}
        for key, index in name_pairs:
            expect = contract[index]
            check("%s.%s = %s" % (entity, key, expect), spec.get(key) == expect,
                  "实际 %r" % (spec.get(key),))

    print("\n2) 生成的 mutation 文档与契约逐字一致")
    for entity, contract in _schema.ENTITY_CONTRACT.items():
        update_mut, update_input = contract[0], contract[1]
        doc = fields.update_mutation(entity)
        want = "mutation Update($input: %s!) { %s(input: $input) { id } }" % (update_input, update_mut)
        check("%s 的 mutation 文档" % entity, doc == want, "得到 %r" % doc)
        check("%s 的 mutation 通过契约校验" % entity, validate_detail(doc)[0],
              validate_detail(doc)[1])

    print("\n3) 生成的查询文档能通过 schema 校验")
    for entity in _schema.ENTITY_CONTRACT:
        for kind, doc in (("one", fields.one_query(entity)), ("list", fields.list_query(entity))):
            ok, err = validate_detail(doc)
            check("%s 的 %s 查询通过校验" % (entity, kind), ok, err)

        one = fields.one_query(entity)
        missing = [k for k in ("id", "custom_fields") if k not in one]
        check("%s 查询取出 id 与 custom_fields" % entity, not missing, "缺 %s" % missing)
        for key in sorted(_schema.TRANSLATABLE_FIELDS[entity]):
            check("%s 查询含可翻译字段 %s" % (entity, key), key in one)

        listing = fields.list_query(entity)
        check("%s 列表查询含 count" % entity, "count" in listing)
        check("%s 列表查询含 %s" % (entity, _schema.ENTITY_CONTRACT[entity][4]),
              _schema.ENTITY_CONTRACT[entity][4] in listing)

    print("\n4) 回归锚点：当年那个优先级 bug 的产物必须被拦下")
    good = "mutation Update($input: SceneUpdateInput!) { sceneUpdate(input: $input) { id } }"
    check("校验器放过正确文档", _schema.is_valid(good), validate_detail(good)[1])

    for entity, contract in _schema.ENTITY_CONTRACT.items():
        # 这一行就是 v1.1.0 出事的原始写法，故意保留下来当回归锚点
        legacy_type = "%sUpdateInput" % entity[0].upper() + entity[1:]
        check("旧写法产物 %s 确实不等于契约值 %s" % (legacy_type, contract[1]),
              legacy_type != contract[1],
              "旧写法竟然也对？契约可能被写错了")
        bad_doc = "mutation Update($input: %s!) { %s(input: $input) { id } }" % (legacy_type, contract[0])
        check("校验器拦下旧写法产物（%s）" % legacy_type, not _schema.is_valid(bad_doc),
              "竟然通过了：%r" % bad_doc)

    cross = "mutation Update($input: PerformerUpdateInput!) { sceneUpdate(input: $input) { id } }"
    check("校验器拦下类型与 mutation 不配对", not _schema.is_valid(cross))

    fantasy = "mutation Update($input: SceneUpdateInputXX!) { sceneUpdate(input: $input) { id } }"
    check("校验器拦下不存在的类型", not _schema.is_valid(fantasy))

    no_var = "mutation Update { sceneUpdate(input: $input) { id } }"
    check("校验器拦下缺少 $input 声明的 mutation", not _schema.is_valid(no_var))

    print("\n5) 写回输入只会用 UpdateInput 允许的键")
    samples = {
        "scene": {"id": "1", "title": "原标题", "details": "旧简介", "custom_fields": {}},
        "performer": {"id": "2", "name": "Old Name", "details": "旧简介", "custom_fields": {}},
        "studio": {"id": "3", "name": "Old Studio", "details": "旧简介", "custom_fields": {}},
        "tag": {"id": "4", "name": "Old Tag", "description": "旧描述",
                "aliases": ["已有别名"], "custom_fields": {}},
    }
    translate_field = {"scene": "title", "performer": "name", "studio": "name", "tag": "name"}

    for entity, record in samples.items():
        key = translate_field[entity]
        translations = [{"field": key, "original": record[key],
                         "translated": "中文译文", "engine": "stub"}]
        inp, written = fields.build_update_input(entity, record, translations,
                                                 FakeSettings(keep_original=True))
        allowed = _schema.UPDATE_INPUT_KEYS[entity]
        extra = sorted(set(inp) - allowed)
        check("%s 写回输入没有多余键" % entity, not extra, "多余: %s" % extra)
        check("%s 写回包含 id" % entity, inp.get("id") == record["id"])
        check("%s 写回了译文" % entity, inp.get(key) == "中文译文")
        check("%s 记录了写入字段" % entity, key in written)
        custom = inp.get("custom_fields") or {}
        custom_extra = sorted(set(custom) - _schema.CUSTOM_FIELDS_KEYS)
        check("%s 的 custom_fields 用 CustomFieldsInput 的键" % entity, not custom_extra,
              "多余: %s" % custom_extra)
        check("%s 用 partial 写入原文" % entity, "partial" in custom)
        check("%s 原文键带 tr_src_ 前缀" % entity,
              fields.SRC_PREFIX + key in (custom.get("partial") or {}))

    # 标签改名 vs 追加别名
    tag_record = samples["tag"]
    alias_inp, alias_written = fields.build_update_input(
        "tag", tag_record,
        [{"field": "name", "original": "Old Tag", "translated": "中文标签", "engine": "stub"}],
        FakeSettings(keep_original=True, tag_name_mode="alias"))
    check("别名模式不改 name", "name" not in alias_inp)
    check("别名模式把译文并进 aliases", "中文标签" in (alias_inp.get("aliases") or []))
    check("别名模式保留原有别名", "已有别名" in (alias_inp.get("aliases") or []))
    check("别名模式记录为 name(alias)", "name(alias)" in alias_written)

    # 回滚输入的 remove 必须是字符串列表（CustomFieldsInput.remove: [String!]）
    rollback_record = {
        "id": "9", "title": "中文标题", "details": "中文简介",
        "custom_fields": {fields.SRC_PREFIX + "title": "Original Title"},
    }
    rb_inp, restored = fields.build_rollback_input("scene", rollback_record)
    check("回滚还原了标题", rb_inp.get("title") == "Original Title")
    check("回滚记录了还原字段", restored == ["title"], str(restored))
    remove = (rb_inp.get("custom_fields") or {}).get("remove")
    check("回滚的 remove 是非空字符串列表",
          isinstance(remove, list) and remove and all(isinstance(x, str) for x in remove),
          str(remove))

    print("\n6) 列表过滤参数合法")
    filt = fields.build_list_filter(2, 100)
    extra = sorted(set(filt) - _schema.FIND_FILTER_KEYS)
    check("过滤器没有多余键", not extra, "多余: %s" % extra)
    check("按 id 升序翻页（翻译会改标题，按标题排会打乱翻页）", filt.get("sort") == "id")
    check("direction 是 SortDirectionEnum 合法值",
          filt.get("direction") in _schema.SORT_DIRECTIONS, str(filt.get("direction")))
    check("page / per_page 是整数",
          isinstance(filt.get("page"), int) and isinstance(filt.get("per_page"), int))

    print("\n" + "=" * 60)
    print("通过 %d 项，失败 %d 项" % (len(PASSED), len(FAILED)))
    if FAILED:
        for name in FAILED:
            print("  失败: %s" % name)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
