/* 移动端导航抽屉（配合 app.css 的 @media (max-width:900px) 与 base.html 的 #nav-toggle）。

   设计取向：
   - 断点与 app.css 保持一致（900px），桌面端按钮 display:none，点了也没得点；
   - 打开后可通过「点遮罩 / 按 Esc / 点菜单里的链接」三种方式收起 —— 菜单里的链接
     可能被 HTMX 局部刷新（页面不跳转），所以必须显式收起，否则抽屉会一直挡着内容；
   - 视口拉回桌面宽度时复位，避免窗口尺寸变化后残留 nav-open 状态。
*/
(function () {
  "use strict";

  var BREAKPOINT = 900;

  function init() {
    var toggle = document.getElementById("nav-toggle");
    var backdrop = document.getElementById("nav-backdrop");
    var sidebar = document.getElementById("app-sidebar");
    if (!toggle || !sidebar) return;

    function isOpen() {
      return document.body.classList.contains("nav-open");
    }

    function setOpen(open) {
      document.body.classList.toggle("nav-open", open);
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      toggle.setAttribute("aria-label", open ? "关闭导航菜单" : "打开导航菜单");
      if (backdrop) backdrop.setAttribute("aria-hidden", open ? "false" : "true");
    }

    toggle.addEventListener("click", function () {
      setOpen(!isOpen());
    });

    if (backdrop) {
      backdrop.addEventListener("click", function () {
        setOpen(false);
      });
    }

    sidebar.addEventListener("click", function (event) {
      if (event.target.closest && event.target.closest("a")) setOpen(false);
    });

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && isOpen()) setOpen(false);
    });

    window.addEventListener("resize", function () {
      if (window.innerWidth > BREAKPOINT && isOpen()) setOpen(false);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
