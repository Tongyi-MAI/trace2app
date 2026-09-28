"use strict";

(function (root) {
  var SESSION_SID_KEY = "ghostweb.state_kernel.sid";
  var CLIENT_INSTANCE_KEY = "ghostweb.state_kernel.client_instance";

  function createClientInstanceId() {
    try {
      if (root.crypto && typeof root.crypto.randomUUID === "function") {
        return root.crypto.randomUUID();
      }
      if (root.crypto && typeof root.crypto.getRandomValues === "function") {
        var bytes = new Uint32Array(4);
        root.crypto.getRandomValues(bytes);
        return Array.prototype.map.call(bytes, function (value) {
          return value.toString(16).padStart(8, "0");
        }).join("");
      }
    } catch (_error) {}
    return [Date.now().toString(36), Math.random().toString(36).slice(2)].join("-");
  }

  function clientInstanceFromRuntimeContext() {
    try {
      var existing = root.sessionStorage.getItem(CLIENT_INSTANCE_KEY) || "";
      if (existing) return existing;
      var created = createClientInstanceId();
      root.sessionStorage.setItem(CLIENT_INSTANCE_KEY, created);
      return created;
    } catch (_error) {
      return createClientInstanceId();
    }
  }

  function request(method, path, body) {
    if (typeof XMLHttpRequest === "undefined") {
      throw new Error("Environment State Kernel requires an HTTP-hosted browser runtime");
    }
    var xhr = new XMLHttpRequest();
    xhr.open(method, path, false);
    xhr.setRequestHeader("Content-Type", "application/json");
    xhr.send(body === undefined ? null : JSON.stringify(body));
    var payload = {};
    try { payload = xhr.responseText ? JSON.parse(xhr.responseText) : {}; } catch (_error) {}
    if (xhr.status < 200 || xhr.status >= 300) {
      var detail = payload && payload.error ? payload.error : { message: "HTTP " + xhr.status };
      var error = new Error(detail.message || "Environment State Kernel request failed");
      error.code = detail.code || "state_kernel_request_failed";
      error.status = xhr.status;
      throw error;
    }
    return payload;
  }

  function sidFromRuntimeContext() {
    var locationSid = "";
    try {
      locationSid = new URL(root.location.href).searchParams.get("sid") || "";
    } catch (_error) {}
    if (locationSid) {
      try { root.sessionStorage.setItem(SESSION_SID_KEY, locationSid); } catch (_error) {}
      return locationSid;
    }
    try { return root.sessionStorage.getItem(SESSION_SID_KEY) || ""; } catch (_error) {}
    return "";
  }

  function EnvironmentStateClient() {
    var bootstrap = request("POST", "/api/v1/bootstrap", { sid: sidFromRuntimeContext() || undefined });
    this.sid = bootstrap.sid;
    this.bootstrapState = bootstrap.state || {};
    this.clientInstanceId = clientInstanceFromRuntimeContext();
    try { root.sessionStorage.setItem(SESSION_SID_KEY, this.sid); } catch (_error) {}
    this.stateVersion = bootstrap.state_version;
    this.sequenceKey = [
      "ghostweb.state_kernel.sequence",
      this.sid,
      this.stateVersion.episode_epoch
    ].join(":");
    this.sequence = 0;
    try {
      this.sequence = Number(root.sessionStorage.getItem(this.sequenceKey) || 0) || 0;
    } catch (_error) {}
  }

  EnvironmentStateClient.prototype._nextKey = function (label) {
    this.sequence += 1;
    try { root.sessionStorage.setItem(this.sequenceKey, String(this.sequence)); } catch (_error) {}
    return [
      this.sid,
      this.stateVersion.episode_epoch,
      this.clientInstanceId,
      label,
      this.sequence
    ].join(":");
  };

  EnvironmentStateClient.prototype._refreshStateVersion = function () {
    var bootstrap = request("POST", "/api/v1/bootstrap", { sid: this.sid });
    this.sid = bootstrap.sid;
    this.bootstrapState = bootstrap.state || {};
    try { root.sessionStorage.setItem(SESSION_SID_KEY, this.sid); } catch (_error) {}
    var previousEpoch = this.stateVersion && this.stateVersion.episode_epoch;
    this.stateVersion = bootstrap.state_version;
    if (previousEpoch !== this.stateVersion.episode_epoch) {
      this.sequenceKey = [
        "ghostweb.state_kernel.sequence",
        this.sid,
        this.stateVersion.episode_epoch,
        this.clientInstanceId
      ].join(":");
      this.sequence = 0;
      try {
        this.sequence = Number(root.sessionStorage.getItem(this.sequenceKey) || 0) || 0;
      } catch (_error) {}
    }
  };

  EnvironmentStateClient.prototype._executeWithRevisionRetry = function (transactionId, payload, idempotencyKey) {
    var lastError = null;
    for (var attempt = 0; attempt < 2; attempt += 1) {
      try {
        var result = request(
          "POST",
          "/api/v1/sessions/" + encodeURIComponent(this.sid) + "/transactions/" + encodeURIComponent(transactionId),
          {
            expected_revision: this.stateVersion.revision,
            idempotency_key: idempotencyKey,
            payload: payload || {}
          }
        );
        this.stateVersion = result.state_version;
        return result;
      } catch (error) {
        lastError = error;
        if (attempt !== 0 || !error || error.status !== 409 || error.code !== "conflict") throw error;
        this._refreshStateVersion();
      }
    }
    throw lastError;
  };

  EnvironmentStateClient.prototype.readStorage = function (storageKey, defaultValue) {
    var result = request(
      "GET",
      "/api/v1/sessions/" + encodeURIComponent(this.sid) + "/storage/" + encodeURIComponent(storageKey)
    );
    this.stateVersion = result.state_version;
    return result.value === undefined || result.value === null ? defaultValue : result.value;
  };

  EnvironmentStateClient.prototype.replaceStorage = function (storageKey, value) {
    return this._executeWithRevisionRetry(
      "compat::storage_replace",
      { storage_key: storageKey, value: value },
      this._nextKey("storage_replace")
    );
  };

  EnvironmentStateClient.prototype.execute = function (transactionId, payload) {
    return this._executeWithRevisionRetry(
      transactionId,
      payload || {},
      this._nextKey(transactionId)
    );
  };

  EnvironmentStateClient.prototype.reset = function () {
    var result = request(
      "POST",
      "/internal/v1/sessions/" + encodeURIComponent(this.sid) + "/:reset",
      {}
    );
    this.stateVersion = result.state_version;
    this.bootstrapState = result.state || {};
    this.sequenceKey = [
      "ghostweb.state_kernel.sequence",
      this.sid,
      this.stateVersion.episode_epoch,
      this.clientInstanceId
    ].join(":");
    this.sequence = 0;
    try { root.sessionStorage.setItem(this.sequenceKey, "0"); } catch (_error) {}
    return result;
  };

  EnvironmentStateClient.prototype.snapshot = function (kind) {
    return request(
      "POST",
      "/internal/v1/sessions/" + encodeURIComponent(this.sid) + "/:snapshot",
      { kind: kind || "browser_probe" }
    );
  };

  root.GhostwebEnvironmentStateClient = EnvironmentStateClient;
  root.GhostwebEnvironmentState = root.GhostwebEnvironmentState || new EnvironmentStateClient();
})(typeof window !== "undefined" ? window : globalThis);
