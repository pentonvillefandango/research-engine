"use strict";
document.addEventListener("DOMContentLoaded", function () {
  var button = document.getElementById("copy-new-key");
  var code = document.getElementById("new-key");
  if (!button || !code) return;
  button.addEventListener("click", function () {
    var text = code.textContent || "";
    function selectText() {
      var range = document.createRange();
      range.selectNodeContents(code);
      var sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      button.textContent = "Selected: press Ctrl/Cmd+C";
    }
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(
        function () { button.textContent = "Copied"; },
        selectText
      );
    } else {
      selectText();
    }
  });
});
