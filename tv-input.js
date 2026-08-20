(function () {
    "use strict";

    function isTizen() {
        if (/Tizen/i.test(navigator.userAgent) || window.tizen) { return true; }
        var parameters = new URLSearchParams(window.location.search);
        if (parameters.get("platform") === "tizen") { return true; }
        try {
            return new URL(parameters.get("next") || "", window.location.origin)
                .searchParams.get("platform") === "tizen";
        } catch (_error) { return false; }
    }

    function keyboardCharacter(event) {
        var code = event.which || event.keyCode || 0;
        if (code >= 65 && code <= 90) {
            var letter = String.fromCharCode(code);
            return event.shiftKey ? letter : letter.toLowerCase();
        }
        if (code >= 48 && code <= 57) {
            return event.shiftKey ? ")!@#$%^&*("[code - 48] : String.fromCharCode(code);
        }
        if (code >= 96 && code <= 105) { return String(code - 96); }
        if (code === 32) { return " "; }
        var plain = { 186: ";", 187: "=", 188: ",", 189: "-", 190: ".", 191: "/", 192: "`", 219: "[", 220: "\\", 221: "]", 222: "'" };
        var shifted = { 186: ":", 187: "+", 188: "<", 189: "_", 190: ">", 191: "?", 192: "~", 219: "{", 220: "|", 221: "}", 222: "\"" };
        return (event.shiftKey ? shifted : plain)[code] || "";
    }

    function replaceSelection(field, replacement, start, end) {
        if (typeof field.setRangeText === "function") {
            field.setRangeText(replacement, start, end, "end");
        } else {
            field.value = field.value.slice(0, start) + replacement + field.value.slice(end);
        }
        field.dispatchEvent(new Event("input", { bubbles: true }));
    }

    function install(field, toggle) {
        field.readOnly = true;
        field.setAttribute("inputmode", "none");
        toggle.hidden = false;

        function setScreenKeyboard(enabled) {
            field.readOnly = !enabled;
            field.setAttribute("inputmode", enabled ? (field.type === "search" ? "search" : "text") : "none");
            toggle.classList.toggle("active", enabled);
            toggle.setAttribute("aria-pressed", String(enabled));
            toggle.setAttribute("aria-label", enabled ? "Use physical keyboard" : "Use on-screen keyboard");
            toggle.setAttribute("title", enabled ? "Use physical keyboard" : "Use on-screen keyboard");
            if (enabled) { window.setTimeout(function () { field.focus(); }, 0); }
            else { toggle.focus(); }
        }

        toggle.addEventListener("click", function () {
            setScreenKeyboard(toggle.getAttribute("aria-pressed") !== "true");
        });

        field.addEventListener("keydown", function (event) {
            if (event.ctrlKey || event.altKey || event.metaKey || !field.readOnly) { return; }
            var before = field.value;
            var start = Number.isInteger(field.selectionStart) ? field.selectionStart : before.length;
            var end = Number.isInteger(field.selectionEnd) ? field.selectionEnd : start;
            var key = event.key && event.key.length === 1 ? event.key
                : ((event.key === "Backspace" || event.key === "Delete")
                    ? event.key : keyboardCharacter(event));
            if (!(key.length === 1 || key === "Backspace" || key === "Delete")) { return; }
            event.preventDefault();
            event.stopPropagation();
            if (key.length === 1) { replaceSelection(field, key, start, end); }
            else if (key === "Backspace" && (start !== end || start > 0)) {
                replaceSelection(field, "", start !== end ? start : start - 1, end);
            } else if (key === "Delete" && (start !== end || end < before.length)) {
                replaceSelection(field, "", start, start !== end ? end : end + 1);
            }
        }, true);

        document.addEventListener("keydown", function (event) {
            if ((event.key === "Escape" || event.keyCode === 10009)
                    && toggle.getAttribute("aria-pressed") === "true") {
                event.preventDefault();
                event.stopImmediatePropagation();
                setScreenKeyboard(false);
            }
        }, true);
    }

    function installLoginNavigation(form) {
        form.addEventListener("keydown", function (event) {
            if (event.key !== "ArrowDown" && event.key !== "ArrowUp" && event.key !== "Enter") { return; }
            var controls = Array.prototype.slice.call(form.querySelectorAll("input,button"))
                .filter(function (control) {
                    return !control.disabled && !control.hidden && control.type !== "hidden";
                });
            var current = document.activeElement;
            if (event.key === "Enter") {
                if (current && current.tagName === "BUTTON") {
                    event.preventDefault();
                    current.click();
                } else if (current && current.tagName === "INPUT") {
                    event.preventDefault();
                    if (typeof form.requestSubmit === "function") { form.requestSubmit(); }
                    else { form.submit(); }
                }
                return;
            }
            var index = controls.indexOf(current);
            if (index < 0 || (current && !current.readOnly && current.tagName === "INPUT")) { return; }
            var next = event.key === "ArrowDown" ? index + 1 : index - 1;
            if (next < 0 || next >= controls.length) { return; }
            event.preventDefault();
            controls[next].focus();
        });
    }

    document.addEventListener("DOMContentLoaded", function () {
        if (!isTizen()) { return; }
        document.body.classList.add("tizen-tv", "tv-keyboard-ready");
        var fields = document.querySelectorAll("[data-tv-input]");
        Array.prototype.forEach.call(fields, function (field) {
            var toggle = document.querySelector("[data-tv-input-toggle='" + field.id + "']");
            if (toggle) { install(field, toggle); }
        });
        var forms = document.querySelectorAll("[data-tv-login-form]");
        Array.prototype.forEach.call(forms, installLoginNavigation);
    });

    window.FluxaTvInput = { isTizen: isTizen };
}());
