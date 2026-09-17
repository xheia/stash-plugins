# -*- coding: utf-8 -*-
"""实体字段映射：查询语句、可翻译字段、写回输入构造。

覆盖场景 / 演员 / 工作室 / 标签四个实体。

关于分页稳定性：批量任务按 `sort: "id", direction: "ASC"` 翻页。
这一点必须保证 —— 因为翻译会改写 title / name，如果按 title 排序，
翻页过程中顺序会被自己打乱，导致漏译或重复处理。四个实体的排序白名单里
都包含 "id"（见 pkg/sqlite/*.go 的 *SortOptions）。

关于原文保护：译文写回时，原文可以存进 custom_fields 的 tr_src_<字段> 键，
「回滚翻译」任务据此把原文还回去。写回一律用 partial 模式，不会冲掉其它自定义字段。
"""
from __future__ import annotations

SRC_PREFIX = "tr_src_"
ENGINE_PREFIX = "tr_engine_"

# 注意：下面每个实体的 update_input / find_one / find_list / list_key / update
# 都是 Stash GraphQL schema 里的 **字面名字**，一律写死，不做任何拼接。
# 曾经在 update_mutation 里用 "%sUpdateInput" % entity[0].upper() + entity[1:]
# 拼类型名，因 % 优先级高于 + 被解析成 ("S" + "UpdateInput") + "cene"
# = "SUpdateInputcene"，导致所有写回被 Stash 以 GRAPHQL_VALIDATION_FAILED 拒绝。
# 出处：stash v0.31.1 源码 graphql/schema/types/{scene,performer,studio,tag}.graphql
# 与 graphql/schema/schema.graphql 的 Mutation 段。

# 字段角色：
#   plain     —— 普通字段，直接覆盖
#   tag_name  —— 标签名称，受 tag_name_mode 控制（改名 or 追加别名）
ENTITY_SPECS = {
    "scene": {
        "label": "场景",
        "find_one": "findScene",
        "find_list": "findScenes",
        "list_key": "scenes",
        "update": "sceneUpdate",
        "update_input": "SceneUpdateInput",
        "fields": [
            {"key": "title", "gate": "translate_title", "label": "标题", "role": "plain"},
            {"key": "details", "gate": "translate_details", "label": "简介", "role": "plain"},
        ],
    },
    "performer": {
        "label": "演员",
        "find_one": "findPerformer",
        "find_list": "findPerformers",
        "list_key": "performers",
        "update": "performerUpdate",
        "update_input": "PerformerUpdateInput",
        "fields": [
            {"key": "name", "gate": "translate_names", "label": "姓名", "role": "plain"},
            {"key": "details", "gate": "translate_details", "label": "简介", "role": "plain"},
        ],
    },
    "studio": {
        "label": "工作室",
        "find_one": "findStudio",
        "find_list": "findStudios",
        "list_key": "studios",
        "update": "studioUpdate",
        "update_input": "StudioUpdateInput",
        "fields": [
            {"key": "name", "gate": "translate_title", "label": "名称", "role": "plain"},
            {"key": "details", "gate": "translate_details", "label": "简介", "role": "plain"},
        ],
    },
    "tag": {
        "label": "标签",
        "find_one": "findTag",
        "find_list": "findTags",
        "list_key": "tags",
        "update": "tagUpdate",
        "update_input": "TagUpdateInput",
        "fields": [
            {"key": "name", "gate": "translate_title", "label": "名称", "role": "tag_name"},
            {"key": "description", "gate": "translate_details", "label": "描述", "role": "plain"},
        ],
    },
}

ALL_ENTITIES = ["scene", "performer", "studio", "tag"]


def spec(entity):
    return ENTITY_SPECS[entity]


def label(entity):
    return ENTITY_SPECS[entity]["label"]


def fields(entity):
    return ENTITY_SPECS[entity]["fields"]


def field_role(entity, key):
    for item in ENTITY_SPECS[entity]["fields"]:
        if item["key"] == key:
            return item["role"]
    return "plain"


def enabled_fields(entity, settings):
    """按设置过滤出本次要处理的字段。"""
    result = []
    for item in ENTITY_SPECS[entity]["fields"]:
        if settings.field_enabled(item["gate"]):
            result.append(item)
    return result


# --------------------------------------------------------------------------- #
# 查询语句
# --------------------------------------------------------------------------- #
def _field_selection(entity):
    keys = [f["key"] for f in ENTITY_SPECS[entity]["fields"]]
    # 标签要额外取 aliases，追加别名模式需要
    if entity == "tag":
        keys.append("aliases")
    return " ".join(keys)


def one_query(entity):
    s = ENTITY_SPECS[entity]
    return (
        "query One($id: ID!) { %s(id: $id) { id %s custom_fields } }"
        % (s["find_one"], _field_selection(entity))
    )


def list_query(entity):
    s = ENTITY_SPECS[entity]
    return (
        "query List($filter: FindFilterType) { %s(filter: $filter) "
        "{ count %s { id %s custom_fields } } }"
        % (s["find_list"], s["list_key"], _field_selection(entity))
    )


def update_mutation(entity):
    s = ENTITY_SPECS[entity]
    return (
        "mutation Update($input: %s!) { %s(input: $input) { id } }"
        % (s["update_input"], s["update"])
    )


def extract_one(entity, data):
    return (data or {}).get(ENTITY_SPECS[entity]["find_one"])


def extract_list(entity, data):
    result = (data or {}).get(ENTITY_SPECS[entity]["find_list"]) or {}
    return result.get("count") or 0, result.get(ENTITY_SPECS[entity]["list_key"]) or []


# --------------------------------------------------------------------------- #
# 写回输入构造
# --------------------------------------------------------------------------- #
def build_list_filter(page, per_page):
    return {
        "page": page,
        "per_page": per_page,
        "sort": "id",
        "direction": "ASC",
    }


def _existing_custom(record):
    raw = record.get("custom_fields")
    return raw if isinstance(raw, dict) else {}


def original_of(record, key):
    """取回此前保存的原文（如果有）。"""
    return _existing_custom(record).get(SRC_PREFIX + key)


def build_update_input(entity, record, translations, settings):
    """把一批译文组装成 mutation 的 input。

    translations: [{"field": str, "original": str, "translated": str, "engine": str}, ...]
    返回 (input_dict, 实际写入的字段名列表)。input_dict 为 None 表示无需写回。
    """
    inp = {"id": str(record.get("id"))}
    custom = {}
    written = []

    for item in translations:
        key = item["field"]
        translated = item["translated"]
        original = item["original"]
        engine = item.get("engine") or ""

        if entity == "tag" and key == "name" and settings.tag_name_mode == "alias":
            # 保留原名，仅追加中文别名：可逆且不影响其它引用该标签的场景
            aliases = list(record.get("aliases") or [])
            if translated in aliases:
                continue
            aliases.append(translated)
            inp["aliases"] = aliases
            written.append("name(alias)")
            continue

        inp[key] = translated
        written.append(key)

        if settings["keep_original"] and original:
            custom[SRC_PREFIX + key] = original
            custom[ENGINE_PREFIX + key] = engine

    if not written:
        return None, []

    if custom:
        inp["custom_fields"] = {"partial": custom}

    return inp, written


def build_rollback_input(entity, record):
    """根据 custom_fields 里保存的原文还原字段。

    返回 (input_dict, 还原的字段列表)。无需还原时 input 为 None。
    """
    custom = _existing_custom(record)
    if not custom:
        return None, []

    inp = {"id": str(record.get("id"))}
    restore = []
    remove_keys = []

    for item in ENTITY_SPECS[entity]["fields"]:
        key = item["key"]
        src_key = SRC_PREFIX + key
        if src_key not in custom:
            continue
        original = custom.get(src_key)
        if original is None:
            continue
        # 只有当当前值确实还是我们翻译出来的，才回滚，避免覆盖手工修改
        current = record.get(key)
        if current == original:
            remove_keys.append(src_key)
            engine_key = ENGINE_PREFIX + key
            if engine_key in custom:
                remove_keys.append(engine_key)
            continue
        inp[key] = original
        restore.append(key)
        remove_keys.append(src_key)
        engine_key = ENGINE_PREFIX + key
        if engine_key in custom:
            remove_keys.append(engine_key)

    if not restore:
        # 没有需要还原的字段，但可能有残留的标记键需要清掉
        if remove_keys:
            return {"id": str(record.get("id")), "custom_fields": {"remove": remove_keys}}, []
        return None, []

    if remove_keys:
        inp["custom_fields"] = {"remove": remove_keys}

    return inp, restore
