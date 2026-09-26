/* 底部弹层开合（配合 app.css 的 .sheet-dialog / .sheet）。

   用原生 <dialog>.showModal()：遮罩、Esc 关闭、焦点陷阱、背景惰性化都由浏览器负责，
   不需要自己写 scrim 与按键监听。本脚本只做两件事：
     1) [data-sheet-open="<id>"] 点击 → 打开对应 dialog；
     2) [data-sheet-close] 点击 → 关闭所在 dialog。
*/
(function () {
  "use strict";

  function init() {
    document.querySelectorAll("[data-sheet-open]").forEach(function (trigger) {
      trigger.addEventListener("click", function () {
        var dialog = document.getElementById(trigger.getAttribute("data-sheet-open"));
        if (dialog && typeof dialog.showModal === "function") {
          dialog.showModal();
        }
      });
    });

    document.querySelectorAll("[data-sheet-close]").forEach(function (button) {
      button.addEventListener("click", function () {
        var dialog = button.closest("dialog");
        if (dialog) dialog.close();
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
