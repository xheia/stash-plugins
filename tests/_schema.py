# -*- coding: utf-8 -*-
"""Stash v0.31.1 GraphQL schema 契约：插件依赖的名字白名单 + 请求校验器。

为什么单独抽一个模块：这类 bug 曾经真实发生过 —— 插件用
`"%sUpdateInput" % entity[0].upper() + entity[1:]` 拼类型名，
因为 Python 里 `%` 的优先级高于 `+`，实际被解析成
`("S" + "UpdateInput") + "cene"` = `SUpdateInputcene`，
Stash 直接回 GRAPHQL_VALIDATION_FAILED，四个实体的写回全线失效。
而当时 277 项测试全绿 —— 因为假 Stash 服务只按正则匹配 mutation 名字，
根本不看变量类型名，畸形的类型名照样被当成合法请求处理。

所以这里做两件事：
  1. 把 schema 里的名字写成契约常量，测试拿它和插件用的名字硬碰硬对比；
  2. 提供一个校验器，假服务端在收到请求时先跑一遍，模拟真实 Stash 的
     校验失败路径，让任何名字畸变都在测试里炸出来。

出处（stash v0.31.1 源码，本仓库不含 schema 副本，只记录事实性的名字）：
  graphql/schema/schema.graphql           —— Mutation 段的四个 update 入口
  graphql/schema/types/scene.graphql      —— findScene / findScenes / SceneUpdateInput / FindScenesResultType
  graphql/schema/types/performer.graphql  —— findPerformer / findPerformers / PerformerUpdateInput / FindPerformersResultType
  graphql/schema/types/studio.graphql     —— findStudio / findStudios / StudioUpdateInput / FindStudiosResultType
  graphql/schema/types/tag.graphql        —— findTag / findTags / TagUpdateInput / FindTagsResultType
  graphql/schema/types/metadata.graphql   —— CustomFieldsInput{full,partial,remove}
  graphql/schema/types/filters.graphql    —— FindFilterType{page,per_page,sort,direction} / SortDirectionEnum{ASC,DESC}
"""
from __future__ import annotations

import re

# --------------------------------------------------------------------------- #
# 契约：实体 -> schema 里的字面名字
# --------------------------------------------------------------------------- #
# 每项格式：(update_mutation, update_input_type, find_one, find_list, list_key)
ENTITY_CONTRACT = {
    "scene": ("sceneUpdate", "SceneUpdateInput", "findScene", "findScenes", "scenes"),
    "performer": ("performerUpdate", "PerformerUpdateInput",
                  "findPerformer", "findPerformers", "performers"),
    "studio": ("studioUpdate", "StudioUpdateInput", "findStudio", "findStudios", "studios"),
    "tag": ("tagUpdate", "TagUpdateInput", "findTag", "findTags", "tags"),
}

# 每个实体在列表查询里必须能取到的字段（插件靠这些字段读写）
ITERABLE_FIELDS = ("id", "custom_fields")

# 每个实体的可翻译字段（对应 schema 里的标量字段名）
TRANSLATABLE_FIELDS = {
    "scene": {"title", "details"},
    "performer": {"name", "details"},
    "studio": {"name", "details"},
    "tag": {"name", "description"},
}

# 插件使用的非实体类型（标量 + 输入类型 + 枚举）
AUXILIARY_TYPES = {
    "ID", "Int", "String", "Boolean", "Float",
    "Map", "FindFilterType", "CustomFieldsInput", "SortDirectionEnum",
}

# 每个 XxxUpdateInput 接受的字段（插件只允许往里塞这些键）
# 出处：graphql/schema/types/*.graphql 的 input XxxUpdateInput 定义
UPDATE_INPUT_KEYS = {
    "scene": {"id", "title", "details", "custom_fields"},
    "performer": {"id", "name", "details", "custom_fields"},
    "studio": {"id", "name", "details", "custom_fields"},
    "tag": {"id", "name", "description", "aliases", "custom_fields"},
}

# FindFilterType 允许的键
FIND_FILTER_KEYS = {"q", "page", "per_page", "sort", "direction"}

# SortDirectionEnum 的合法取值
SORT_DIRECTIONS = {"ASC", "DESC"}

# CustomFieldsInput 允许的键
CUSTOM_FIELDS_KEYS = {"full", "partial", "remove"}

KNOWN_TYPES = {spec[1] for spec in ENTITY_CONTRACT.values()} | AUXILIARY_TYPES
KNOWN_MUTATIONS = {spec[0] for spec in ENTITY_CONTRACT.values()}
KNOWN_QUERIES = set()
for _spec in ENTITY_CONTRACT.values():
    KNOWN_QUERIES.add(_spec[2])
    KNOWN_QUERIES.add(_spec[3])

# mutation 名 -> 它唯一合法的 input 类型
MUTATION_INPUT_TYPES = {spec[0]: spec[1] for spec in ENTITY_CONTRACT.values()}


# --------------------------------------------------------------------------- #
# 校验器：模拟 Stash 的 GraphQL 校验
# --------------------------------------------------------------------------- #
class SchemaViolation(ValueError):
    """名字不在契约里，真实 Stash 会回 GRAPHQL_VALIDATION_FAILED。"""


_VAR_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)\s*:\s*([A-Za-z_][A-Za-z0-9_]*)")
_CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_OP_NAME_RE = re.compile(r"\b(?:query|mutation|subscription)\s+([A-Za-z_][A-Za-z0-9_]*)")


def variable_types(query):
    """取出 query 里所有 `$var: Type` 的 (变量名, 类型名)。"""
    return _VAR_RE.findall(query or "")


def called_fields(query):
    """取出 query 里所有形如 `name(` 的字段名（含 mutation 与查询入口）。"""
    return _CALL_RE.findall(query or "")


def operation_names(query):
    """取出具名操作的名字（`query List(...)` 里的 List）。

    这些是操作名不是字段名，校验顶层字段时要排除，否则报假警。
    """
    return set(_OP_NAME_RE.findall(query or ""))


def validate(query):
    """校验一份 GraphQL 文档是否只用了契约里的名字。

    通过返回 None，违规抛 SchemaViolation（附上与真实 Stash 一致的报错措辞）。
    """
    if not query:
        raise SchemaViolation("空查询")

    # 1) 变量类型必须真实存在
    for var, type_name in variable_types(query):
        if type_name not in KNOWN_TYPES:
            raise SchemaViolation('Unknown type "%s".' % type_name)

    # 2) update mutation 的 $input 类型必须与 mutation 配对
    for mutation, expected in MUTATION_INPUT_TYPES.items():
        if not re.search(r"\b%s\s*\(" % mutation, query):
            continue
        found = [t for v, t in variable_types(query) if v == "input"]
        if not found:
            raise SchemaViolation("mutation %s 没有声明 $input 变量" % mutation)
        actual = found[0]
        if actual != expected:
            raise SchemaViolation(
                'Variable "$input" of type "%s!" used in position expecting type "%s!".'
                % (actual, expected)
            )

    # 3) 顶层入口字段必须在契约里（find* / update*）
    operations = operation_names(query)
    for name in called_fields(query):
        if name in operations:
            continue
        if name in KNOWN_MUTATIONS or name in KNOWN_QUERIES:
            continue
        # configuration / plugins 是插件设置读取用的公共入口
        if name in ("query", "mutation", "subscription",
                    "configuration", "plugins", "runPluginOperation"):
            continue
        raise SchemaViolation("Unknown field \"%s\"." % name)

    return None


def is_valid(query):
    try:
        validate(query)
        return True
    except SchemaViolation:
        return False
