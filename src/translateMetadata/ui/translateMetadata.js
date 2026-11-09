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
  // 库访问全部做保护：任何一个库缺失都不该让整个 UI 脚本抛异常，
  // 更不能影响同一页面里的其它插件。
  var libs = PluginApi.libraries || {};
  var Button = (libs.Bootstrap || {}).Button || "button";
  var NavLink =
    (libs.ReactRouterDOM || {}).NavLink ||
    function (props) {
      return h("a", { href: props.to }, props.children);
    };
  var solid = libs.FontAwesomeSolid || {};
  var faIcon = solid.faLanguage || solid.faGlobe || solid.faEthernet;
  var useToast =
    PluginApi.hooks && PluginApi.hooks.useToast
      ? PluginApi.hooks.useToast
      : function () {
          return {
            toast: function () {},
            success: function () {},
            error: function () {},
          };
        };

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

  // 防抖：CF 隧道/弱网下按钮可能被连点，重复 runPluginOperation 会把额度打爆。
  // 1.2 秒内的重复点击直接忽略；真正执行期间还有 busy 状态兜底。
  var lastRunAt = 0;
  var DEBOUNCE_MS = 1200;

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
      var now = Date.now();
      if (now - lastRunAt < DEBOUNCE_MS) {
        return;
      }
      lastRunAt = now;

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
            "已翻译 " + result.count + " 个字段，页面即将刷新：" + result.result
          );
          // 服务端写库后 Stash 前端（Apollo 缓存）不会自己重新拉数据，
          // 整页刷新是唯一能保证详情页重新读取的可靠方式。
          // 延迟一下让 toast 先展示出来。
          setTimeout(function () {
            window.location.reload();
          }, 1200);
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

  // 错误隔离：我们的按钮渲染失败（图标库缺失、API 变更等）绝不能把整个导航栏
  // 一起带崩 —— 否则同在这块 DOM 里的其它插件（如 SFWSwitch）会跟着消失。
  class ErrorCatcher extends React.Component {
    constructor(p) {
      super(p);
      this.state = { failed: false };
    }
    static getDerivedStateFromError() {
      return { failed: true };
    }
    componentDidCatch(err) {
      try {
        console.error("[Metadata Translator] 导航栏按钮渲染失败：", err);
      } catch (e) {
        /* 忽略 */
      }
    }
    render() {
      return this.state.failed ? null : this.props.children;
    }
  }

  // 只在桌面导航栏（.navbar-buttons）里渲染。MainNavBar.UtilityItems 在页面上
  // 有两处（移动端折叠菜单 + 桌面按钮区），不加以区分会出现两套按钮，
  // 也会让 React 更频繁地改动 .navbar-buttons 的子树。
  function NavSlot() {
    var ref = React.useRef(null);
    var state = React.useState(false);
    var visible = state[0];
    var setVisible = state[1];

    React.useEffect(function () {
      var el = ref.current;
      try {
        if (el && el.closest && el.closest(".navbar-buttons")) {
          setVisible(true);
        }
      } catch (e) {
        /* 忽略：老浏览器没有 closest 就保持隐藏 */
      }
    }, []);

    return h(
      "span",
      { ref: ref, style: { display: visible ? "contents" : "none" } },
      visible
        ? h(
            React.Fragment,
            null,
            h(ErrorCatcher, { key: "mt-translate" }, h(TranslateNavItem)),
            h(ErrorCatcher, { key: "mt-tasks" }, h(TasksNavItem))
          )
        : null
    );
  }

  try {
    PluginApi.patch.before("MainNavBar.UtilityItems", function (props) {
      return [
        {
          children: h(
            React.Fragment,
            null,
            h(NavSlot, { key: "metadata-translator-nav" }),
            props.children
          ),
        },
      ];
    });
  } catch (e) {
    // 注册失败就静默降级：后端任务与 Hook 仍然可用，不影响任何其它插件。
    try {
      console.error("[Metadata Translator] 导航栏注册失败：", e);
    } catch (ignored) {
      /* 忽略 */
    }
  }
})();
