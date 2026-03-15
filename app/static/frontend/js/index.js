(function () {
  "use strict";

  var currentLanguage = "ar";
  var LAST_PHONE_STORAGE_KEY = "fastapi-base-last-phone-number";
  var translations = {
    ar: {
      dir: "rtl",
      lang: "ar",
      phoneLabel: "رقم الهاتف",
      passwordLabel: "كلمة السر",
      forgot: "نسيت كلمة السر",
      signIn: "تسجيل الدخول",
      selfRegistration: "التسجيل الذاتي",
      firstTimeLogin: "تسجيل الدخول لأول مرة",
      faceId: "بصمة الوجه",
      contact: "تواصل معنا",
      toggleButton: "English",
      showPassword: "إظهار كلمة السر",
      hidePassword: "إخفاء كلمة السر",
      defaultPhoneHint: "الرجاء إدخال رقم الهاتف الذي يبدأ بـ 077 أو 078 أو 079.",
      invalidPhoneHint: "يرجى ادخال رقم الهاتف بشكل صحيح",
      invalidPasswordHint: "يرجى أدخال كلمة السر بشكل صحيح",
      emptyPasswordHint: "يرجى أدخال كلمة السر بشكل صحيح"
    },
    en: {
      dir: "ltr",
      lang: "en",
      phoneLabel: "Phone number",
      passwordLabel: "Password",
      forgot: "Forgot password",
      signIn: "Sign in",
      selfRegistration: "Self registration",
      firstTimeLogin: "First-time login",
      faceId: "Face ID",
      contact: "Contact us",
      toggleButton: "العربية",
      showPassword: "Show password",
      hidePassword: "Hide password",
      defaultPhoneHint: "Please enter a phone number starting with 077, 078, or 079.",
      invalidPhoneHint: "Please enter a valid phone number.",
      invalidPasswordHint: "Please enter a valid password.",
      emptyPasswordHint: "Please enter a valid password."
    }
  };

  function getLocale() {
    return translations[currentLanguage];
  }

  function normalizePhoneDigits(value) {
    return value
      .replace(/[\u0660-\u0669]/g, function (char) {
        return String(char.charCodeAt(0) - 1632);
      })
      .replace(/[\u06F0-\u06F9]/g, function (char) {
        return String(char.charCodeAt(0) - 1776);
      })
      .replace(/[^\d]/g, "");
  }

  function isPotentialPhonePrefix(value) {
    return /^(|0|07|077\d{0,7}|078\d{0,7}|079\d{0,7}|7|77\d{0,7}|78\d{0,7}|79\d{0,7})$/.test(value);
  }

  function clampPhoneLength(value) {
    if (/^(77|78|79)\d*$/.test(value)) {
      return value.slice(0, 9);
    }
    return value.slice(0, 10);
  }

  function normalizeJordanMobile(value) {
    var normalized = clampPhoneLength(normalizePhoneDigits(value || ""));
    if (/^(77|78|79)\d{7}$/.test(normalized)) {
      normalized = "0" + normalized;
    }
    return normalized;
  }

  function init() {
    var body = document.body;
    var fields = document.querySelectorAll(".field");
    var phoneInput = document.getElementById("phone-input");
    var phoneHint = document.getElementById("phone-hint");
    var passwordInput = document.getElementById("password-input");
    var passwordToggle = document.getElementById("password-toggle");
    var passwordHint = document.getElementById("password-hint");
    var languageToggle = document.getElementById("language-toggle");
    var eyeOpenSrc = body.dataset.eyeOpenSrc || "";
    var eyeClosedSrc = body.dataset.eyeClosedSrc || "";

    if (!phoneInput || !phoneHint || !passwordInput || !passwordHint || !languageToggle) {
      return;
    }

    function setPhoneHint(message, isError) {
      phoneHint.textContent = message;
      phoneHint.classList.toggle("error", Boolean(isError));
    }

    function setPhoneValidity(message) {
      phoneInput.setCustomValidity(message || "");
    }

    function setPasswordHint(message, isError) {
      passwordHint.textContent = message;
      passwordHint.classList.toggle("is-hidden", !message);
      passwordHint.classList.toggle("error", Boolean(isError));
    }

    function setPasswordValidity(message) {
      passwordInput.setCustomValidity(message || "");
    }

    function updatePasswordToggleAlt() {
      if (!passwordToggle) {
        return;
      }
      var locale = getLocale();
      var isHidden = passwordInput.type === "password";
      passwordToggle.alt = isHidden ? locale.showPassword : locale.hidePassword;
    }

    function applyLocalization(language) {
      var locale = translations[language];
      var phoneHasError = phoneHint.classList.contains("error");
      var passwordHasError = passwordHint.classList.contains("error");

      currentLanguage = language;
      document.documentElement.lang = locale.lang;
      document.documentElement.dir = locale.dir;

      document.querySelectorAll("[data-i18n]").forEach(function (element) {
        var key = element.getAttribute("data-i18n");
        if (locale[key]) {
          element.textContent = locale[key];
        }
      });

      languageToggle.textContent = locale.toggleButton;
      updatePasswordToggleAlt();

      if (phoneInput.value === "") {
        setPhoneHint(locale.defaultPhoneHint, false);
      } else if (phoneHasError) {
        if (phoneInput.value.length < 10) {
          setPhoneHint(locale.invalidPhoneHint, true);
        } else {
          setPhoneHint(locale.defaultPhoneHint, true);
        }
      } else {
        setPhoneHint(locale.defaultPhoneHint, false);
      }

      if (passwordHasError) {
        setPasswordHint(locale.invalidPasswordHint, true);
      }
    }

    function validatePhoneOnInput() {
      var locale = getLocale();
      phoneInput.value = clampPhoneLength(normalizePhoneDigits(phoneInput.value));

      if (phoneInput.value === "") {
        setPhoneHint(locale.defaultPhoneHint, false);
        setPhoneValidity("");
        return;
      }

      if (!isPotentialPhonePrefix(phoneInput.value)) {
        setPhoneHint(locale.defaultPhoneHint, true);
        setPhoneValidity(locale.defaultPhoneHint);
        return;
      }

      setPhoneHint(locale.defaultPhoneHint, false);
      setPhoneValidity("");
    }

    function validatePhoneOnBlur() {
      var locale = getLocale();
      phoneInput.value = normalizeJordanMobile(phoneInput.value);

      if (phoneInput.value === "") {
        setPhoneHint(locale.defaultPhoneHint, false);
        setPhoneValidity(locale.defaultPhoneHint);
        return;
      }

      if (phoneInput.value.length < 10) {
        setPhoneHint(locale.invalidPhoneHint, true);
        setPhoneValidity(locale.invalidPhoneHint);
        return;
      }

      if (!/^07[789]\d{7}$/.test(phoneInput.value)) {
        setPhoneHint(locale.defaultPhoneHint, true);
        setPhoneValidity(locale.defaultPhoneHint);
        return;
      }

      setPhoneHint(locale.defaultPhoneHint, false);
      setPhoneValidity("");
    }

    function persistPhoneForVerification() {
      var normalizedPhone = normalizeJordanMobile(phoneInput.value);
      if (!/^07[789]\d{7}$/.test(normalizedPhone)) {
        return;
      }
      try {
        localStorage.setItem(LAST_PHONE_STORAGE_KEY, normalizedPhone);
      } catch (error) {
      }
    }

    function validatePasswordOnBlur() {
      var locale = getLocale();

      if (passwordInput.value.length === 0) {
        setPasswordHint("", false);
        setPasswordValidity(locale.emptyPasswordHint);
        return;
      }

      if (passwordInput.value.length < 6) {
        setPasswordHint(locale.invalidPasswordHint, true);
        setPasswordValidity(locale.invalidPasswordHint);
        return;
      }

      setPasswordHint("", false);
      setPasswordValidity("");
    }

    fields.forEach(function (field) {
      var input = field.querySelector(".field-input");
      if (!input) {
        return;
      }

      function syncFieldState() {
        field.classList.toggle("has-value", input.value.trim() !== "");
      }

      input.addEventListener("focus", function () {
        field.classList.add("is-active");
      });

      input.addEventListener("blur", function () {
        field.classList.remove("is-active");
        syncFieldState();
      });

      input.addEventListener("input", function () {
        if (input === phoneInput) {
          validatePhoneOnInput();
        }
        syncFieldState();
      });

      syncFieldState();
    });

    phoneInput.addEventListener("focus", function () {
      setPhoneHint(getLocale().defaultPhoneHint, false);
      setPhoneValidity("");
    });

    phoneInput.addEventListener("blur", function () {
      validatePhoneOnBlur();
    });

    passwordInput.addEventListener("focus", function () {
      setPasswordHint("", false);
      setPasswordValidity("");
    });

    passwordInput.addEventListener("blur", function () {
      validatePasswordOnBlur();
    });

    window.validateLoginForm = function () {
      validatePhoneOnBlur();
      validatePasswordOnBlur();
      if (phoneInput.checkValidity() && passwordInput.checkValidity()) {
        persistPhoneForVerification();
        return true;
      }
      return false;
    };

    if (passwordToggle) {
      passwordToggle.addEventListener("click", function () {
        var isHidden = passwordInput.type === "password";
        passwordInput.type = isHidden ? "text" : "password";
        passwordInput.classList.toggle("is-masked", !passwordInput.classList.contains("is-masked"));
        if (eyeOpenSrc && eyeClosedSrc) {
          passwordToggle.src = isHidden ? eyeOpenSrc : eyeClosedSrc;
        }
        updatePasswordToggleAlt();
      });
    }

    languageToggle.addEventListener("click", function () {
      applyLocalization(currentLanguage === "ar" ? "en" : "ar");
    });

    applyLocalization(currentLanguage);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
