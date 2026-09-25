/* 邮箱配置表单：服务商预设的即时填充。
   1. 选择「服务商预设」后，自动填好 IMAP/SMTP 主机、端口与加密开关；
   2. 只填空值，不覆盖管理员已经手填的内容；
   3. 显示该服务商的授权码/注意事项提示。
   数据来源：模板里 {{ provider_presets|json_script:"provider-presets" }}（无内联脚本、无外部 CDN）。
*/
(function () {
  "use strict";

  function presetMap() {
    var node = document.getElementById("provider-presets");
    if (!node) return {};
    try {
      var list = JSON.parse(node.textContent);
      var map = {};
      list.forEach(function (item) {
        map[item.key] = item;
      });
      return map;
    } catch (err) {
      return {};
    }
  }

  function setValue(input, value) {
    if (!input || value === undefined || value === null || value === "") return false;
    if (String(input.value || "").trim() !== "") return false; // 不覆盖已填内容
    input.value = value;
    return true;
  }

  function setCheckbox(input, value) {
    if (!input || typeof value !== "boolean") return;
    input.checked = value;
  }

  function fill(form, preset) {
    setValue(form.querySelector("input[name='imap_host']"), preset.imap_host);
    setValue(form.querySelector("input[name='imap_port']"), preset.imap_port);
    setCheckbox(form.querySelector("input[name='imap_ssl']"), preset.imap_ssl);
    setValue(form.querySelector("input[name='smtp_host']"), preset.smtp_host);
    setValue(form.querySelector("input[name='smtp_port']"), preset.smtp_port);
    setCheckbox(form.querySelector("input[name='smtp_ssl']"), preset.smtp_ssl);
  }

  function showHint(node, preset) {
    if (!node) return;
    var text = (preset && preset.auth_hint) || "";
    if (!text) {
      node.classList.add("hidden");
      node.textContent = "";
      return;
    }
    node.textContent = (preset.label ? preset.label + "：" : "") + text;
    node.classList.remove("hidden");
  }

  document.addEventListener("DOMContentLoaded", function () {
    var select = document.getElementById("id_provider");
    if (!select) return;
    var form = select.closest("form") || document;
    var hint = document.getElementById("provider-hint");
    var presets = presetMap();

    function apply() {
      var preset = presets[select.value];
      if (!preset) {
        showHint(hint, null);
        return;
      }
      fill(form, preset);
      showHint(hint, preset);
    }

    select.addEventListener("change", apply);
    if (select.value) apply();
  });
})();
