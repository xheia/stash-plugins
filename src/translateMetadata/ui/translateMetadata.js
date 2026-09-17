/*
 * Metadata Translator —— 前端部分
 *
 * 在顶部导航栏加一个「译」按钮：翻译当前打开页面的实体（场景 / 演员 / 工作室 / 标签）。
 *
 * 实现要点：调用 Stash 的 runPluginOperation 同步执行插件，
 * 由后端脚本去调翻译接口。这样浏览器不直接请求翻译服务，
 * 既绕开了 CORS，也能复用插件里配置好的引擎和凭证。
 *
 * 注意：本文件是注入到页面的普通 JS，没有构建步骤，所以不能写 JSX，
 * 一律用 React.createElement。
 */
(function () {
  "use strict";

  var PluginApi = window.PluginApi;
  if (!PluginApi) {
    return;
  }

  var React = PluginApi.React;
  var h = React.createElement;
  var Button = PluginApi.libraries.Bootstrap.Button;
  var NavLink = PluginApi.libraries.ReactRouterDOM.NavLink;
  var solid = PluginApi.libraries.FontAwesomeSolid;
  var faIcon = solid.faLanguage || solid.faGlobe || solid.faEthernet;
  var useToast = PluginApi.hooks.useToast;

  var PLUGIN_ID = "translateMetadata";
  var RUN_OP_MUTATION =
    "mutation RunTranslateOperation($id: ID!, $args: Map) {" +
    "  runPluginOperation(plugin_id: $id, args: $args)" +
    "}";

  var ENTITY_MAP = {
    scenes: "scene",
    performers: "performer",
    studios: "studio",
    tags: "tag",
  };

  var ENTITY_LABEL = {
    scene: "场景",
    performer: "演员",
    studio: "工作室",
    tag: "标签",
  };

  // 从当前地址栏推断正在浏览的实体
  function currentEntity() {
    var match = window.location.pathname.match(
      /^\/(scenes|performers|studios|tags)\/(\d+)/
    );
    if (!match) {
      return null;
    }
    return { entity: ENTITY_MAP[match[1]], id: match[2] };
  }

  function runPluginOperation(args) {
    return fetch("/graphql", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        query: RUN_OP_MUTATION,
        variables: { id: PLUGIN_ID, args: args },
      }),
    })
      .then(function (response) {
        return response.json();
      })
      .then(function (body) {
        if (body.errors && body.errors.length) {
          throw new Error(
            body.errors
              .map(function (e) {
                return e.message;
              })
              .join("; ")
          );
        }
        return body.data ? body.data.runPluginOperation : null;
      });
  }

  function TranslateNavItem() {
    var toast = useToast();
    var state = React.useState(false);
    var busy = state[0];
    var setBusy = state[1];

    var target = currentEntity();
    var enabled = !!target && !busy;

    function onClick() {
      if (!target) {
        return;
      }
      setBusy(true);
      toast.toast({
        content:
          "正在翻译" + ENTITY_LABEL[target.entity] + " #" + target.id + "，请稍候…",
        delay: 2000,
      });

      runPluginOperation({ mode: "single", entity: target.entity, id: target.id })
        .then(function (result) {
          if (!result) {
            toast.success("插件未返回结果");
            return;
          }
          if (result.error) {
            toast.error(result.error);
            return;
          }
          if (!result.count) {
            toast.success(result.message || "无需翻译");
            return;
          }
          toast.success(
            "已翻译 " + result.count + " 个字段，刷新页面即可看到：" + result.result
          );
        })
        .catch(function (err) {
          toast.error(err);
        })
        .then(function () {
          setBusy(false);
        });
    }

    var title = target
      ? "翻译当前" + ENTITY_LABEL[target.entity] + "的标题与简介"
      : "请先打开场景 / 演员 / 工作室 / 标签的详情页";

    return h(
      "div",
      { className: "nav-utility mrm", style: { display: "flex" } },
      h(
        Button,
        {
          className: "minimal d-flex align-items-center h-100",
          disabled: !enabled,
          title: title,
          onClick: onClick,
        },
        h(PluginApi.components.Icon, { icon: faIcon, className: busy ? "fa-spin" : "" })
      )
    );
  }

  function TasksNavItem() {
    return h(
      NavLink,
      {
        className: "nav-utility mrm",
        exact: true,
        to: "/settings?tab=tasks",
        title: "打开任务页，可批量翻译整个库",
      },
      h(
        Button,
        { className: "minimal d-flex align-items-center h-100" },
        h(PluginApi.components.Icon, { icon: solid.faList || faIcon })
      )
    );
  }

  PluginApi.patch.before("MainNavBar.UtilityItems", function (props) {
    return [
      {
        children: h(
          React.Fragment,
          null,
          h(TranslateNavItem),
          h(TasksNavItem),
          props.children
        ),
      },
    ];
  });
})();
