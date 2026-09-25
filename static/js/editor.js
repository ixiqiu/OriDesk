/* 工单详情页交互：
   1. Quill 富文本编辑器 ↔ 隐藏字段 body_html 同步
   2. 纯文本模式切换
   3. 远程图片占位图点击后按 data-src 加载（配合后端 sanitize_html 的占位策略）
   4. HTMX 局部刷新后的重新绑定
*/
(function () {
  "use strict";

  function initRemoteImages(scope) {
    (scope || document).querySelectorAll("img.remote-image[data-remote='1']").forEach(function (img) {
      if (img.dataset.bound === "1") return;
      img.dataset.bound = "1";
      img.style.cursor = "pointer";
      img.title = "点击加载远程图片";
      img.addEventListener("click", function () {
        var src = img.getAttribute("data-src");
        if (!src) return;
        img.removeAttribute("data-remote");
        img.classList.remove("remote-image");
        img.setAttribute("src", src);
      });
    });
  }

  function initEditor() {
    var holder = document.getElementById("reply-editor");
    var hidden = document.querySelector("#reply-form input[name='body_html']");
    if (!holder || !hidden || typeof Quill === "undefined" || holder.dataset.ready === "1") return;
    holder.dataset.ready = "1";

    var quill = new Quill(holder, {
      theme: "snow",
      placeholder: "输入回复内容…",
      modules: {
        toolbar: [
          ["bold", "italic", "underline", "strike"],
          [{ header: [1, 2, 3, false] }],
          [{ list: "ordered" }, { list: "bullet" }],
          ["blockquote", "link"],
          [{ color: [] }, { background: [] }],
          ["clean"],
        ],
      },
    });

    function sync() {
      var html = quill.root.innerHTML.trim();
      hidden.value = html === "<p><br></p>" ? "" : html;
    }
    quill.on("text-change", sync);
    var form = document.getElementById("reply-form");
    if (form) form.addEventListener("submit", sync);

    var toggle = document.getElementById("toggle-plain");
    var plainWrap = document.getElementById("plain-wrap");
    if (toggle && plainWrap) {
      toggle.addEventListener("change", function () {
        var plain = toggle.checked;
        holder.classList.toggle("hidden", plain);
        plainWrap.classList.toggle("hidden", !plain);
        if (plain) {
          var text = hidden.value.replace(/<[^>]+>/g, "").trim();
          var textarea = plainWrap.querySelector("textarea");
          if (textarea && !textarea.value) textarea.value = text;
        } else {
          var textarea = plainWrap.querySelector("textarea");
          if (textarea && textarea.value) {
            quill.clipboard.dangerouslyPasteHTML(
              textarea.value.split("\n").map(function (line) {
                return "<p>" + line.replace(/&/g, "&amp;").replace(/</g, "&lt;") + "</p>";
              }).join("")
            );
          }
        }
      });
    }
    sync();
  }

  document.addEventListener("DOMContentLoaded", function () {
    initEditor();
    initRemoteImages(document);
    document.body.addEventListener("htmx:afterSwap", function (event) {
      initRemoteImages(event.target);
    });
  });

  window.OrDesk = { initEditor: initEditor, initRemoteImages: initRemoteImages };
})();
