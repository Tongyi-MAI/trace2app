
import json
import asyncio
import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import List, Dict, Any, Tuple
from trace2world_pipeline.core.llm_caller import call_openai_api_json_async
import threading
from trace2world_pipeline.runtime_backend.runtime_contract import build_runtime_contract


class Trace2WorldSdkGenerator:

    SDK_METHOD_CONTEXT_BUDGET = 48000
    SDK_METHOD_MAX_INTERFACES_PER_SHARD = 30

    def __init__(self, logger=None, model=None, reasoning_effort="medium", max_concurrent=None):
        self.logger = logger
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.max_concurrent = max_concurrent
        self._sdk_shard_attempt_lock = threading.Lock()
        self._sdk_shard_attempt_counts = {}
        self._sdk_shard_latest_attempts = {}

    async def generate_parallel(self, tasks: List[Dict[str, Any]],
                                entity_schema: Dict[str, Any],
                                interfaces: Dict[str, Any],
                                website_type: str,
                                generated_data: Dict[str, Any],
                                task_interaction_contracts: Dict[str, Any] = None) -> Tuple[str, str]:
        if self.logger:
            self.logger.start_stage("Parallel Generation", "backend")
            self.logger.log_info("Starting parallel generation of implementation and tests...")
            self.logger.log_info(f"Using pre-generated data for test consistency ({len(generated_data)} entity types)")

        try:

            impl_task = self._generate_implementation(
                entity_schema,
                interfaces,
                website_type,
                init_data=generated_data,
                task_interaction_contracts=task_interaction_contracts,
            )
            test_task = self._generate_tests(tasks, entity_schema, interfaces, website_type, generated_data)


            results = await asyncio.gather(impl_task, test_task)
        except Exception as e:
            import traceback
            if self.logger:
                self.logger.log_error(f"Error in parallel generation: {str(e)}")
                self.logger.log_error(f"Stack trace:\n{traceback.format_exc()}")
            raise

        implementation = results[0]
        tests = results[1]

        if self.logger:
            self.logger.log_info("Successfully generated implementation and tests in parallel")
            self.logger.end_stage("Parallel Generation")

        return implementation, tests

    async def _generate_implementation(self,
                                       entity_schema: Dict[str, Any],
                                       interfaces: Dict[str, Any],
                                       website_type: str,
                                       init_data: Dict[str, Any] = None,
                                       trace_context: str = None,
                                       task_interaction_contracts: Dict[str, Any] = None) -> str:
        bundle = await self._generate_implementation_bundle(
            entity_schema, interfaces, website_type,
            init_data=init_data, trace_context=trace_context,
            task_interaction_contracts=task_interaction_contracts,
        )
        return bundle["monolithic"]

    async def _generate_implementation_bundle(self,
                                              entity_schema: Dict[str, Any],
                                              interfaces: Dict[str, Any],
                                              website_type: str,
                                              init_data: Dict[str, Any] = None,
                                              trace_context: str = None,
                                              task_interaction_contracts: Dict[str, Any] = None,
                                              state_kernel_enabled: bool = False) -> Dict[str, str]:
        total_interfaces = sum(
            len(iface_list) for iface_list in interfaces.values()
            if isinstance(iface_list, list)
        )
        if self.logger:
            self.logger.log_info(f"Generating SDK ({total_interfaces} interfaces)")

        methods = await self._generate_method_bodies(entity_schema, interfaces, website_type, init_data=init_data, trace_context=trace_context)

        runtime_contract = build_runtime_contract(
            entity_schema,
            interfaces,
            task_interaction_contracts=task_interaction_contracts,
        )
        deterministic_aliases = self._build_deterministic_interface_alias_methods(
            interfaces,
            runtime_contract,
        )
        if deterministic_aliases:
            methods = (methods or "") + "\n\n" + deterministic_aliases

        return self._build_sdk_bundle(
            methods,
            init_data=init_data or {},
            entity_schema=entity_schema,
            runtime_contract=runtime_contract,
            state_kernel_enabled=state_kernel_enabled,
        )

    def _build_sdk_template(self, methods: str, init_data: Dict[str, Any] = None,
                            entity_schema: Dict[str, Any] = None,
                            runtime_contract: Dict[str, Any] = None) -> str:

        storage_key_map = {}
        if entity_schema:
            storage_key_map = self._extract_storage_key_map(entity_schema)


        id_counter = 0
        seed_payload = {}
        seed_storage_keys = []
        init_lines = []
        if init_data:
            for key, data in init_data.items():
                if key.startswith("_"):
                    continue

                storage_key = storage_key_map.get(key, key)
                seed_storage_keys.append(storage_key)

                if isinstance(data, list):
                    data = self._dedupe_seed_records(storage_key, data)
                    for i, item in enumerate(data):
                        if isinstance(item, dict) and 'id' not in item:
                            item['id'] = f"{storage_key}_{i}"
                            id_counter += 1
                seed_payload[storage_key] = data
                escaped = json.dumps(json.dumps(data, ensure_ascii=False))
                init_lines.append(f'    if (!localStorage.getItem("{storage_key}")) {{\n      localStorage.setItem("{storage_key}", {escaped});\n    }}')

        seed_version_input = {
            "seed": seed_payload,
            "runtime_contract": runtime_contract or {},
        }
        seed_version = hashlib.sha256(
            json.dumps(seed_version_input, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()[:16]
        seed_storage_keys_json = json.dumps(seed_storage_keys, ensure_ascii=False)
        reset_lines = [
            f'    const __ghostwebSeedVersion = "{seed_version}";',
            '    if (localStorage.getItem("__ghostweb_seed_version") !== __ghostwebSeedVersion) {',
            f'      [{", ".join(json.dumps(k, ensure_ascii=False) for k in seed_storage_keys)}].forEach(function (key) {{ localStorage.removeItem(key); }});',
            '      localStorage.removeItem("idCounter");',
            '      localStorage.setItem("__ghostweb_seed_version", __ghostwebSeedVersion);',
            '    }',
            '    if (!localStorage.getItem("idCounter")) {\n      localStorage.setItem("idCounter", "1000");\n    }',
        ]
        if not seed_storage_keys:
            reset_lines[2] = f'      {seed_storage_keys_json}.forEach(function (key) {{ localStorage.removeItem(key); }});'
        init_lines = reset_lines + init_lines

        init_body = "\n".join(init_lines)

        header = '''"use strict";

// localStorage polyfill for Node.js and environments without localStorage
const localStorage = (function () {
  try {
    if (typeof globalThis !== "undefined" && globalThis.localStorage) {
      return globalThis.localStorage;
    }
  } catch (e) {}
  let store = {};
  return {
    getItem: function (key) {
      return Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null;
    },
    setItem: function (key, value) {
      store[key] = String(value);
    },
    removeItem: function (key) {
      delete store[key];
    },
    clear: function () {
      store = {};
    },
    key: function (index) {
      return Object.keys(store)[index] || null;
    },
    get length() {
      return Object.keys(store).length;
    }
  };
})();

class BusinessLogic {
  constructor() {
    this._initStorage();
    this.idCounter = this._getNextIdCounter();
    this._installValidationResultAliases();
  }

  _initStorage() {
''' + init_body + '''
  }

  _getFromStorage(key, defaultValue) {
    const data = localStorage.getItem(key);
    if (data === null || data === undefined) {
      return defaultValue !== undefined ? defaultValue : [];
    }
    try {
      return JSON.parse(data);
    } catch (e) {
      return defaultValue !== undefined ? defaultValue : [];
    }
  }

  _saveToStorage(key, data) {
    localStorage.setItem(key, JSON.stringify(data));
  }

  navigateRoute(targetFilename, metadata) {
    const event = {
      target_filename: String(targetFilename || ""),
      metadata: this._interfacePayloadFromObject(metadata),
      at: Date.now()
    };
    const events = this._getFromStorage("__ghostweb_route_events", []);
    events.push(event);
    this._saveToStorage("__ghostweb_route_events", events.slice(-128));
    return false;
  }

  getRouteEvents() {
    return this._getFromStorage("__ghostweb_route_events", []);
  }

  _getNextIdCounter() {
    const current = parseInt(localStorage.getItem("idCounter") || "1000", 10);
    const next = current + 1;
    localStorage.setItem("idCounter", String(next));
    return next;
  }

  _generateId(prefix) {
    return prefix + "_" + this._getNextIdCounter();
  }

  _isPlainObject(value) {
    return Boolean(value && typeof value === "object" && !Array.isArray(value));
  }

  _interfacePayloadFromObject(value) {
    return this._isPlainObject(value) ? Object.assign({}, value) : {};
  }

  _interfaceOptionsFromObject(value, fallback) {
    const opts = typeof fallback === "boolean" ? { setAsDefault: fallback } : Object.assign({}, fallback || {});
    if (!this._isPlainObject(value)) return opts;
    if (value.setAsDefault !== undefined) opts.setAsDefault = value.setAsDefault;
    if (value.isDefault !== undefined && opts.setAsDefault === undefined) opts.setAsDefault = value.isDefault;
    if (value.makeDefault !== undefined && opts.setAsDefault === undefined) opts.setAsDefault = value.makeDefault;
    return opts;
  }

  _interfaceIdentifierFromObject(value, fallback) {
    if (!this._isPlainObject(value)) return fallback;
    return value.id || value.identifier || value.recordId || value.record_id || value.itemId || value.item_id || value.entityId || value.entity_id || fallback;
  }

  _findByIdOrName(collection, identifier) {
    if (!collection || !Array.isArray(collection)) return null;
    const index = this._findEntityIndex(collection, identifier, this._inferStorageKeyFromRecords(collection));
    return index >= 0 ? collection[index] : null;
  }

  _installValidationResultAliases() {
    const proto = Object.getPrototypeOf(this);
    if (!proto || proto.__gwValidationAliasesInstalled) return;
    Object.defineProperty(proto, "__gwValidationAliasesInstalled", {
      value: true,
      enumerable: false,
      configurable: true
    });
    Object.getOwnPropertyNames(proto).forEach((name) => {
      if (!/^validate[A-Z_]/.test(name)) return;
      if (name === "constructor") return;
      const original = proto[name];
      if (typeof original !== "function" || original.__gwValidationWrapped) return;
      const wrapped = function () {
        const result = original.apply(this, arguments);
        if (result && typeof result.then === "function") {
          return result.then((value) => this._withValidationAliases(value));
        }
        return this._withValidationAliases(result);
      };
      Object.defineProperty(wrapped, "__gwValidationWrapped", {
        value: true,
        enumerable: false
      });
      proto[name] = wrapped;
    });
  }

  _withValidationAliases(result) {
    if (!result || typeof result !== "object" || Array.isArray(result)) return result;
    const hasIsValid = Object.prototype.hasOwnProperty.call(result, "isValid");
    const hasValid = Object.prototype.hasOwnProperty.call(result, "valid");
    const hasOk = Object.prototype.hasOwnProperty.call(result, "ok");
    if (!hasIsValid && !hasValid && !hasOk) return result;
    const value = Boolean(hasIsValid ? result.isValid : (hasValid ? result.valid : result.ok));
    if (!hasIsValid) result.isValid = value;
    if (!hasValid) result.valid = value;
    if (!hasOk) result.ok = value;
    return result;
  }

  _normalizeString(value) {
    return (value || "").toString().trim().toLowerCase();
  }

'''

        footer = '''}

if (typeof module !== 'undefined' && module.exports) module.exports = BusinessLogic;
if (typeof window !== 'undefined') window.WebsiteSDK = new BusinessLogic();
'''


        cleaned = (methods or "").strip()
        if cleaned.startswith('class '):
            brace_start = cleaned.find('{')
            if brace_start > 0:
                cleaned = cleaned[brace_start + 1:]
                if cleaned.rstrip().endswith('}'):
                    cleaned = cleaned.rstrip()[:-1]
        lines = cleaned.split('\n')
        cleaned_lines = [l for l in lines if not l.strip().startswith('module.exports')
                        and not l.strip().startswith('if (typeof window')]
        cleaned = '\n'.join(cleaned_lines)

        deterministic_methods = self._build_deterministic_entity_methods(
            entity_schema or {},
            runtime_contract=runtime_contract,
        )
        compatibility_methods = self._build_passenger_form_compat_methods(
            cleaned,
            entity_schema=entity_schema or {},
            runtime_contract=runtime_contract or {},
        )


        return header + deterministic_methods + cleaned + '\n' + compatibility_methods + '\n' + footer

    def _has_passenger_form_compat_authority(
        self,
        existing_methods: str = "",
        entity_schema: Dict[str, Any] = None,
        runtime_contract: Dict[str, Any] = None,
    ) -> bool:
        existing = existing_methods or ""
        if re.search(
            r"\b(?:addPassenger|validatePassengerForm|updatePassengerFormField|submitPassengerForm)\s*\(",
            existing,
        ):
            return True
        authority_text = json.dumps(
            {
                "entity_schema": entity_schema or {},
                "runtime_contract": runtime_contract or {},
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).lower()
        return any(
            token in authority_text
            for token in (
                "passengerform",
                "passenger_form",
                "passengerprofile",
                "passenger_profile",
                '"passenger"',
                '"passengers"',
            )
        )

    def _build_passenger_form_compat_methods(
        self,
        existing_methods: str = "",
        *,
        entity_schema: Dict[str, Any] = None,
        runtime_contract: Dict[str, Any] = None,
    ) -> str:
        if not self._has_passenger_form_compat_authority(
            existing_methods,
            entity_schema=entity_schema,
            runtime_contract=runtime_contract,
        ):
            return ""
        existing = existing_methods or ""
        has_update = re.search(r"\bupdatePassengerFormField\s*\(", existing) is not None
        has_submit = re.search(r"\bsubmitPassengerForm\s*\(", existing) is not None
        if has_update and has_submit:
            return ""

        public_methods: List[str] = []
        if not has_update:
            public_methods.append('''
  updatePassengerFormField(formMode, passengerId, fieldName, value) {
    const draft = this._gwPassengerFormDraft(formMode, passengerId);
    const field = this._gwPassengerFormFieldName(fieldName);
    draft.fields[field] = value;
    draft.fields[fieldName] = value;
    draft.updatedAt = Date.now();
    this._saveToStorage(this._gwPassengerFormDraftKey(formMode, passengerId), draft);
    return { success: true, field, value, fields: Object.assign({}, draft.fields) };
  }
''')
        if not has_submit:
            public_methods.append('''
  submitPassengerForm(formMode, passengerId) {
    const mode = String(formMode || "add");
    const draft = this._gwPassengerFormDraft(mode, passengerId);
    const fields = draft.fields || {};
    const name = fields.name || "";
    const idType = fields.id_type || fields.idType || "中国居民身份证";
    const idNumber = fields.id_number || fields.idNumber || fields.certificateNumber || fields.certificate_number || "";
    const phone = fields.phone || fields.mobile || fields.mobile_number || "";
    const passengerType = fields.passenger_type || fields.passengerType || fields.discount_type || fields.discountType || "成人";
    let validation = { is_valid: true, isValid: true, valid: true };
    if (typeof this.validatePassengerForm === "function") {
      validation = this.validatePassengerForm(name, idType, idNumber, phone) || validation;
    }
    const valid = validation.isValid !== false && validation.is_valid !== false && validation.valid !== false && validation.ok !== false;
    if (!valid) return Object.assign({ success: false, message: "请正确填写乘车人信息" }, validation);
    if (mode === "edit" && passengerId) {
      const passengers = this._getFromStorage("passengers", []);
      const passenger = this._findByIdOrName(passengers, passengerId);
      if (passenger) {
        passenger.name = name || passenger.name;
        passenger.id_type = idType || passenger.id_type;
        passenger.id_number = idNumber || passenger.id_number;
        passenger.id_number_masked = this._gwMaskIdNumber(idNumber || passenger.id_number_masked || "");
        passenger.masked_id_number = passenger.id_number_masked;
        passenger.phone = phone || passenger.phone;
        passenger.mobile = phone || passenger.mobile;
        passenger.passenger_type = passengerType || passenger.passenger_type;
        passenger.ticket_type = passengerType || passenger.ticket_type;
        this._saveToStorage("passengers", passengers);
        return { success: true, message: "保存成功", passenger_id: passenger.id || passengerId, passenger };
      }
    }
    if (typeof this.addPassenger === "function") {
      return this.addPassenger(name, idType, idNumber, phone, passengerType, passengerType);
    }
    const passengers = this._getFromStorage("passengers", []);
    const passengerIdOut = this._generateId ? this._generateId("passengers") : ("passengers_" + String(passengers.length));
    const passenger = {
      id: passengerIdOut,
      name,
      id_type: idType,
      id_number: idNumber,
      id_number_masked: this._gwMaskIdNumber(idNumber),
      masked_id_number: this._gwMaskIdNumber(idNumber),
      phone,
      mobile: phone,
      passenger_type: passengerType,
      ticket_type: passengerType,
      item_ref: name,
      current_user: "否"
    };
    passengers.push(passenger);
    this._saveToStorage("passengers", passengers);
    return { success: true, message: "添加成功", passenger_id: passengerIdOut, passenger };
  }
''')

        return '''
  _gwPassengerFormDraftKey(formMode, passengerId) {
    return "__ghostweb_passenger_form_draft::" + String(formMode || "add") + "::" + String(passengerId || "new");
  }

  _gwPassengerFormFieldName(fieldName) {
    const key = String(fieldName || "");
    const aliases = {
      idNumber: "id_number",
      certificateNumber: "id_number",
      certificate_number: "id_number",
      mobile: "phone",
      phoneNumber: "phone",
      discountType: "passenger_type",
      discount_type: "passenger_type",
      passengerType: "passenger_type",
      idType: "id_type"
    };
    return aliases[key] || key;
  }

  _gwPassengerFormDraft(formMode, passengerId) {
    const key = this._gwPassengerFormDraftKey(formMode, passengerId);
    const stored = this._getFromStorage(key, {});
    const fields = Object.assign({}, stored && stored.fields ? stored.fields : {});
    if (passengerId && !Object.keys(fields).length) {
      const passengers = this._getFromStorage("passengers", []);
      const passenger = this._findByIdOrName(passengers, passengerId);
      if (passenger) {
        fields.name = passenger.name || "";
        fields.id_type = passenger.id_type || "中国居民身份证";
        fields.id_number = passenger.id_number || passenger.masked_id_number || passenger.id_number_masked || "";
        fields.phone = passenger.phone || passenger.mobile || "";
        fields.passenger_type = passenger.passenger_type || passenger.ticket_type || "成人";
      }
    }
    return Object.assign({}, stored || {}, { formMode: formMode || "add", passengerId: passengerId || null, fields });
  }

  _gwMaskIdNumber(value) {
    const text = String(value || "");
    if (text.length <= 7) return text;
    return text.slice(0, 4) + "**********" + text.slice(-3);
  }

''' + "\n".join(public_methods)

    def _build_sdk_bundle(self, methods: str, init_data: Dict[str, Any] = None,
                          entity_schema: Dict[str, Any] = None,
                          runtime_contract: Dict[str, Any] = None,
                          state_kernel_enabled: bool = False) -> Dict[str, str]:


        storage_key_map = {}
        if entity_schema:
            storage_key_map = self._extract_storage_key_map(entity_schema)

        id_counter = 0
        seed_payload = {}
        seed_storage_keys = []
        if init_data:
            for key, data in init_data.items():
                if key.startswith("_"):
                    continue
                storage_key = storage_key_map.get(key, key)
                seed_storage_keys.append(storage_key)
                if isinstance(data, list):
                    data = self._dedupe_seed_records(storage_key, data)
                    for i, item in enumerate(data):
                        if isinstance(item, dict) and 'id' not in item:
                            item['id'] = f"{storage_key}_{i}"
                            id_counter += 1
                seed_payload[storage_key] = data

        seed_version_input = {
            "seed": seed_payload,
            "runtime_contract": runtime_contract or {},
        }
        seed_version = hashlib.sha256(
            json.dumps(seed_version_input, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()[:16]


        seed_json = json.dumps(seed_payload, ensure_ascii=False, indent=None)
        seed_data_js = (
            '"use strict";\n'
            "(function (root) {\n"
            "  var data = " + seed_json + ";\n"
            '  var version = "' + seed_version + '";\n'
            '  if (typeof root !== "undefined") { root.GhostwebSeedData = data; root.GhostwebSeedVersion = version; }\n'
            '  if (typeof module !== "undefined" && module.exports) { module.exports = { GhostwebSeedData: data, GhostwebSeedVersion: version }; }\n'
            "})(typeof window !== 'undefined' ? window : typeof globalThis !== 'undefined' ? globalThis : this);\n"
        )


        localstorage_polyfill = '''const localStorage = (function () {
  try {
    if (typeof globalThis !== "undefined" && globalThis.localStorage) {
      return globalThis.localStorage;
    }
  } catch (e) {}
  let store = {};
  return {
    getItem: function (key) {
      return Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null;
    },
    setItem: function (key, value) {
      store[key] = String(value);
    },
    removeItem: function (key) {
      delete store[key];
    },
    clear: function () {
      store = {};
    },
    key: function (index) {
      return Object.keys(store)[index] || null;
    },
    get length() {
      return Object.keys(store).length;
    }
  };
})();
'''
        if state_kernel_enabled:
            localstorage_polyfill = '''const stateStorage = (function () {
  var client = (typeof globalThis !== "undefined" && globalThis.GhostwebEnvironmentState) || null;
  if (!client) throw new Error("Missing environment-state client; pages must be HTTP-hosted by the SQLite state service");
  return {
    getItem: function (key) {
      var value = client.readStorage(String(key), null);
      return value === null || value === undefined ? null : JSON.stringify(value);
    },
    setItem: function (key, value) {
      var parsed;
      try { parsed = JSON.parse(String(value)); } catch (_error) { parsed = String(value); }
      client.replaceStorage(String(key), parsed);
    },
    removeItem: function (key) { client.replaceStorage(String(key), null); },
    clear: function () { throw new Error("State Kernel forbids unscoped storage clear"); },
    key: function () { return null; },
    get length() { return 0; }
  };
})();
'''

        seed_storage_keys_literal = ", ".join(json.dumps(k, ensure_ascii=False) for k in seed_storage_keys)

        data_layer_header = '''"use strict";

''' + localstorage_polyfill + '''

class GhostwebDataLayer {
  constructor(seedData, seedVersion) {
    var _g = typeof globalThis !== "undefined" ? globalThis : (typeof window !== "undefined" ? window : {});
    this._seedData = seedData || _g.GhostwebSeedData || {};
    this._seedVersion = seedVersion || _g.GhostwebSeedVersion || "";
    this._initStorage();
    this.idCounter = this._getNextIdCounter();
    this._installValidationResultAliases();
  }

  _initStorage() {
    if (localStorage.getItem("__ghostweb_seed_version") === this._seedVersion) return;
    [''' + seed_storage_keys_literal + '''].forEach(function (key) { localStorage.removeItem(key); });
    localStorage.removeItem("idCounter");
    localStorage.setItem("__ghostweb_seed_version", this._seedVersion);
    if (!localStorage.getItem("idCounter")) {
      localStorage.setItem("idCounter", "1000");
    }
    const seed = this._seedData;
    Object.keys(seed).forEach(function (key) {
      if (!localStorage.getItem(key)) {
        localStorage.setItem(key, JSON.stringify(seed[key]));
      }
    });
  }

  _getFromStorage(key, defaultValue) {
    const data = localStorage.getItem(key);
    if (data === null || data === undefined) {
      return defaultValue !== undefined ? defaultValue : [];
    }
    try {
      return JSON.parse(data);
    } catch (e) {
      return defaultValue !== undefined ? defaultValue : [];
    }
  }

  _saveToStorage(key, data) {
    localStorage.setItem(key, JSON.stringify(data));
  }

  navigateRoute(targetFilename, metadata) {
    const event = {
      target_filename: String(targetFilename || ""),
      metadata: this._interfacePayloadFromObject(metadata),
      at: Date.now()
    };
    const events = this._getFromStorage("__ghostweb_route_events", []);
    events.push(event);
    this._saveToStorage("__ghostweb_route_events", events.slice(-128));
    return false;
  }

  getRouteEvents() {
    return this._getFromStorage("__ghostweb_route_events", []);
  }

  _getNextIdCounter() {
    const current = parseInt(localStorage.getItem("idCounter") || "1000", 10);
    const next = current + 1;
    localStorage.setItem("idCounter", String(next));
    return next;
  }

  _generateId(prefix) {
    return prefix + "_" + this._getNextIdCounter();
  }

  _isPlainObject(value) {
    return Boolean(value && typeof value === "object" && !Array.isArray(value));
  }

  _interfacePayloadFromObject(value) {
    return this._isPlainObject(value) ? Object.assign({}, value) : {};
  }

  _interfaceOptionsFromObject(value, fallback) {
    const opts = typeof fallback === "boolean" ? { setAsDefault: fallback } : Object.assign({}, fallback || {});
    if (!this._isPlainObject(value)) return opts;
    if (value.setAsDefault !== undefined) opts.setAsDefault = value.setAsDefault;
    if (value.isDefault !== undefined && opts.setAsDefault === undefined) opts.setAsDefault = value.isDefault;
    if (value.makeDefault !== undefined && opts.setAsDefault === undefined) opts.setAsDefault = value.makeDefault;
    return opts;
  }

  _interfaceIdentifierFromObject(value, fallback) {
    if (!this._isPlainObject(value)) return fallback;
    return value.id || value.identifier || value.recordId || value.record_id || value.itemId || value.item_id || value.entityId || value.entity_id || fallback;
  }

  _findByIdOrName(collection, identifier) {
    if (!collection || !Array.isArray(collection)) return null;
    const index = this._findEntityIndex(collection, identifier, this._inferStorageKeyFromRecords(collection));
    return index >= 0 ? collection[index] : null;
  }

  _installValidationResultAliases() {
    const proto = Object.getPrototypeOf(this);
    if (!proto || proto.__gwValidationAliasesInstalled) return;
    Object.defineProperty(proto, "__gwValidationAliasesInstalled", {
      value: true,
      enumerable: false,
      configurable: true
    });
    Object.getOwnPropertyNames(proto).forEach((name) => {
      if (!/^validate[A-Z_]/.test(name)) return;
      if (name === "constructor") return;
      const original = proto[name];
      if (typeof original !== "function" || original.__gwValidationWrapped) return;
      const wrapped = function () {
        const result = original.apply(this, arguments);
        if (result && typeof result.then === "function") {
          return result.then((value) => this._withValidationAliases(value));
        }
        return this._withValidationAliases(result);
      };
      Object.defineProperty(wrapped, "__gwValidationWrapped", {
        value: true,
        enumerable: false
      });
      proto[name] = wrapped;
    });
  }

  _withValidationAliases(result) {
    if (!result || typeof result !== "object" || Array.isArray(result)) return result;
    const hasIsValid = Object.prototype.hasOwnProperty.call(result, "isValid");
    const hasValid = Object.prototype.hasOwnProperty.call(result, "valid");
    const hasOk = Object.prototype.hasOwnProperty.call(result, "ok");
    if (!hasIsValid && !hasValid && !hasOk) return result;
    const value = Boolean(hasIsValid ? result.isValid : (hasValid ? result.valid : result.ok));
    if (!hasIsValid) result.isValid = value;
    if (!hasValid) result.valid = value;
    if (!hasOk) result.ok = value;
    return result;
  }

  _normalizeString(value) {
    return (value || "").toString().trim().toLowerCase();
  }

'''
        if state_kernel_enabled:
            data_layer_header = data_layer_header.replace("localStorage", "stateStorage")
            data_layer_header = re.sub(
                r"  _initStorage\(\) \{.*?\n  \}\n\n  _getFromStorage",
                "  _initStorage() {\n    // World seed is materialized in immutable world.sqlite by the pipeline.\n  }\n\n  _getFromStorage",
                data_layer_header,
                flags=re.S,
            )

        deterministic_methods = self._build_deterministic_entity_methods(
            entity_schema or {},
            runtime_contract=runtime_contract,
        )

        data_layer_footer = '''}

if (typeof module !== 'undefined' && module.exports) module.exports = GhostwebDataLayer;
if (typeof window !== 'undefined') window.GhostwebDataLayer = GhostwebDataLayer;
'''

        data_layer_js = data_layer_header + deterministic_methods + data_layer_footer


        cleaned = (methods or "").strip()
        if cleaned.startswith('class '):
            brace_start = cleaned.find('{')
            if brace_start > 0:
                cleaned = cleaned[brace_start + 1:]
                if cleaned.rstrip().endswith('}'):
                    cleaned = cleaned.rstrip()[:-1]
        cleaned_lines = [l for l in cleaned.split('\n')
                         if not l.strip().startswith('module.exports')
                         and not l.strip().startswith('if (typeof window')]
        cleaned = '\n'.join(cleaned_lines)

        bl_header = '''"use strict";

class WebsiteSDK extends GhostwebDataLayer {
  constructor() {
    var _g = typeof globalThis !== "undefined" ? globalThis : (typeof window !== "undefined" ? window : {});
    super(_g.GhostwebSeedData || {}, _g.GhostwebSeedVersion || "");
  }

'''

        bl_footer = '''}

if (typeof module !== 'undefined' && module.exports) module.exports = WebsiteSDK;
if (typeof window !== 'undefined') window.WebsiteSDK = new WebsiteSDK();
'''

        compatibility_methods = self._build_passenger_form_compat_methods(
            cleaned,
            entity_schema=entity_schema or {},
            runtime_contract=runtime_contract or {},
        )
        business_logic_js = bl_header + cleaned + '\n' + compatibility_methods + '\n' + bl_footer


        monolithic = self._build_sdk_template(
            methods, init_data=init_data,
            entity_schema=entity_schema,
            runtime_contract=runtime_contract,
        )

        return {
            "data_layer": data_layer_js,
            "seed_data": seed_data_js,
            "business_logic": business_logic_js,
            "monolithic": monolithic,
        }

    def _dedupe_seed_records(self, storage_key: str, records: List[Any]) -> List[Any]:
        result = []
        key_to_indices = {}
        for record in records or []:
            if not isinstance(record, dict):
                result.append(record)
                continue
            raw_record = dict(record)
            if not self._seed_record_has_meaningful_content(raw_record):
                continue
            dedupe_key = self._seed_record_dedupe_key(raw_record)
            if dedupe_key:
                for index in key_to_indices.get(dedupe_key, []):
                    if self._seed_records_compatible(result[index], raw_record):
                        result[index] = self._merge_seed_records(result[index], raw_record)
                        break
                else:
                    key_to_indices.setdefault(dedupe_key, []).append(len(result))
                    result.append(raw_record)
                continue
            result.append(raw_record)
        return result

    def _seed_record_has_meaningful_content(self, record: Dict[str, Any]) -> bool:
        if not isinstance(record, dict):
            return False
        for key, value in record.items():
            if str(key) == "id":
                continue
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            if isinstance(value, (list, dict)) and not value:
                continue
            return True
        return False

    def _seed_record_dedupe_key(self, record: Dict[str, Any]) -> str:
        if not isinstance(record, dict):
            return ""
        for key in ("id", "record_id", "entity_id", "item_id", "key", "slug"):
            value = record.get(key)
            if value not in (None, ""):
                return "{}:{}".format(key, str(value).strip())
        return ""

    def _seed_records_compatible(self, base: Dict[str, Any], incoming: Dict[str, Any]) -> bool:
        if not isinstance(base, dict) or not isinstance(incoming, dict):
            return False
        for key in set(base.keys()) & set(incoming.keys()):
            if key == "id":
                continue
            left = base.get(key)
            right = incoming.get(key)
            if left in (None, "") or right in (None, ""):
                continue
            if left == right:
                continue
            left_text = str(left).strip()
            right_text = str(right).strip()
            if left_text and right_text and (left_text in right_text or right_text in left_text):
                continue
            return False
        return True

    def _merge_seed_records(self, base: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(incoming or {})
        merged.update(base or {})
        for key, value in (incoming or {}).items():
            if merged.get(key) in (None, "") and value not in (None, ""):
                merged[key] = value
        return merged

    def _js_string(self, value: str) -> str:
        return json.dumps(value or "", ensure_ascii=False)

    def _camel_plural(self, name: str) -> str:
        if not name:
            return "Items"
        if name.endswith("y") and (len(name) < 2 or name[-2].lower() not in "aeiou"):
            return name[:-1] + "ies"
        if name.endswith(("s", "x", "z", "ch", "sh")):
            return name + "es"
        return name + "s"

    def _snake_case(self, name: str) -> str:
        import re
        value = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name or "item")
        value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
        value = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower()
        return value or "item"

    def _lower_camel(self, name: str) -> str:
        cleaned = "".join(ch for ch in (name or "item") if ch.isalnum()) or "Item"
        return cleaned[:1].lower() + cleaned[1:]

    def _build_deterministic_entity_methods(self,
                                            entity_schema: Dict[str, Any],
                                            runtime_contract: Dict[str, Any] = None) -> str:
        contract = runtime_contract or build_runtime_contract(entity_schema or {})
        entities = (contract or {}).get("entities", {})
        methods_by_entity = (contract or {}).get("methods_by_entity", {})
        if not entities:
            return ""

        lines = ["""
  _toSnakeCase(value) {
    return (value || "item")
      .toString()
      .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
      .replace(/[^A-Za-z0-9]+/g, "_")
      .replace(/^_+|_+$/g, "")
      .toLowerCase() || "item";
  }

  _toLowerCamelCase(value) {
    const snake = this._toSnakeCase(value);
    return snake.replace(/_([a-z0-9])/g, function (_, ch) { return ch.toUpperCase(); });
  }

  _listEntityRecords(storageKey) {
    const records = this._getFromStorage(storageKey, []);
    return Array.isArray(records) ? records.slice() : [];
  }

  _saveEntityRecords(storageKey, records) {
    this._saveToStorage(storageKey, Array.isArray(records) ? records : []);
  }

  _entityIdentifier(record) {
    if (!record || typeof record !== "object") return "";
    return record.id || record.record_id || record.entity_id || record.item_id || record.key || record.slug || "";
  }

  _matchesEntityIdentifier(record, identifier) {
    if (!record || identifier === null || identifier === undefined) return false;
    const target = String(identifier).trim();
    const candidates = [
      record.id,
      record.record_id,
      record.entity_id,
      record.item_id,
      record.key,
      record.slug
    ];
    return candidates.some(value => value !== null && value !== undefined && String(value) === target);
  }

  _singularizeIdentityStem(value) {
    const text = String(value || '').trim().toLowerCase();
    if (!text) return "";
    if (text.endsWith("ies") && text.length > 3) return text.slice(0, -3) + "y";
    if (text.endsWith("ses") && text.length > 3) return text.slice(0, -2);
    if (text.endsWith("s") && text.length > 1) return text.slice(0, -1);
    return text;
  }

  _identityAliasStems(storageKey) {
    const normalized = String(storageKey || '')
      .replace(/[^A-Za-z0-9_]+/g, '_')
      .replace(/^_+|_+$/g, '')
      .toLowerCase();
    if (!normalized) return [];
    const tokens = normalized.split('_').filter(Boolean);
    const singularTokens = tokens.map(token => this._singularizeIdentityStem(token));
    const candidates = [
      singularTokens.join('_'),
      tokens.length ? tokens[tokens.length - 1] : '',
      singularTokens.length ? singularTokens[singularTokens.length - 1] : ''
    ];
    return Array.from(new Set(candidates.filter(value => value && value !== normalized)));
  }

  _matchesOrdinalIdentityAlias(storageKey, ordinalIndex, identifier) {
    if (identifier === null || identifier === undefined) return false;
    const target = String(identifier).trim().toLowerCase();
    if (!target || !/[_-]\\d+$/.test(target)) return false;
    const numbers = [String(ordinalIndex), String(ordinalIndex + 1)];
    for (const stem of this._identityAliasStems(storageKey)) {
      for (const number of numbers) {
        if (target === stem + "_" + number || target === stem + "-" + number) return true;
      }
    }
    return false;
  }

  _inferStorageKeyFromRecords(records) {
    if (!Array.isArray(records)) return "";
    for (const record of records) {
      const id = record && record.id !== undefined && record.id !== null ? String(record.id) : "";
      const match = id.match(/^(.+?)[_-]\\d+$/);
      if (match && match[1]) return match[1];
    }
    return "";
  }

  _findEntityIndex(records, identifier, storageKey) {
    if (!Array.isArray(records)) return -1;
    const exactIndex = records.findIndex(record => this._matchesEntityIdentifier(record, identifier));
    if (exactIndex >= 0) return exactIndex;
    const key = storageKey || this._inferStorageKeyFromRecords(records);
    if (!key) return -1;
    return records.findIndex((record, index) => this._matchesOrdinalIdentityAlias(key, index, identifier));
  }

  _findEntityRecord(storageKey, identifier) {
    const records = this._dedupeEntityRecords(this._listEntityRecords(storageKey));
    if (identifier === null || identifier === undefined || identifier === "") {
      return records.length ? records[0] : null;
    }
    const index = this._findEntityIndex(records, identifier, storageKey);
    return index >= 0 ? records[index] : null;
  }

  _normalizeEntityRecord(entityName, payload, existing, fieldDefs) {
    const record = Object.assign({}, existing || {}, this._interfacePayloadFromObject(payload || {}));
    for (const key of Object.keys(record)) {
      const snakeKey = this._toSnakeCase(key);
      if (snakeKey && snakeKey !== key && record[key] !== undefined && record[key] !== null && record[key] !== "") {
        record[snakeKey] = record[key];
      }
    }
    if (!record.id) {
      record.id = this._generateId(this._toSnakeCase(entityName));
    }
    return record;
  }

  _entityDedupeKey(record) {
    if (!record || typeof record !== "object") return "";
    for (const key of ["id", "record_id", "entity_id", "item_id", "key", "slug"]) {
      const value = record[key];
      if (value !== null && value !== undefined && value !== "") return key + ":" + String(value);
    }
    return "";
  }

  _entityRecordHasMeaningfulContent(record) {
    if (!record || typeof record !== "object") return false;
    for (const key of Object.keys(record)) {
      if (key === "id") continue;
      const value = record[key];
      if (value === null || value === undefined) continue;
      if (typeof value === "string" && value.trim() === "") continue;
      if (Array.isArray(value) && value.length === 0) continue;
      if (typeof value === "object" && !Array.isArray(value) && Object.keys(value).length === 0) continue;
      return true;
    }
    return false;
  }

  _entityRecordsCompatible(base, incoming) {
    if (!base || !incoming || typeof base !== "object" || typeof incoming !== "object") return false;
    for (const key of Object.keys(base)) {
      if (key === "id" || !Object.prototype.hasOwnProperty.call(incoming, key)) continue;
      const left = base[key];
      const right = incoming[key];
      if (left === null || left === undefined || left === "" || right === null || right === undefined || right === "") continue;
      if (left === right) continue;
      const leftText = String(left).trim();
      const rightText = String(right).trim();
      if (leftText && rightText && (leftText.includes(rightText) || rightText.includes(leftText))) continue;
      return false;
    }
    return true;
  }

  _mergeEntityRecords(base, incoming) {
    const merged = Object.assign({}, incoming || {}, base || {});
    for (const key of Object.keys(incoming || {})) {
      if (merged[key] === "" || merged[key] === null || merged[key] === undefined) {
        merged[key] = incoming[key];
      }
    }
    return merged;
  }

  _dedupeEntityRecords(records) {
    const result = [];
    const keyToIndices = {};
    for (const record of records || []) {
      const normalized = this._normalizeEntityRecord("Entity", record || {});
      if (!this._entityRecordHasMeaningfulContent(normalized)) continue;
      const key = this._entityDedupeKey(normalized);
      if (key) {
        let merged = false;
        for (const index of keyToIndices[key] || []) {
          if (this._entityRecordsCompatible(result[index], normalized)) {
            result[index] = this._mergeEntityRecords(result[index], normalized);
            merged = true;
            break;
          }
        }
        if (merged) continue;
        if (!keyToIndices[key]) keyToIndices[key] = [];
        keyToIndices[key].push(result.length);
      }
      result.push(normalized);
    }
    return result;
  }

  _entityListResult(storageKey, collectionAliases, lowerPluralKey, fieldDefs) {
    const records = this._dedupeEntityRecords(this._listEntityRecords(storageKey));
    const defaultRecord = records.find(record => record.is_default === true || record.isDefault === true || record.is_default_badge === true) || null;
    const result = {
      items: records,
      records: records,
      defaultId: defaultRecord ? defaultRecord.id : ""
    };
    if (lowerPluralKey) result[lowerPluralKey] = records;
    for (const alias of collectionAliases || []) {
      if (alias) result[alias] = records;
    }
    return result;
  }

  _entityRecordResult(statusKey, record, recordAliases, lowerEntityKey, fieldDefs) {
    const item = Object.assign({}, record || {});
    const result = Object.assign({}, item, { record: item, item: item });
    if (statusKey) result[statusKey] = true;
    if (lowerEntityKey) result[lowerEntityKey] = item;
    for (const alias of recordAliases || []) {
      if (alias) result[alias] = item;
    }
    return result;
  }

  _clearDefaultFlags(records) {
    for (const record of records || []) {
      if (!record || typeof record !== "object") continue;
      if (Object.prototype.hasOwnProperty.call(record, "is_default")) record.is_default = false;
      if (Object.prototype.hasOwnProperty.call(record, "isDefault")) record.isDefault = false;
      if (Object.prototype.hasOwnProperty.call(record, "is_default_badge")) record.is_default_badge = false;
    }
  }

  _setEntityDefault(records, identifier, storageKey) {
    const index = this._findEntityIndex(records, identifier, storageKey);
    if (index < 0) return null;
    this._clearDefaultFlags(records);
    if (Object.prototype.hasOwnProperty.call(records[index], "is_default")) records[index].is_default = true;
    else if (Object.prototype.hasOwnProperty.call(records[index], "isDefault")) records[index].isDefault = true;
    else records[index].is_default_badge = true;
    return records[index];
  }

  _createEntityRecord(storageKey, entityName, payload, options, fieldDefs) {
    const records = this._listEntityRecords(storageKey);
    const record = this._normalizeEntityRecord(entityName, payload || {}, null, fieldDefs || []);
    const existingIndex = this._findEntityIndex(records, this._entityIdentifier(record));
    if (existingIndex >= 0) {
      records[existingIndex] = this._normalizeEntityRecord(entityName, record, records[existingIndex], fieldDefs || []);
      if (options && options.setAsDefault) this._setEntityDefault(records, records[existingIndex].id, storageKey);
      this._saveEntityRecords(storageKey, records);
      return records[existingIndex];
    }
    records.unshift(record);
    if (options && options.setAsDefault) this._setEntityDefault(records, record.id, storageKey);
    this._saveEntityRecords(storageKey, records);
    return record;
  }

  _updateEntityRecord(storageKey, entityName, identifier, patch, options, fieldDefs) {
    const records = this._listEntityRecords(storageKey);
    const index = this._findEntityIndex(records, identifier, storageKey);
    if (index < 0) {
      return this._createEntityRecord(storageKey, entityName, patch || {}, options, fieldDefs || []);
    }
    records[index] = this._normalizeEntityRecord(entityName, patch || {}, records[index], fieldDefs || []);
    if (options && options.setAsDefault) this._setEntityDefault(records, records[index].id, storageKey);
    this._saveEntityRecords(storageKey, records);
    return records[index];
  }

  _gwDraftStorageKey(storageKey) {
    return "__gw_drafts__" + storageKey;
  }

  _gwDraftIdFrom(value) {
    if (this._isPlainObject(value)) {
      return value.draftId || value.draft_id || value.id || value.identifier || "";
    }
    return String(value || "");
  }

  _gwFieldAliasPatch(fieldKey, fieldValue) {
    // Returns the field-value patch under both the raw key (camelCase or
    // user-provided) and its snake_case normalization so downstream readers
    // can look up either form. Business-specific alias enrichment
    // (title/company/tax/address/phone/bank — Meituan invoice-flavoured) was
    // removed 2026-05-28; if an entity needs additional aliases they should
    // come from entity_schema metadata, not from hardcoded snake-substring
    // branches in this template. See spec §15.6.
    const raw = String(fieldKey || "").trim();
    if (!raw) return {};
    const snake = raw
      .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
      .replace(/[^A-Za-z0-9]+/g, "_")
      .replace(/^_+|_+$/g, "")
      .toLowerCase();
    const patch = {};
    [raw, snake].forEach((alias) => {
      if (alias) patch[alias] = fieldValue;
    });
    return patch;
  }

  _gwCreateEntityDraft(storageKey, payload, options) {
    const draftsKey = this._gwDraftStorageKey(storageKey);
    const drafts = this._getFromStorage(draftsKey, {});
    const draftId = this._generateId(storageKey + "_draft");
    const draft = {
      id: draftId,
      draftId: draftId,
      fields: this._interfacePayloadFromObject(payload || {}),
      options: this._interfaceOptionsFromObject(options || {}, {}),
      status: "draft"
    };
    drafts[draftId] = draft;
    this._saveToStorage(draftsKey, drafts);
    return draft;
  }

  _gwUpdateEntityDraftField(storageKey, draftIdentifier, fieldKey, fieldValue) {
    const draftsKey = this._gwDraftStorageKey(storageKey);
    const drafts = this._getFromStorage(draftsKey, {});
    let draftId = this._gwDraftIdFrom(draftIdentifier);
    if (!draftId || !drafts[draftId]) {
      const draft = this._gwCreateEntityDraft(storageKey, {}, {});
      draftId = draft.draftId;
      drafts[draftId] = draft;
    }
    const draft = drafts[draftId] || { id: draftId, draftId: draftId, fields: {}, options: {}, status: "draft" };
    draft.fields = Object.assign({}, draft.fields || {}, this._gwFieldAliasPatch(fieldKey, fieldValue));
    draft.status = "draft";
    drafts[draftId] = draft;
    this._saveToStorage(draftsKey, drafts);
    return draft;
  }

  _gwCommitEntityDraft(storageKey, draftIdentifier, options, saveFn) {
    const directPayload = this._isPlainObject(draftIdentifier) && !this._gwDraftIdFrom(draftIdentifier)
      ? this._interfacePayloadFromObject(draftIdentifier)
      : null;
    if (directPayload) return saveFn(directPayload, this._interfaceOptionsFromObject(draftIdentifier, options || {}));
    const draftsKey = this._gwDraftStorageKey(storageKey);
    const drafts = this._getFromStorage(draftsKey, {});
    const draftId = this._gwDraftIdFrom(draftIdentifier);
    const draft = draftId ? drafts[draftId] : null;
    const payload = this._interfacePayloadFromObject((draft && draft.fields) || draftIdentifier || {});
    const mergedOptions = Object.assign({}, (draft && draft.options) || {}, this._interfaceOptionsFromObject(options || {}, {}));
    const result = saveFn(payload, mergedOptions);
    if (draftId && drafts[draftId]) {
      delete drafts[draftId];
      this._saveToStorage(draftsKey, drafts);
    }
    if (this._isPlainObject(result)) {
      result.draftId = draftId || result.draftId || "";
      result.committed = true;
    }
    return result;
  }

"""]

        for entity_name, spec in sorted(entities.items()):
            method_entity = spec.get("method_stem") or ("".join(ch for ch in entity_name if ch.isalnum()) or "Entity")
            method_plural = spec.get("method_plural_stem") or self._camel_plural(method_entity)
            lower_entity = spec.get("lower_stem") or self._lower_camel(method_entity)
            lower_plural = spec.get("lower_plural_stem") or self._lower_camel(method_plural)
            snake_entity = spec.get("snake_stem") or self._snake_case(entity_name)
            storage_key = self._js_string(spec.get("storage_key") or self._snake_case(entity_name) + "s")
            entity_literal = self._js_string(entity_name)
            field_defs = []
            requirement_by_field: Dict[str, bool] = {}
            for field in spec.get("fields", []):
                if not isinstance(field, dict):
                    continue
                field_name = str(field.get("name", "") or "")
                if not field_name.endswith("_requirement"):
                    continue
                base_name = field_name[: -len("_requirement")]
                examples = field.get("observed_examples", [])
                if not isinstance(examples, list):
                    examples = [examples]
                requirement_text = " ".join(str(item) for item in examples)
                if "必填" in requirement_text or "required" in requirement_text.lower():
                    requirement_by_field[base_name] = True
            for field in spec.get("fields", []):
                if not isinstance(field, dict):
                    continue
                field_name = field.get("name", "")
                if not field_name or field_name == "id":
                    continue
                field_defs.append({
                    "key": field_name,
                    "label": field_name,
                    "controlType": "switch" if field.get("type") == "boolean" else "text",
                    "required": bool(field.get("required") is True or requirement_by_field.get(str(field_name))),
                })
            fields_json = json.dumps(field_defs[:20], ensure_ascii=False)
            method_specs = list((methods_by_entity.get(entity_name) or {}).values())
            collection_aliases = sorted(set(
                alias
                for method in method_specs
                for alias in method.get("collection_aliases", [])
                if alias
            ))
            record_aliases = sorted(set(
                alias
                for method in method_specs
                for alias in method.get("record_aliases", [])
                if alias
            ))
            collection_aliases_json = json.dumps(collection_aliases, ensure_ascii=False)
            record_aliases_json = json.dumps(record_aliases, ensure_ascii=False)
            lines.append(f"""  _gwList{method_plural}() {{
    return this._entityListResult({storage_key}, {collection_aliases_json}, {self._js_string(lower_plural)}, {fields_json});
  }}

  _gwGet{method_plural}() {{
    return this._gwList{method_plural}().items;
  }}

  _gwGet{method_entity}(identifier) {{
    const record = this._findEntityRecord({storage_key}, identifier);
    return record ? Object.assign({{}}, record) : null;
  }}

  _gwCreate{method_entity}(payload, options) {{
    const opts = typeof options === "boolean" ? {{ setAsDefault: options }} : (options || {{}});
    const record = this._createEntityRecord({storage_key}, {entity_literal}, payload || {{}}, opts, {fields_json});
    return this._entityRecordResult("created", record, {record_aliases_json}, {self._js_string(lower_entity)}, {fields_json});
  }}

  _gwUpdate{method_entity}(identifier, patch, options) {{
    const opts = typeof options === "boolean" ? {{ setAsDefault: options }} : (options || {{}});
    const record = this._updateEntityRecord({storage_key}, {entity_literal}, identifier, patch || {{}}, opts, {fields_json});
    return this._entityRecordResult("updated", record, {record_aliases_json}, {self._js_string(lower_entity)}, {fields_json});
  }}

  _gwDelete{method_entity}(identifier) {{
    const records = this._listEntityRecords({storage_key});
    const index = this._findEntityIndex(records, identifier, {storage_key});
    if (index < 0) return {{ deleted: false }};
    const removed = records.splice(index, 1)[0];
    this._saveEntityRecords({storage_key}, records);
    return {{ deleted: true, record: removed, item: removed }};
  }}

  _gwSetDefault{method_entity}(identifier) {{
    const records = this._listEntityRecords({storage_key});
    const record = this._setEntityDefault(records, identifier, {storage_key});
    this._saveEntityRecords({storage_key}, records);
    return {{ defaultId: record ? record.id : "", updated: !!record }};
  }}

  _gwGet{method_entity}Detail(identifier) {{
    const record = this._gwGet{method_entity}(identifier);
    return this._entityRecordResult(null, record || {{}}, {record_aliases_json}, {self._js_string(lower_entity)}, {fields_json});
  }}

  _gwGet{method_entity}ListPageModel() {{
    const list = this._gwList{method_plural}();
    return {{
      pageKey: {self._js_string(snake_entity + "_list")},
      pageTitle: {entity_literal},
      listSection: {{
        sectionTitle: {entity_literal},
        items: list.items
      }},
      items: list.items,
      records: list.items,
      {lower_plural}: list.items,
      defaultId: list.defaultId
    }};
  }}

  _gwGet{method_entity}FormPageModel(mode, identifier) {{
    const record = mode === "edit" ? this._gwGet{method_entity}(identifier) : null;
    const rawRecord = record ? Object.assign({{}}, record) : null;
    const fields = {fields_json}.map(field => Object.assign({{}}, field, {{
      value: rawRecord ? (rawRecord[field.key] || "") : ""
    }}));
    const result = {{
      page: {{ pageTitle: mode === "edit" ? "Edit " + {entity_literal} : "Create " + {entity_literal}, mode: mode || "create", canDelete: mode === "edit" }},
      record: rawRecord || {{}},
      item: rawRecord || {{}},
      data: rawRecord || {{}},
      form: {{ fields: fields }}
    }};
    result[{self._js_string(lower_entity)}] = rawRecord || {{}};
    for (const alias of {record_aliases_json}) {{
      if (alias && !Object.prototype.hasOwnProperty.call(result, alias)) result[alias] = rawRecord || {{}};
    }}
    return result;
  }}

  _gwSave{method_entity}(payload, options) {{
    const opts = options || {{}};
    if (opts.id || opts.identifier || opts.mode === "edit") {{
      return this._gwUpdate{method_entity}(opts.id || opts.identifier, payload, opts);
    }}
    return this._gwCreate{method_entity}(payload, opts);
  }}

  _gwSave_{snake_entity}(payload, options) {{
    return this._gwSave{method_entity}(payload, options);
  }}

  _gwCreate{method_entity}Draft(payload, options) {{
    return this._gwCreateEntityDraft({storage_key}, payload || {{}}, options || {{}});
  }}

  _gwUpdate{method_entity}DraftField(draftIdentifier, fieldKey, fieldValue) {{
    return this._gwUpdateEntityDraftField({storage_key}, draftIdentifier, fieldKey, fieldValue);
  }}

  _gwCommit{method_entity}Draft(draftIdentifier, options) {{
    return this._gwCommitEntityDraft({storage_key}, draftIdentifier, options || {{}}, (payload, opts) => this._gwSave{method_entity}(payload, opts));
  }}

""")
            canonical_names = set([
                "list" + method_plural,
                "get" + method_plural,
                "get" + method_entity,
                "create" + method_entity,
                "update" + method_entity,
                "delete" + method_entity,
                "setDefault" + method_entity,
                "get" + method_entity + "Detail",
                "get" + method_entity + "ListPageModel",
                "get" + method_entity + "FormPageModel",
                "save" + method_entity,
                "save_" + snake_entity,
            ])
            for method in sorted(method_specs, key=lambda item: item.get("name", "")):
                method_name = method.get("name")
                if not method_name:
                    continue
                safe_method_name = "".join(ch for ch in method_name if ch.isalnum() or ch == "_")
                if not safe_method_name or safe_method_name[0].isdigit():
                    continue
                operation = method.get("operation") or self._infer_interface_operation(method)
                wrapper = self._build_deterministic_alias_wrapper(
                    safe_method_name,
                    operation,
                    method_entity,
                    method_plural,
                    snake_entity,
                    method,
                    spec,
                    entity_name,
                )
                if wrapper:
                    lines.append(wrapper)

        return "".join(lines)

    def _build_deterministic_interface_alias_methods(self,
                                                     interfaces: Dict[str, Any],
                                                     runtime_contract: Dict[str, Any]) -> str:
        entities = (runtime_contract or {}).get("entities", {})
        if not entities or not isinstance(interfaces, dict):
            return ""
        entity_names = sorted(entities.keys(), key=len, reverse=True)
        lines = []
        emitted = set()
        for iface in interfaces.get("interfaces", []) or []:
            if not isinstance(iface, dict):
                continue
            name = iface.get("name", "")
            if not name or name in emitted:
                continue
            operation = iface.get("operation") or self._infer_interface_operation(iface)
            entity_name = iface.get("entity") or self._infer_interface_entity(iface, entity_names)
            if not entity_name or entity_name not in entities:
                continue
            if not self._is_deterministic_sdk_interface(iface, entity_names):
                continue
            spec = entities[entity_name]
            method_entity = spec.get("method_stem") or ("".join(ch for ch in entity_name if ch.isalnum()) or "Entity")
            method_plural = spec.get("method_plural_stem") or self._camel_plural(method_entity)
            snake_entity = spec.get("snake_stem") or self._snake_case(entity_name)
            safe_name = "".join(ch for ch in name if ch.isalnum() or ch == "_")
            if not safe_name or safe_name[0].isdigit():
                continue
            wrapper = self._build_deterministic_alias_wrapper(
                safe_name,
                operation,
                method_entity,
                method_plural,
                snake_entity,
                iface,
                spec,
                entity_name,
            )
            if wrapper:
                lines.append(wrapper)
                emitted.add(name)
        return "".join(lines)

    def _build_deterministic_alias_wrapper(self,
                                           method_name: str,
                                           operation: str,
                                           method_entity: str,
                                           method_plural: str,
                                           snake_entity: str,
                                           iface: Dict[str, Any] = None,
                                           entity_spec: Dict[str, Any] = None,
                                           entity_name: str = "") -> str:
        params = self._interface_param_names(iface or {})
        signature = ", ".join(params)
        identifier = self._select_entity_identifier_param(iface or {}, entity_name, method_entity, params)
        payload = self._build_interface_payload_expr(iface or {}, entity_spec or {}, params)
        patch = self._select_patch_param(iface or {}, params) or payload
        options = self._select_options_expr(iface or {}, params)
        mode_param = self._select_param_by_name(params, ("mode",)) or "\"create\""
        if not identifier:
            identifier = params[0] if params else "undefined"
        if not params:
            if operation in ("create", "save", "submit", "select", "apply"):
                signature = "payload, options"
                payload = "payload"
                options = "options"
            elif operation == "update":
                signature = "identifier, patch, options"
                identifier = "identifier"
                patch = "patch"
                options = "options"
            elif operation in ("get", "delete", "set_default"):
                signature = "identifier"
                identifier = "identifier"
            elif operation == "form_page_model":
                signature = "mode, identifier"
                mode_param = "mode"
                identifier = "identifier"
        if operation == "list":
            return """  {name}() {{
    return this._gwList{plural}();
  }}

""".format(name=method_name, plural=method_plural) if not signature else """  {name}({signature}) {{
    return this._gwList{plural}();
  }}

""".format(name=method_name, signature=signature, plural=method_plural)
        if operation == "list_page_model":
            return """  {name}({signature}) {{
    return this._gwGet{entity}ListPageModel();
  }}

""".format(name=method_name, signature=signature, entity=method_entity)
        if operation == "form_page_model":
            return """  {name}({signature}) {{
    return this._gwGet{entity}FormPageModel({mode}, {identifier});
  }}

""".format(name=method_name, signature=signature or "mode, identifier", entity=method_entity, mode=mode_param, identifier=identifier)
        if operation == "get":
            return """  {name}({signature}) {{
    return this._gwGet{entity}Detail({identifier});
  }}

""".format(name=method_name, signature=signature or "identifier", entity=method_entity, identifier=identifier)
        if operation == "create":
            first_param = params[0] if params else "payload"
            return """  {name}({signature}) {{
    if (arguments.length === 1 && this._isPlainObject({first_param})) {{
      return this._gwCreate{entity}(this._interfacePayloadFromObject({first_param}), this._interfaceOptionsFromObject({first_param}, {{}}));
    }}
    return this._gwCreate{entity}({payload}, {options});
  }}

""".format(name=method_name, signature=signature or "payload, options", entity=method_entity, payload=payload, options=options, first_param=first_param)
        if operation == "create_record_draft":
            first_param = params[0] if params else "payload"
            return """  {name}({signature}) {{
    if (arguments.length === 1 && this._isPlainObject({first_param})) {{
      return this._gwCreate{entity}Draft(this._interfacePayloadFromObject({first_param}), this._interfaceOptionsFromObject({first_param}, {{}}));
    }}
    return this._gwCreate{entity}Draft({payload}, {options});
  }}

""".format(name=method_name, signature=signature or "payload, options", entity=method_entity, payload=payload, options=options, first_param=first_param)
        if operation == "update":
            first_param = params[0] if params else "identifier"
            return """  {name}({signature}) {{
    if (arguments.length === 1 && this._isPlainObject({first_param})) {{
      const input = {first_param};
      return this._gwUpdate{entity}(
        this._interfaceIdentifierFromObject(input, {identifier}),
        this._interfacePayloadFromObject(input.patch || input.updates || input.values || input),
        this._interfaceOptionsFromObject(input, {options})
      );
    }}
    return this._gwUpdate{entity}({identifier}, {patch}, {options});
  }}

""".format(name=method_name, signature=signature or "identifier, patch, options", entity=method_entity, identifier=identifier, patch=patch, options=options, first_param=first_param)
        if operation == "update_current_draft":
            draft_id = self._select_param_by_name(params, ("draftId", "draft_id")) or identifier
            field_key = self._select_param_by_name(params, ("fieldKey", "field", "name", "key")) or (params[1] if len(params) > 1 else "fieldKey")
            field_value = self._select_param_by_name(params, ("fieldValue", "value", "inputValue")) or (params[2] if len(params) > 2 else "fieldValue")
            first_param = params[0] if params else "draftId"
            return """  {name}({signature}) {{
    if (arguments.length === 1 && this._isPlainObject({first_param})) {{
      const input = {first_param};
      return this._gwUpdate{entity}DraftField(
        input.draftId || input.draft_id || input.id || input.identifier,
        input.fieldKey || input.field || input.name || input.key,
        input.fieldValue || input.value || input.inputValue
      );
    }}
    return this._gwUpdate{entity}DraftField({draft_id}, {field_key}, {field_value});
  }}

""".format(name=method_name, signature=signature or "draftId, fieldKey, fieldValue", entity=method_entity, draft_id=draft_id, field_key=field_key, field_value=field_value, first_param=first_param)
        if operation == "delete":
            return """  {name}({signature}) {{
    return this._gwDelete{entity}({identifier});
  }}

""".format(name=method_name, signature=signature or "identifier", entity=method_entity, identifier=identifier)
        if operation == "set_default":
            return """  {name}({signature}) {{
    return this._gwSetDefault{entity}({identifier});
  }}

""".format(name=method_name, signature=signature or "identifier", entity=method_entity, identifier=identifier)
        if operation in ("save", "submit", "select", "apply"):
            first_param = params[0] if params else "payload"
            return """  {name}({signature}) {{
    if (arguments.length === 1 && this._isPlainObject({first_param})) {{
      return this._gwSave{entity}(this._interfacePayloadFromObject({first_param}), this._interfaceOptionsFromObject({first_param}, {{}}));
    }}
    return this._gwSave{entity}({payload}, {options});
  }}

""".format(name=method_name, signature=signature or "payload, options", entity=method_entity, payload=payload, options=options, first_param=first_param)
        if operation == "commit_draft":
            draft_id = self._select_param_by_name(params, ("draftId", "draft_id")) or identifier
            first_param = params[0] if params else "draftId"
            return """  {name}({signature}) {{
    if (arguments.length === 1 && this._isPlainObject({first_param})) {{
      return this._gwCommit{entity}Draft({first_param}, this._interfaceOptionsFromObject({first_param}, {{}}));
    }}
    return this._gwCommit{entity}Draft({draft_id}, {options});
  }}

""".format(name=method_name, signature=signature or "draftId, options", entity=method_entity, draft_id=draft_id, options=options, first_param=first_param)
        return ""

    def _interface_param_names(self, iface: Dict[str, Any]) -> List[str]:
        result = []
        used = set()
        for index, param in enumerate(iface.get("parameters", []) or []):
            if not isinstance(param, dict):
                continue
            raw_name = str(param.get("name") or "arg{}".format(index))
            name = "".join(ch for ch in raw_name if ch.isalnum() or ch == "_") or "arg{}".format(index)
            if name[0].isdigit():
                name = "arg_" + name
            base = name
            suffix = 2
            while name in used:
                name = "{}{}".format(base, suffix)
                suffix += 1
            used.add(name)
            result.append(name)
        return result

    def _select_param_by_name(self, params: List[str], names: Tuple[str, ...]) -> str:
        lowered = {name.lower(): name for name in params}
        for name in names:
            if name.lower() in lowered:
                return lowered[name.lower()]
        return ""

    def _select_entity_identifier_param(self,
                                        iface: Dict[str, Any],
                                        entity_name: str,
                                        method_entity: str,
                                        params: List[str]) -> str:
        raw_params = iface.get("parameters", []) or []
        for index, param in enumerate(raw_params):
            if not isinstance(param, dict) or index >= len(params):
                continue
            entity_ref = param.get("entityReference")
            if isinstance(entity_ref, dict) and entity_ref.get("entity") in (entity_name, method_entity):
                return params[index]
        entity_tokens = [
            self._snake_case(entity_name).replace("_", ""),
            self._snake_case(method_entity).replace("_", ""),
        ]
        for name in params:
            normalized = name.lower().replace("_", "")
            if normalized in ("userid", "user", "sessionid", "channel"):
                continue
            if normalized.endswith("id") and any(token and token in normalized for token in entity_tokens):
                return name
        for name in params:
            normalized = name.lower().replace("_", "")
            if normalized.endswith("id") and normalized not in ("userid", "sessionid"):
                return name
        return ""

    def _select_patch_param(self, iface: Dict[str, Any], params: List[str]) -> str:
        raw_params = iface.get("parameters", []) or []
        preferred = set(["patch", "payload", "data", "input", "updates", "values"])
        for index, param in enumerate(raw_params):
            if not isinstance(param, dict) or index >= len(params):
                continue
            if params[index].lower() in preferred or param.get("type") == "object":
                return params[index]
        return ""

    def _select_options_expr(self, iface: Dict[str, Any], params: List[str]) -> str:
        options_param = self._select_param_by_name(params, ("options",))
        if options_param:
            return options_param
        default_param = self._select_param_by_name(params, ("setAsDefault", "isDefault", "makeDefault"))
        if default_param:
            return "{{ setAsDefault: {} }}".format(default_param)
        return "{}"

    def _build_interface_payload_expr(self,
                                      iface: Dict[str, Any],
                                      entity_spec: Dict[str, Any],
                                      params: List[str]) -> str:
        raw_params = iface.get("parameters", []) or []
        if len(params) == 1:
            return params[0]
        if params:
            first_raw = raw_params[0] if raw_params and isinstance(raw_params[0], dict) else {}
            if params[0].lower() in set(["payload", "data", "input", "values"]) and first_raw.get("type") == "object":
                return params[0]
        fields = set()
        for field in entity_spec.get("fields", []) or []:
            if isinstance(field, dict) and field.get("name"):
                fields.add(field.get("name"))
        pairs = []
        for index, param_name in enumerate(params):
            raw_param = raw_params[index] if index < len(raw_params) and isinstance(raw_params[index], dict) else {}
            if raw_param.get("entityReference"):
                continue
            if param_name == "options":
                continue
            key = self._canonical_payload_key(param_name, fields)
            pairs.append("{}: {}".format(json.dumps(key), param_name))
        return "{" + ", ".join(pairs) + "}"

    def _canonical_payload_key(self, param_name: str, field_names: set) -> str:
        if param_name in field_names:
            return param_name
        snake = self._snake_case(param_name)
        if snake in field_names:
            return snake
        return param_name

    def _build_storage_schema(self, entity_schema: Dict[str, Any]) -> Dict[str, Any]:
        schema = {}


        if 'entities' in entity_schema:
            for entity in entity_schema['entities']:
                if not isinstance(entity, dict):
                    continue
                entity_name = entity.get('name', '')
                if not entity_name or entity_name.startswith('_'):
                    continue
                storage_key = entity.get('storage_key', entity_name.lower() + 's')
                fields = []
                for field in entity.get('fields', []):
                    if isinstance(field, dict):
                        f = {'name': field.get('name', '')}
                        if field.get('type'):
                            f['type'] = field['type']
                        if field.get('foreign_key') or field.get('references'):
                            f['fk'] = field.get('foreign_key') or field.get('references')
                        if field.get('description'):
                            f['desc'] = field['description']
                        fields.append(f)
                schema[entity_name] = {'storage_key': storage_key, 'fields': fields}
            return schema


        for entity_name, entity_def in entity_schema.items():
            if entity_name.startswith('_') or not isinstance(entity_def, dict):
                continue
            storage_key = entity_def.get('storage_key', entity_name.lower() + 's')
            fields = []
            for field in entity_def.get('fields', []):
                if isinstance(field, dict):
                    f = {'name': field.get('name', '')}
                    if field.get('type'):
                        f['type'] = field['type']
                    if field.get('foreign_key') or field.get('references'):
                        f['fk'] = field.get('foreign_key') or field.get('references')
                    if field.get('description'):
                        f['desc'] = field['description']
                    fields.append(f)
            schema[entity_name] = {'storage_key': storage_key, 'fields': fields}
        return schema

    async def _generate_method_bodies(self,
                                       entity_schema: Dict[str, Any],
                                       interfaces: Dict[str, Any],
                                       website_type: str,
                                       init_data: Dict[str, Any] = None,
                                       trace_context: str = None) -> str:
        storage_schema = self._build_storage_schema(entity_schema)
        implementation_view = self._build_sdk_implementation_view(
            entity_schema,
            interfaces,
            storage_schema,
        )
        helper_functions = interfaces.get("helperFunctions", [])
        data_snapshot = self._build_sdk_data_snapshot(entity_schema, init_data or {})
        semantic_shard_contracts = self._build_sdk_semantic_shard_contracts(
            implementation_view,
            storage_schema,
            data_snapshot,
            trace_context,
            helper_functions,
            website_type,
        )
        shards = self._build_sdk_method_shards(
            semantic_shard_contracts,
            storage_schema,
            data_snapshot,
            trace_context,
            helper_functions,
            website_type,
        )

        if self.logger:
            self.logger.log_info(
                "SDK method context sharding: {} methods into {} semantic shards and {} call shards".format(
                    len(implementation_view.get("methods", [])),
                    len(semantic_shard_contracts),
                    len(shards),
                )
            )
            self._write_sdk_context_budget_report(semantic_shard_contracts, shards)

        if not shards:
            return ""

        async def run_shard(shard):
            return await self._generate_method_body_shard(shard, website_type)

        first_pass = await asyncio.gather(
            *[run_shard(shard) for shard in shards],
            return_exceptions=True,
        )
        shard_results = [None] * len(shards)
        failed_indexes = []
        for index, result in enumerate(first_pass):
            if isinstance(result, Exception):
                failed_indexes.append(index)
            else:
                shard_results[index] = result

        if failed_indexes:
            if self.logger:
                self.logger.log_warning(
                    "SDK method generation retrying {} failed shard(s): {}".format(
                        len(failed_indexes),
                        ", ".join(
                            str(shards[index].get("shard_id", "shard_{}".format(index)))
                            for index in failed_indexes
                        ),
                    )
                )
            retry_pass = await asyncio.gather(
                *[run_shard(shards[index]) for index in failed_indexes],
                return_exceptions=True,
            )
            unrecovered = []
            for index, result in zip(failed_indexes, retry_pass):
                if isinstance(result, Exception):
                    unrecovered.append((index, result))
                else:
                    shard_results[index] = result
            if unrecovered:
                detail = "; ".join(
                    "{}: {}".format(
                        shards[index].get("shard_id", "shard_{}".format(index)),
                        error,
                    )
                    for index, error in unrecovered
                )
                raise RuntimeError(
                    "generate_sdk_methods failed after retry for {}".format(detail)
                ) from unrecovered[0][1]

        methods = self._assemble_sdk_shard_results(shards, shard_results)
        missing_methods = self._find_missing_interface_methods(
            implementation_view.get("methods", []),
            methods,
        )
        if missing_methods:
            raise RuntimeError(
                "SDK method coverage incomplete; missing_methods={}".format(
                    ", ".join(missing_methods)
                )
            )
        return methods

    async def _generate_method_body_shard(self, shard: Dict[str, Any], website_type: str) -> str:
        prompt = shard.get("prompt", "")
        shard_id = shard.get("shard_id", "sdk_methods")
        attempt_index = self._next_sdk_shard_attempt_index(shard_id)
        attempt_dir = self._create_sdk_shard_attempt_dir(shard_id, attempt_index)

        call_id = None
        if self.logger:
            self._ensure_sdk_logger_stage_directory()
            call_id = self.logger.log_api_call(
                "Generate SDK Methods ({})".format(shard_id),
                prompt,
                additional_args={
                    "website_type": website_type,
                    "shard_id": shard_id,
                    "method_count": len(shard.get("methods", [])),
                    "estimated_tokens": shard.get("estimated_tokens"),
                },
                stage="Generate SDK",
            )

        request_payload = {
            "schema": "ghostweb.sdk-shard-request.v1",
            "shard_id": shard_id,
            "attempt_index": attempt_index,
            "call_id": call_id,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "website_type": website_type,
            "expected_method_names": self._expected_sdk_shard_method_names(shard),
            "prompt_sha256": self._sha256_text(prompt),
            "prompt_size_bytes": len(prompt.encode("utf-8")),
        }
        request_ref = self._persist_sdk_shard_attempt_file(
            attempt_dir, "request.json", self._canonical_json_bytes(request_payload)
        )
        prompt_ref = self._persist_sdk_shard_attempt_file(
            attempt_dir, "prompt.txt", prompt.encode("utf-8")
        )

        if attempt_dir is not None and not call_id:
            error = RuntimeError(
                "SDK method shard {} has no durable call_id; refusing provider call".format(
                    shard_id
                )
            )
            self._persist_sdk_shard_failure(
                shard=shard,
                attempt_index=attempt_index,
                attempt_dir=attempt_dir,
                call_id=call_id,
                request_ref=request_ref,
                prompt_ref=prompt_ref,
                error=error,
            )
            raise error

        raw_response_ref = None
        try:
            response, usage = await call_openai_api_json_async(
                [{"role": "user", "content": prompt}],
                model=self.model,
                reasoning_effort=self.reasoning_effort,
                stage="generate_sdk_methods",
            )

            raw_response_ref = self._persist_sdk_shard_attempt_file(
                attempt_dir,
                "raw_response.txt",
                self._raw_sdk_response_bytes(response),
            )

            if isinstance(response, str):
                response = json.loads(response)

            if response is None:
                raise RuntimeError("LLM returned None for {}".format(shard_id))

            if not isinstance(response, dict):
                raise RuntimeError(
                    "SDK method shard {} returned {}, expected JSON object".format(
                        shard_id,
                        type(response).__name__,
                    )
                )
            if set(response) != {"methods"}:
                raise RuntimeError(
                    "SDK method shard {} must return exactly the methods field; got fields={}".format(
                        shard_id,
                        sorted(str(key) for key in response),
                    )
                )
            if not isinstance(response["methods"], str):
                raise RuntimeError(
                    "SDK method shard {} methods field must be a string".format(shard_id)
                )

            cleaned = self._clean_generated_method_body(response["methods"])
            verdict = self._assert_generated_method_body_coverage(shard, cleaned)
            cleaned_ref = self._persist_sdk_shard_attempt_file(
                attempt_dir, "cleaned_methods.js", cleaned.encode("utf-8")
            )
            verdict_ref = self._persist_sdk_shard_attempt_file(
                attempt_dir,
                "validation_verdict.json",
                self._canonical_json_bytes(
                    {
                        "schema": "ghostweb.sdk-shard-validation-verdict.v1",
                        "status": "admitted",
                        **verdict,
                    }
                ),
            )
            manifest_ref = self._persist_sdk_shard_attempt_manifest(
                shard=shard,
                attempt_index=attempt_index,
                attempt_dir=attempt_dir,
                call_id=call_id,
                status="admitted",
                refs={
                    "request": request_ref,
                    "prompt": prompt_ref,
                    "raw_response": raw_response_ref,
                    "cleaned_methods": cleaned_ref,
                    "validation_verdict": verdict_ref,
                },
                usage=usage,
            )
            if manifest_ref is not None:
                with self._sdk_shard_attempt_lock:
                    self._sdk_shard_latest_attempts[shard_id] = manifest_ref

            if self.logger:
                self.logger.log_api_response(
                    "Generate SDK Methods ({})".format(shard_id),
                    success=True,
                    response=response,
                    usage_info=usage,
                    stage="Generate SDK",
                    call_id=call_id,
                )
            return cleaned

        except Exception as e:
            import traceback
            exception_raw_response = getattr(e, "raw_response", None)
            if raw_response_ref is None and exception_raw_response is not None:
                raw_response_ref = self._persist_sdk_shard_attempt_file(
                    attempt_dir,
                    "raw_response.txt",
                    self._raw_sdk_response_bytes(exception_raw_response),
                )
            self._persist_sdk_shard_failure(
                shard=shard,
                attempt_index=attempt_index,
                attempt_dir=attempt_dir,
                call_id=call_id,
                request_ref=request_ref,
                prompt_ref=prompt_ref,
                error=e,
                raw_response_ref=raw_response_ref,
            )
            if self.logger:
                self.logger.log_error("SDK generation failed for {}: {}".format(shard_id, str(e)))
                self.logger.log_error("Stack trace:\n{}".format(traceback.format_exc()))
                self.logger.log_api_response(
                    "Generate SDK Methods ({})".format(shard_id),
                    success=False,
                    error=str(e),
                    stage="Generate SDK",
                    call_id=call_id,
                )
            raise

    def _sdk_shard_artifact_root(self) -> Path | None:
        configured = os.environ.get("T2W_SDK_SHARD_ARTIFACT_ROOT", "").strip()
        if configured:
            return Path(configured).expanduser().resolve()
        output_dir = getattr(self.logger, "output_dir", None) if self.logger else None
        if not output_dir:
            return None
        return Path(output_dir).resolve() / "backend" / "generate_sdk" / "shard_attempts"

    def _ensure_sdk_logger_stage_directory(self) -> None:
        if not self.logger or not getattr(self.logger, "output_dir", None):
            return
        stage_name = "Generate SDK"
        stage_dir = Path(self.logger.output_dir).resolve() / "backend" / "generate_sdk"
        stage_dir.mkdir(parents=True, exist_ok=True)
        stage_directories = getattr(self.logger, "stage_directories", None)
        if isinstance(stage_directories, dict):
            stage_directories.setdefault(stage_name, str(stage_dir))
        counters = getattr(self.logger, "llm_call_counters", None)
        if isinstance(counters, dict):
            lock = getattr(self.logger, "lock", None)
            if lock is None:
                counters.setdefault(stage_name, 0)
            else:
                with lock:
                    counters.setdefault(stage_name, 0)

    def _next_sdk_shard_attempt_index(self, shard_id: str) -> int:
        with self._sdk_shard_attempt_lock:
            value = self._sdk_shard_attempt_counts.get(shard_id, 0) + 1
            self._sdk_shard_attempt_counts[shard_id] = value
            return value

    def _create_sdk_shard_attempt_dir(
        self, shard_id: str, attempt_index: int
    ) -> Path | None:
        root = self._sdk_shard_artifact_root()
        if root is None:
            return None
        safe_shard_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", shard_id).strip("._")
        if not safe_shard_id:
            raise RuntimeError("SDK shard id cannot be normalized to an empty path")
        attempt_dir = root / safe_shard_id / "attempt_{:03d}".format(attempt_index)
        attempt_dir.mkdir(parents=True, exist_ok=False)
        return attempt_dir

    @staticmethod
    def _sha256_bytes(payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()

    @classmethod
    def _sha256_text(cls, value: str) -> str:
        return cls._sha256_bytes((value or "").encode("utf-8"))

    @staticmethod
    def _canonical_json_bytes(value: Any) -> bytes:
        return (
            json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")

    def _persist_sdk_shard_attempt_file(
        self, attempt_dir: Path | None, filename: str, payload: bytes
    ) -> Dict[str, Any] | None:
        if attempt_dir is None:
            return None
        target = attempt_dir / filename
        if target.exists():
            raise RuntimeError("immutable SDK shard artifact already exists: {}".format(target))
        fd, temporary_name = tempfile.mkstemp(prefix=".{}-".format(filename), dir=str(attempt_dir))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, target)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
        return {
            "path": str(target),
            "sha256": self._sha256_bytes(payload),
            "size_bytes": len(payload),
        }

    def _raw_sdk_response_bytes(self, response: Any) -> bytes:
        if isinstance(response, str):
            return response.encode("utf-8")
        return self._canonical_json_bytes(response)

    @staticmethod
    def _method_names_from_contract_rows(rows: Any) -> List[str]:
        names = []
        for row in rows or []:
            if isinstance(row, dict):
                name = str(row.get("name", "")).strip()
            else:
                name = str(row).strip()
            if name:
                names.append(name)
        return names

    def _expected_sdk_shard_method_names(self, shard: Dict[str, Any]) -> List[str]:
        names = self._method_names_from_contract_rows((shard or {}).get("methods", []))
        names.extend(
            self._method_names_from_contract_rows(
                (shard or {}).get("helper_functions", [])
            )
        )
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise RuntimeError(
                "SDK method shard {} repeats required method names: {}".format(
                    (shard or {}).get("shard_id", "sdk_methods"),
                    ", ".join(duplicates),
                )
            )
        return names

    def _sdk_method_coverage_verdict(
        self, shard: Dict[str, Any], generated_methods: str
    ) -> Dict[str, Any]:
        expected = self._expected_sdk_shard_method_names(shard)
        emitted = sorted(self._extract_generated_method_names(generated_methods))
        expected_set = set(expected)
        emitted_set = set(emitted)
        return {
            "shard_id": (shard or {}).get("shard_id", "sdk_methods"),
            "expected_method_names": expected,
            "emitted_method_names": emitted,
            "missing_method_names": sorted(expected_set - emitted_set),
            "unexpected_method_names": sorted(emitted_set - expected_set),
            "exact_method_set_match": expected_set == emitted_set,
        }

    def _persist_sdk_shard_attempt_manifest(
        self,
        *,
        shard: Dict[str, Any],
        attempt_index: int,
        attempt_dir: Path | None,
        call_id: str | None,
        status: str,
        refs: Dict[str, Any],
        usage: Dict[str, Any] | None = None,
    ) -> Dict[str, Any] | None:
        if attempt_dir is None:
            return None
        manifest = {
            "schema": "ghostweb.sdk-shard-attempt-manifest.v1",
            "status": status,
            "shard_id": (shard or {}).get("shard_id", "sdk_methods"),
            "attempt_index": attempt_index,
            "call_id": call_id,
            "expected_method_names": self._expected_sdk_shard_method_names(shard),
            "artifacts": {key: value for key, value in refs.items() if value is not None},
            "usage": usage if isinstance(usage, dict) else None,
            "manifest_last": True,
        }
        return self._persist_sdk_shard_attempt_file(
            attempt_dir, "manifest.json", self._canonical_json_bytes(manifest)
        )

    def _persist_sdk_shard_failure(
        self,
        *,
        shard: Dict[str, Any],
        attempt_index: int,
        attempt_dir: Path | None,
        call_id: str | None,
        request_ref: Dict[str, Any] | None,
        prompt_ref: Dict[str, Any] | None,
        error: Exception,
        raw_response_ref: Dict[str, Any] | None = None,
    ) -> None:
        if attempt_dir is None or (attempt_dir / "manifest.json").exists():
            return
        refs = {
            "request": request_ref,
            "prompt": prompt_ref,
            "raw_response": raw_response_ref,
        }
        emitted_methods = ""
        if raw_response_ref is not None:
            raw_path = Path(raw_response_ref["path"])
            try:
                raw_payload = json.loads(raw_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError, ValueError):
                raw_payload = None
            if isinstance(raw_payload, dict) and isinstance(raw_payload.get("methods"), str):
                emitted_methods = self._clean_generated_method_body(raw_payload["methods"])
                refs["cleaned_methods"] = self._persist_sdk_shard_attempt_file(
                    attempt_dir, "cleaned_methods.js", emitted_methods.encode("utf-8")
                )
        verdict = self._sdk_method_coverage_verdict(shard, emitted_methods)
        verdict.update(
            {
                "schema": "ghostweb.sdk-shard-validation-verdict.v1",
                "status": "rejected",
                "error_type": type(error).__name__,
                "error": str(error),
            }
        )
        refs["validation_verdict"] = self._persist_sdk_shard_attempt_file(
            attempt_dir,
            "validation_verdict.json",
            self._canonical_json_bytes(verdict),
        )
        refs["error"] = self._persist_sdk_shard_attempt_file(
            attempt_dir,
            "error.json",
            self._canonical_json_bytes(
                {
                    "schema": "ghostweb.sdk-shard-error.v1",
                    "error_type": type(error).__name__,
                    "error": str(error),
                }
            ),
        )
        self._persist_sdk_shard_attempt_manifest(
            shard=shard,
            attempt_index=attempt_index,
            attempt_dir=attempt_dir,
            call_id=call_id,
            status="rejected",
            refs=refs,
        )

    def _read_and_verify_sdk_artifact_ref(self, ref: Dict[str, Any], role: str) -> bytes:
        if not isinstance(ref, dict):
            raise RuntimeError("SDK shard reuse missing {} artifact ref".format(role))
        path = Path(str(ref.get("path", "")))
        if not path.is_file():
            raise RuntimeError("SDK shard reuse missing {} artifact: {}".format(role, path))
        payload = path.read_bytes()
        if len(payload) != ref.get("size_bytes"):
            raise RuntimeError("SDK shard reuse {} size mismatch: {}".format(role, path))
        if self._sha256_bytes(payload) != ref.get("sha256"):
            raise RuntimeError("SDK shard reuse {} SHA mismatch: {}".format(role, path))
        return payload

    def _validate_sdk_shard_attempt_for_reuse(
        self, manifest_path: str | Path, shard: Dict[str, Any]
    ) -> str:
        path = Path(manifest_path)
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("schema") != "ghostweb.sdk-shard-attempt-manifest.v1":
            raise RuntimeError("SDK shard reuse manifest schema mismatch: {}".format(path))
        if manifest.get("status") != "admitted" or manifest.get("manifest_last") is not True:
            raise RuntimeError("SDK shard reuse manifest is not an admitted manifest-last attempt")
        if not isinstance(manifest.get("call_id"), str) or not manifest["call_id"].strip():
            raise RuntimeError("SDK shard reuse requires a durable call_id")
        expected = self._expected_sdk_shard_method_names(shard)
        if manifest.get("expected_method_names") != expected:
            raise RuntimeError("SDK shard reuse expected-method binding mismatch")
        refs = manifest.get("artifacts")
        if not isinstance(refs, dict):
            raise RuntimeError("SDK shard reuse manifest has no artifact map")
        request = json.loads(
            self._read_and_verify_sdk_artifact_ref(refs.get("request"), "request").decode(
                "utf-8"
            )
        )
        prompt = self._read_and_verify_sdk_artifact_ref(refs.get("prompt"), "prompt")
        raw = self._read_and_verify_sdk_artifact_ref(
            refs.get("raw_response"), "raw_response"
        )
        cleaned = self._read_and_verify_sdk_artifact_ref(
            refs.get("cleaned_methods"), "cleaned_methods"
        ).decode("utf-8")
        verdict = json.loads(
            self._read_and_verify_sdk_artifact_ref(
                refs.get("validation_verdict"), "validation_verdict"
            ).decode("utf-8")
        )
        if request.get("call_id") != manifest["call_id"]:
            raise RuntimeError("SDK shard reuse request/call_id mismatch")
        if request.get("expected_method_names") != expected:
            raise RuntimeError("SDK shard reuse request expected-method mismatch")
        if request.get("prompt_sha256") != self._sha256_bytes(prompt):
            raise RuntimeError("SDK shard reuse request prompt SHA mismatch")
        current_prompt = str((shard or {}).get("prompt", "")).encode("utf-8")
        if prompt != current_prompt:
            raise RuntimeError("SDK shard reuse prompt differs from current shard prompt")
        raw_payload = json.loads(raw.decode("utf-8"))
        if not isinstance(raw_payload, dict) or set(raw_payload) != {"methods"}:
            raise RuntimeError("SDK shard reuse raw response violates methods-only contract")
        if not isinstance(raw_payload.get("methods"), str):
            raise RuntimeError("SDK shard reuse raw methods payload is not a string")
        if verdict.get("status") != "admitted" or verdict.get("exact_method_set_match") is not True:
            raise RuntimeError("SDK shard reuse validation verdict is not exact/admitted")
        if verdict.get("expected_method_names") != expected:
            raise RuntimeError("SDK shard reuse verdict expected-method mismatch")
        if verdict.get("missing_method_names") or verdict.get("unexpected_method_names"):
            raise RuntimeError("SDK shard reuse verdict contains coverage differences")
        current_verdict = self._sdk_method_coverage_verdict(shard, cleaned)
        if not current_verdict["exact_method_set_match"]:
            raise RuntimeError("SDK shard reuse cleaned body fails current exact-method gate")
        if self._clean_generated_method_body(raw_payload["methods"]) != cleaned:
            raise RuntimeError("SDK shard reuse cleaned body does not derive from raw response")
        return cleaned

    def _validate_sdk_shard_prompt_only_reuse_decision(
        self, decision_ref: Dict[str, Any], shard: Dict[str, Any]
    ) -> str:
        decision_payload = self._read_and_verify_sdk_artifact_ref(
            decision_ref, "prompt_only_reuse_decision"
        )
        decision = json.loads(decision_payload.decode("utf-8"))
        if decision.get("schema") != "ghostweb.sdk-shard-prompt-only-reuse-decision.v1":
            raise RuntimeError("SDK shard prompt-only reuse decision schema mismatch")
        if decision.get("status") != "reuse_admitted" or decision.get("manifest_last") is not True:
            raise RuntimeError("SDK shard prompt-only reuse decision is not admitted/manifest-last")
        if decision.get("changed_dependencies") != ["prompt_guidance"]:
            raise RuntimeError("SDK shard reuse permits only the prompt_guidance dependency change")
        if decision.get("preserved_dependencies") != [
            "expected_top_level_method_names",
            "methods_only_response_schema",
            "raw_response_bytes",
            "cleaned_method_bytes",
            "exact_method_set_gate",
        ]:
            raise RuntimeError("SDK shard prompt-only reuse preserved-dependency contract mismatch")
        expected = self._expected_sdk_shard_method_names(shard)
        if decision.get("shard_id") != (shard or {}).get("shard_id", "sdk_methods"):
            raise RuntimeError("SDK shard prompt-only reuse shard identity mismatch")
        if decision.get("expected_method_names") != expected:
            raise RuntimeError("SDK shard prompt-only reuse expected-method binding mismatch")
        if decision.get("response_schema") != {
            "type": "object",
            "properties": {"methods": {"type": "string"}},
            "required": ["methods"],
            "additionalProperties": False,
        }:
            raise RuntimeError("SDK shard prompt-only reuse methods-only schema mismatch")

        source_manifest_ref = decision.get("source_attempt_manifest")
        self._read_and_verify_sdk_artifact_ref(
            source_manifest_ref, "source_attempt_manifest"
        )
        source_manifest_path = Path(source_manifest_ref["path"])
        source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
        source_artifacts = source_manifest.get("artifacts")
        if not isinstance(source_artifacts, dict):
            raise RuntimeError("SDK shard prompt-only reuse source manifest has no artifacts")
        source_prompt = self._read_and_verify_sdk_artifact_ref(
            source_artifacts.get("prompt"), "source_prompt"
        )
        source_prompt_sha256 = self._sha256_bytes(source_prompt)
        current_prompt = str((shard or {}).get("prompt", "")).encode("utf-8")
        current_prompt_sha256 = self._sha256_bytes(current_prompt)
        if decision.get("source_prompt_sha256") != source_prompt_sha256:
            raise RuntimeError("SDK shard prompt-only reuse source prompt SHA mismatch")
        if decision.get("target_prompt_sha256") != current_prompt_sha256:
            raise RuntimeError("SDK shard prompt-only reuse target prompt SHA mismatch")
        if source_prompt == current_prompt:
            raise RuntimeError("SDK shard prompt-only reuse decision does not bind a prompt change")
        source_shard = {
            "shard_id": decision["shard_id"],
            "methods": expected,
            "helper_functions": [],
            "prompt": source_prompt.decode("utf-8"),
        }
        cleaned = self._validate_sdk_shard_attempt_for_reuse(
            source_manifest_path, source_shard
        )
        current_verdict = self._sdk_method_coverage_verdict(shard, cleaned)
        if current_verdict.get("exact_method_set_match") is not True:
            raise RuntimeError("SDK shard prompt-only reuse fails current exact-method gate")
        if decision.get("cleaned_methods_sha256") != self._sha256_text(cleaned):
            raise RuntimeError("SDK shard prompt-only reuse cleaned payload SHA mismatch")
        return cleaned

    def _assemble_sdk_shard_results(
        self,
        shards: List[Dict[str, Any]],
        shard_results: List[Any],
        prompt_only_reuse_decisions: Dict[str, Dict[str, Any]] | None = None,
    ) -> str:
        if len(shards) != len(shard_results):
            raise RuntimeError("SDK shard union cardinality mismatch")
        missing_ids = [
            str(shard.get("shard_id", "sdk_methods"))
            for shard, result in zip(shards, shard_results)
            if not isinstance(result, str) or not result.strip()
        ]
        if missing_ids:
            raise RuntimeError(
                "SDK method coverage incomplete; shard union missing payloads={}".format(
                    ", ".join(missing_ids)
                )
            )

        root = self._sdk_shard_artifact_root()
        admitted_refs = []
        reused_refs = []
        prompt_only_reuse_decisions = prompt_only_reuse_decisions or {}
        if root is not None:
            for shard, result in zip(shards, shard_results):
                shard_id = str(shard.get("shard_id", "sdk_methods"))
                ref = self._sdk_shard_latest_attempts.get(shard_id)
                if isinstance(ref, dict):
                    self._read_and_verify_sdk_artifact_ref(ref, "attempt_manifest")
                    validated = self._validate_sdk_shard_attempt_for_reuse(
                        ref["path"], shard
                    )
                    admitted_refs.append(ref)
                else:
                    reuse_ref = prompt_only_reuse_decisions.get(shard_id)
                    if not isinstance(reuse_ref, dict):
                        raise RuntimeError(
                            "SDK shard union incomplete; no admitted manifest or prompt-only reuse decision for {}".format(
                                shard_id
                            )
                        )
                    validated = self._validate_sdk_shard_prompt_only_reuse_decision(
                        reuse_ref, shard
                    )
                    reused_refs.append(reuse_ref)
                if validated != result:
                    raise RuntimeError(
                        "SDK shard union payload differs from admitted evidence for {}".format(
                            shard_id
                        )
                    )

        methods = "\n\n".join(result.strip() for result in shard_results)


        methods = self._normalize_generated_javascript_syntax(methods)
        methods = self._normalize_redeclared_const_after_guard_return(methods)
        expected = []
        for shard in shards:
            expected.extend(self._expected_sdk_shard_method_names(shard))
        emitted = sorted(self._extract_generated_method_names(methods))
        if set(expected) != set(emitted):
            raise RuntimeError(
                "SDK shard union exact-method gate failed; missing={} unexpected={}".format(
                    sorted(set(expected) - set(emitted)),
                    sorted(set(emitted) - set(expected)),
                )
            )

        if root is not None:
            union_path = root.parent / "sdk_shard_union_manifest.json"
            if union_path.exists():
                raise RuntimeError(
                    "immutable SDK shard union manifest already exists: {}".format(
                        union_path
                    )
                )
            union_path.parent.mkdir(parents=True, exist_ok=True)
            payload = self._canonical_json_bytes(
                {
                    "schema": "ghostweb.sdk-shard-union-manifest.v1",
                    "status": "complete",
                    "manifest_last": True,
                    "required_shard_ids": [
                        shard.get("shard_id", "sdk_methods") for shard in shards
                    ],
                    "admitted_shard_attempt_manifests": admitted_refs,
                    "prompt_only_reuse_decisions": reused_refs,
                    "expected_method_names": expected,
                    "emitted_method_names": emitted,
                    "missing_method_names": [],
                    "unexpected_method_names": [],
                    "assembled_methods_sha256": self._sha256_text(methods),
                    "assembled_methods_size_bytes": len(methods.encode("utf-8")),
                }
            )
            fd, temporary_name = tempfile.mkstemp(
                prefix=".sdk_shard_union_manifest.json-", dir=str(union_path.parent)
            )
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_name, union_path)
            except Exception:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass
                raise
        return methods

    def _clean_generated_method_body(self, methods: str) -> str:
        cleaned = (methods or "").strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:javascript|js)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned).strip()
        if cleaned.startswith('class '):
            brace_start = cleaned.find('{')
            if brace_start > 0:
                cleaned = cleaned[brace_start + 1:]
                if cleaned.rstrip().endswith('}'):
                    cleaned = cleaned.rstrip()[:-1]
        lines = cleaned.split('\n')
        cleaned_lines = [
            line for line in lines
            if not line.strip().startswith('module.exports')
            and not line.strip().startswith('if (typeof window')
        ]
        return self._normalize_generated_javascript_syntax('\n'.join(cleaned_lines).strip())

    def _normalize_generated_javascript_syntax(self, code: str) -> str:
        cleaned = self._normalize_nullish_boolean_precedence(code or "")
        cleaned = self._drop_unreachable_scalar_fallback_that_closes_helper_before_type_branches(cleaned)
        cleaned = self._normalize_extra_binary_continuation_close_parens(cleaned)
        cleaned = self._normalize_missing_callback_call_close_before_binary_continuation(cleaned)
        cleaned = self._normalize_missing_call_close_before_binary_continuation(cleaned)
        cleaned = self._normalize_extra_ternary_close_parens(cleaned)
        cleaned = self._normalize_missing_ternary_group_close_before_binary_continuation(cleaned)
        cleaned = self._normalize_missing_group_close_before_index_fallback(cleaned)
        cleaned = self._normalize_missing_group_close_before_literal_fallback(cleaned)
        cleaned = self._normalize_broken_regex_char_classes(cleaned)
        cleaned = self._drop_invalid_generated_line_continuations(cleaned)
        cleaned = self._drop_split_identifier_duplicate_stubs(cleaned)
        cleaned = self._join_split_method_identifier(cleaned)
        cleaned = self._normalize_trailing_commas_after_class_methods(cleaned)
        return cleaned

    def _normalize_redeclared_const_after_guard_return(self, code: str) -> str:
        source_lines = (code or "").splitlines()
        if not source_lines:
            return code or ""

        declaration_re = re.compile(
            r"^(?P<indent>[ \t]*)const\s+"
            r"(?P<name>[A-Za-z_$][\w$]*)\s*=\s*.+;\s*$"
        )
        declarations: Dict[Tuple[int, str, str], int] = {}
        significant_since: Dict[Tuple[int, str, str], List[int]] = {}
        brace_depth = 0

        for index, line in enumerate(source_lines):
            leading_close = re.match(r"^[ \t]*(?P<braces>}+)", line)
            if leading_close:
                closed_scope_floor = brace_depth - len(leading_close.group("braces")) + 1
                for key in list(declarations):
                    if key[0] >= closed_scope_floor:
                        declarations.pop(key, None)
                        significant_since.pop(key, None)

            stripped = line.strip()
            match = declaration_re.match(line)
            if match:
                name = match.group("name")
                indent = match.group("indent")
                key = (brace_depth, indent, name)
                prior_index = declarations.get(key)
                between_indexes = significant_since.get(key, [])
                if prior_index is not None and len(between_indexes) == 1:
                    guard_line = source_lines[between_indexes[0]].strip()
                    if re.fullmatch(
                        r"if\s*\([^{{}};\n]*\)\s*return\s+{}\s*;".format(
                            re.escape(name)
                        ),
                        guard_line,
                    ):
                        source_lines[prior_index] = re.sub(
                            r"^(?P<indent>[ \t]*)const\b",
                            r"\g<indent>let",
                            source_lines[prior_index],
                            count=1,
                        )
                        source_lines[index] = re.sub(
                            r"^(?P<indent>[ \t]*)const\s+{}\s*=".format(
                                re.escape(name)
                            ),
                            r"\g<indent>{} =".format(name),
                            source_lines[index],
                            count=1,
                        )
                        significant_since[key] = []
                    else:
                        declarations[key] = index
                        significant_since[key] = []
                else:
                    declarations[key] = index
                    significant_since[key] = []

            if stripped and not stripped.startswith("//"):
                for key, prior_index in list(declarations.items()):
                    if prior_index != index:
                        significant_since.setdefault(key, []).append(index)

            brace_depth += self._brace_balance(line)
            for key in list(declarations):
                if key[0] > brace_depth:
                    declarations.pop(key, None)
                    significant_since.pop(key, None)

        return "\n".join(source_lines)

    def _drop_unreachable_scalar_fallback_that_closes_helper_before_type_branches(self, code: str) -> str:
        source_lines = (code or "").splitlines()
        if not source_lines:
            return code or ""

        def next_non_empty_index(start: int) -> int:
            for candidate in range(start, len(source_lines)):
                if source_lines[candidate].strip():
                    return candidate
            return -1

        def has_open_arrow_helper_for_arg(line_index: int, arg_name: str) -> bool:
            helper_re = re.compile(
                r"^[ \t]*const\s+[A-Za-z_$][\w$]*\s*=\s*"
                r"\(\s*{}\s*\)\s*=>\s*\{{\s*$".format(re.escape(arg_name))
            )
            for candidate in range(line_index - 1, max(-1, line_index - 120), -1):
                stripped = source_lines[candidate].strip()
                if helper_re.match(source_lines[candidate]):
                    return True
                if stripped == "};":
                    return False
            return False

        return_re = re.compile(
            r"^[ \t]*return\s+[A-Za-z_$][\w$]*\(\s*(?P<arg>[A-Za-z_$][\w$]*)\s*\)\s*;\s*$"
        )
        output_lines = []
        skip_indices = set()
        for index, line in enumerate(source_lines):
            if index in skip_indices:
                continue
            match = return_re.match(line)
            if match:
                arg_name = match.group("arg")
                close_index = next_non_empty_index(index + 1)
                branch_index = next_non_empty_index(close_index + 1) if close_index >= 0 else -1
                branch_line = source_lines[branch_index] if branch_index >= 0 else ""
                branch_re = re.compile(
                    r"^[ \t]*if\s*\(\s*Array\.isArray\(\s*{}\s*\)\s*\)\s*\{{\s*$".format(
                        re.escape(arg_name)
                    )
                )
                if (
                    close_index >= 0
                    and source_lines[close_index].strip() == "}"
                    and branch_re.match(branch_line)
                    and has_open_arrow_helper_for_arg(index, arg_name)
                ):
                    skip_indices.add(close_index)
                    continue
            output_lines.append(line)
        return "\n".join(output_lines)


    def _normalize_missing_group_close_before_index_fallback(self, code: str) -> str:
        lines = []
        statement_paren_balance = 0
        for line in (code or "").splitlines():
            stripped = line.strip()
            if statement_paren_balance > 0 and re.match(r"^\]\s*(?:\|\||&&|\?\?)", stripped):
                close_count = statement_paren_balance
                line = re.sub(r"^([ \t]*)\]", r"\1" + (")" * close_count) + "]", line, count=1)
            lines.append(line)
            statement_paren_balance += self._paren_balance(line)
            if stripped.endswith((";", ",")) and statement_paren_balance <= 0:
                statement_paren_balance = 0
        return "\n".join(lines)


    def _join_split_method_identifier(self, code: str) -> str:
        method_split_re = re.compile(
            r"^(?P<indent>\s*)(?P<prefix>[A-Za-z_])\s+(?P<tail>[A-Za-z_][A-Za-z0-9_]*)\s*"
            r"(?P<rest>\([^)]*\)\s*\{\s*)$"
        )
        out_lines: List[str] = []
        for line in (code or "").splitlines():
            match = method_split_re.match(line)
            if match:
                joined_name = match.group("prefix") + match.group("tail")
                out_lines.append(
                    f"{match.group('indent')}{joined_name}{match.group('rest')}"
                )
            else:
                out_lines.append(line)
        return "\n".join(out_lines)

    def _normalize_trailing_commas_after_class_methods(self, code: str) -> str:
        source_lines = (code or "").splitlines()
        if not source_lines:
            return code or ""

        def next_significant_line(start: int) -> str:
            in_block_comment = False
            for candidate in source_lines[start:]:
                stripped = candidate.strip()
                if not stripped:
                    continue
                if in_block_comment:
                    if "*/" in stripped:
                        in_block_comment = False
                    continue
                if stripped.startswith("/*"):
                    if "*/" not in stripped:
                        in_block_comment = True
                    continue
                if stripped.startswith("//"):
                    continue
                return stripped
            return ""

        method_decl_re = re.compile(
            r"^(?:async\s+)?(?:get\s+|set\s+)?[A-Za-z_$][\w$]*\s*\([^;{}]*\)\s*\{\s*$"
        )

        output_lines: List[str] = []
        brace_depth = 0
        for index, line in enumerate(source_lines):
            stripped = line.strip()
            candidate_line = line
            line_delta = self._brace_balance(line)
            depth_after_line = brace_depth + line_delta
            if stripped == "}," and depth_after_line in (0, 1):
                next_line = next_significant_line(index + 1)
                if not next_line or next_line == "}" or method_decl_re.match(next_line):
                    comma_index = candidate_line.rfind(",")
                    if comma_index >= 0:
                        candidate_line = candidate_line[:comma_index] + candidate_line[comma_index + 1:]
            output_lines.append(candidate_line)
            brace_depth += self._brace_balance(candidate_line)
        return "\n".join(output_lines)

    def _normalize_missing_callback_call_close_before_binary_continuation(self, code: str) -> str:
        lines = []
        paren_balance = 0
        for line in (code or "").splitlines():
            stripped = line.strip()
            match = re.fullmatch(r"}\s*(?P<op>\|\||&&|\?\?)\s*", stripped or "")
            if paren_balance > 0 and match:
                indent = line[: len(line) - len(line.lstrip())]
                line = "{}{}) {}".format(indent, "}", match.group("op"))
            lines.append(line)
            paren_balance += self._paren_balance(line)
            if stripped.endswith(";") and paren_balance < 0:
                paren_balance = 0
        return "\n".join(lines)

    def _normalize_missing_call_close_before_binary_continuation(self, code: str) -> str:
        lines = []
        paren_balance = 0
        previous_non_empty = ""
        for line in (code or "").splitlines():
            stripped = line.strip()
            if (
                paren_balance > 0
                and re.fullmatch(r"(?:\|\||&&|\?\?)", stripped or "")
                and previous_non_empty
                and not re.search(r"(?:\|\||&&|\?\?|[?:,([{])\s*$", previous_non_empty)
            ):
                line = "{}{} {}".format(
                    line[: len(line) - len(line.lstrip())],
                    ")",
                    stripped,
                )
            lines.append(line)
            paren_balance += self._paren_balance(line)
            if stripped:
                previous_non_empty = stripped
            if stripped.endswith(";") and paren_balance < 0:
                paren_balance = 0
        return "\n".join(lines)

    def _normalize_missing_ternary_group_close_before_binary_continuation(self, code: str) -> str:
        lines = []
        paren_balance = 0
        statement_has_ternary = False
        for line in (code or "").splitlines():
            if "?" in line:
                statement_has_ternary = True
            if statement_has_ternary and paren_balance > 0:
                match = re.match(
                    r"^(?P<indent>\s*:\s*)(?P<expr>.+?)(?P<space>\s+)(?P<op>\|\||&&|\?\?)\s*$",
                    line,
                )
                if match:
                    expr = match.group("expr").rstrip()
                    if not expr.endswith(")"):
                        line = "{}{}) {}".format(match.group("indent"), expr, match.group("op"))
            lines.append(line)
            paren_balance += self._paren_balance(line)
            stripped = line.strip()
            if stripped.endswith(";") and paren_balance <= 0:
                paren_balance = 0
                statement_has_ternary = False
        return "\n".join(lines)

    def _normalize_missing_group_close_before_literal_fallback(self, code: str) -> str:
        source_lines = (code or "").splitlines()
        lines = []
        paren_balance = 0
        statement_paren_balance = 0
        for index, line in enumerate(source_lines):
            stripped = line.strip()
            if re.match(r"^(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*=", stripped):
                statement_paren_balance = 0
            next_non_empty = ""
            for candidate in source_lines[index + 1:]:
                if candidate.strip():
                    next_non_empty = candidate.strip()
                    break
            match = re.search(r"\)(?P<space>\s*)(?P<op>\|\||&&|\?\?)\s*$", line)
            if (
                statement_paren_balance > 0
                and match
                and self._paren_balance(line) == 0
                and re.fullmatch(
                    r"(?:''|\"\"|``|null|undefined|false|true|\[\]|\{\})\s*;",
                    next_non_empty,
                )
            ):
                close_index = match.start()
                line = line[:close_index + 1] + ")" + line[close_index + 1:]
            lines.append(line)
            line_balance = self._paren_balance(line)
            paren_balance += line_balance
            statement_paren_balance += line_balance
            if stripped.endswith(";") and paren_balance <= 0:
                paren_balance = 0
            if stripped.endswith(";"):
                statement_paren_balance = 0
        return "\n".join(lines)

    def _drop_invalid_generated_line_continuations(self, code: str) -> str:
        lines = []
        for line in (code or "").splitlines():
            right_stripped = line.rstrip()
            if (
                right_stripped.endswith("\\")
                and self._line_tail_is_outside_js_literal_or_comment(right_stripped[:-1])
            ):
                trailing_space = line[len(right_stripped):]
                line = right_stripped[:-1] + trailing_space
            lines.append(line)
        return "\n".join(lines)

    def _line_tail_is_outside_js_literal_or_comment(self, line: str) -> bool:
        quote = ""
        escaped = False
        block_comment = False
        index = 0
        while index < len(line):
            ch = line[index]
            nxt = line[index + 1] if index + 1 < len(line) else ""
            if block_comment:
                if ch == "*" and nxt == "/":
                    block_comment = False
                    index += 2
                    continue
                index += 1
                continue
            if quote:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == quote:
                    quote = ""
                index += 1
                continue
            if ch == "/" and nxt == "/":
                return False
            if ch == "/" and nxt == "*":
                block_comment = True
                index += 2
                continue
            if ch in ("'", '"', "`"):
                quote = ch
            index += 1
        return quote == "" and not block_comment

    def _drop_split_identifier_duplicate_stubs(self, code: str) -> str:
        stub_pattern = re.compile(
            r"(?ms)^(?P<indent>[ \t]*)(?P<head>[A-Za-z_$][\w$]*)\s+"
            r"(?P<tail>[A-Za-z_$][\w$]*)\s*(?P<params>\([^)]*\))\s*\{"
            r"(?P<body>(?://[^\n]*(?:\n|$)|/\*.*?\*/|\s)*)\}\s*\n*"
        )

        def replace(match):
            candidate = "{}{}".format(match.group("head"), match.group("tail"))
            remaining = code[match.end():]
            if re.search(r"(?m)^[ \t]*{}\s*\(".format(re.escape(candidate)), remaining):
                return ""
            return match.group(0)

        return stub_pattern.sub(replace, code or "")

    def _normalize_broken_regex_char_classes(self, code: str) -> str:
        def fix(match):
            chars = re.sub(r"\s+", "", match.group("chars") or "")
            return "/[^{}]/{}".format(chars, match.group("flags") or "")

        return re.sub(
            r"/\[\^\s*\n\s*(?P<chars>[^\]]+)\]/(?P<flags>[a-z]*)",
            fix,
            code or "",
        )

    def _normalize_nullish_boolean_precedence(self, code: str) -> str:
        def fix_string_call(match):
            expr = match.group(1)
            if "??" not in expr or ("||" not in expr and "&&" not in expr):
                return match.group(0)
            left, right = expr.split("??", 1)
            if "||" in left or "&&" in left:
                return "String(({}) ?? {})".format(left.strip(), right.strip())
            for operator in ("||", "&&"):
                if operator in right:
                    right_left, right_right = right.split(operator, 1)
                    return "String(({} ?? {}) {} {})".format(
                        left.strip(),
                        right_left.strip(),
                        operator,
                        right_right.strip(),
                    )
            return match.group(0)

        return re.sub(
            r"String\(([^()\n;]*(?:\?\?[^()\n;]*(?:\|\||&&)|(?:\|\||&&)[^()\n;]*\?\?)[^()\n;]*)\)",
            fix_string_call,
            code or "",
        )

    def _normalize_extra_binary_continuation_close_parens(self, code: str) -> str:
        lines = []
        for line in (code or "").splitlines():
            lines.append(self._normalize_extra_binary_continuation_close_parens_line(line))
        return "\n".join(lines)

    def _normalize_extra_binary_continuation_close_parens_line(self, line: str) -> str:
        if self._paren_balance(line) >= 0:
            return line
        if not re.search(r"\)\s*(?:\|\||&&|\?\?)\s*$", line or ""):
            return line

        repaired = line
        while self._paren_balance(repaired) < 0:
            matches = [
                match
                for match in re.finditer(r"\)(?=\s*(?:\|\||&&|\?\?)\s*$)", repaired)
                if self._paren_balance(repaired[:match.start()]) == 0
            ]
            if not matches:
                break
            close_index = matches[-1].start()
            repaired = repaired[:close_index] + repaired[close_index + 1:]
        return repaired

    def _normalize_extra_ternary_close_parens(self, code: str) -> str:
        lines = []
        for line in (code or "").splitlines():
            lines.append(self._normalize_extra_ternary_close_parens_line(line))
        return "\n".join(lines)

    def _normalize_extra_ternary_close_parens_line(self, line: str) -> str:
        if "?" not in line or ":" not in line:
            return line
        colon_index = line.rfind(":")
        before = line[:colon_index]
        after = line[colon_index:]
        while before.rstrip().endswith(")") and self._paren_balance(before) < 0:
            close_index = before.rfind(")")
            before = before[:close_index] + before[close_index + 1:]
        return before + after

    def _paren_balance(self, text: str) -> int:
        balance = 0
        quote = ""
        escaped = False
        for ch in text or "":
            if escaped:
                escaped = False
                continue
            if quote:
                if ch == "\\":
                    escaped = True
                elif ch == quote:
                    quote = ""
                continue
            if ch in ("'", '"', "`"):
                quote = ch
                continue
            if ch == "(":
                balance += 1
            elif ch == ")":
                balance -= 1
        return balance

    def _brace_balance(self, text: str) -> int:
        balance = 0
        quote = ""
        escaped = False
        block_comment = False
        index = 0
        while index < len(text or ""):
            ch = text[index]
            nxt = text[index + 1] if index + 1 < len(text or "") else ""
            if block_comment:
                if ch == "*" and nxt == "/":
                    block_comment = False
                    index += 2
                    continue
                index += 1
                continue
            if quote:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == quote:
                    quote = ""
                index += 1
                continue
            if ch == "/" and nxt == "/":
                break
            if ch == "/" and nxt == "*":
                block_comment = True
                index += 2
                continue
            if ch in ("'", '"', "`"):
                quote = ch
                index += 1
                continue
            if ch == "{":
                balance += 1
            elif ch == "}":
                balance -= 1
            index += 1
        return balance

    def _extract_generated_method_names(self, methods: str) -> set:
        names = set()
        brace_depth = 0
        quote = ""
        escaped = False
        block_comment = False
        method_decl_re = re.compile(
            r"^\s*(?:(?:async|static|get|set)\s+)*([A-Za-z_$][\w$]*)\s*\("
        )
        for line in (methods or "").splitlines():
            visible = []
            index = 0
            while index < len(line):
                ch = line[index]
                nxt = line[index + 1] if index + 1 < len(line) else ""
                if block_comment:
                    visible.append(" ")
                    if ch == "*" and nxt == "/":
                        visible.append(" ")
                        block_comment = False
                        index += 2
                        continue
                    index += 1
                    continue
                if quote:
                    visible.append(" ")
                    if escaped:
                        escaped = False
                    elif ch == "\\":
                        escaped = True
                    elif ch == quote:
                        quote = ""
                    index += 1
                    continue
                if ch == "/" and nxt == "/":
                    visible.extend(" " for _ in line[index:])
                    break
                if ch == "/" and nxt == "*":
                    visible.extend((" ", " "))
                    block_comment = True
                    index += 2
                    continue
                if ch in ("'", '"', "`"):
                    visible.append(" ")
                    quote = ch
                    index += 1
                    continue
                visible.append(ch)
                index += 1
            visible_line = "".join(visible)
            if brace_depth == 0:
                match = method_decl_re.match(visible_line)
                if match:
                    names.add(match.group(1))
            brace_depth += visible_line.count("{") - visible_line.count("}")
        return names

    def _find_missing_interface_methods(self,
                                        method_contracts: List[Dict[str, Any]],
                                        generated_methods: str) -> List[str]:
        emitted = self._extract_generated_method_names(generated_methods)
        missing = []
        for method in method_contracts:
            name = method.get("name", "")
            if not name or name in emitted:
                continue
            missing.append(name)
        return missing

    def _assert_generated_method_body_coverage(
        self, shard: Dict[str, Any], generated_methods: str
    ) -> Dict[str, Any]:
        verdict = self._sdk_method_coverage_verdict(shard, generated_methods)
        missing = verdict["missing_method_names"]
        unexpected = verdict["unexpected_method_names"]
        if missing or unexpected:
            details = []
            if missing:
                details.append("missing required method bodies: {}".format(", ".join(missing)))
            if unexpected:
                details.append("unexpected method bodies: {}".format(", ".join(unexpected)))
            raise RuntimeError(
                "SDK method shard {} {}".format(
                    (shard or {}).get("shard_id", "sdk_methods"),
                    "; ".join(details),
                )
            )
        return verdict

    def _select_sdk_snapshot_records(self, records: List[Any], max_records: int = 12) -> List[Any]:
        if not isinstance(records, list):
            return []
        if len(records) <= max_records:
            return records

        selected_indexes = []
        seen_fields = set()

        def add_index(index: int) -> None:
            if index in selected_indexes or index < 0 or index >= len(records):
                return
            selected_indexes.append(index)
            item = records[index]
            if isinstance(item, dict):
                seen_fields.update(str(key) for key in item.keys())

        add_index(0)
        add_index(1)

        while len(selected_indexes) < max_records:
            best_index = -1
            best_score = 0
            for index, item in enumerate(records):
                if index in selected_indexes:
                    continue
                if isinstance(item, dict):
                    keys = set(str(key) for key in item.keys())
                    unseen = keys - seen_fields
                    text_richness = sum(
                        1 for value in item.values()
                        if isinstance(value, str) and len(value.strip()) >= 4
                    )
                    score = len(unseen) * 10 + len(keys) + min(text_richness, 12)
                else:
                    score = 1
                if score > best_score:
                    best_score = score
                    best_index = index
            if best_index < 0:
                break
            add_index(best_index)
            if best_score <= 1:
                break

        return [records[index] for index in selected_indexes]

    def _build_sdk_data_snapshot(self, entity_schema: Dict[str, Any], init_data: Dict[str, Any]) -> Dict[str, Any]:
        if not init_data:
            return {}
        key_map = self._extract_storage_key_map(entity_schema)
        snapshot = {}
        for key, data in init_data.items():
            if key.startswith("_"):
                continue
            storage_key = key_map.get(key, key)
            if isinstance(data, list) and data:
                preview = []
                for i, item in enumerate(self._select_sdk_snapshot_records(data)):
                    item_copy = dict(item) if isinstance(item, dict) else item
                    if isinstance(item_copy, dict) and 'id' not in item_copy:
                        item_copy['id'] = "{}_{}".format(storage_key, i)
                    preview.append(item_copy)
                snapshot[storage_key] = preview
            elif isinstance(data, dict):
                snapshot[storage_key] = data
        return snapshot

    def _build_sdk_implementation_view(self,
                                       entity_schema: Dict[str, Any],
                                       interfaces: Dict[str, Any],
                                       storage_schema: Dict[str, Any]) -> Dict[str, Any]:
        entity_names = sorted(storage_schema.keys(), key=len, reverse=True)
        page_assignments = interfaces.get("page_assignments", {}) if isinstance(interfaces, dict) else {}
        method_to_pages = self._invert_page_assignments(page_assignments)
        methods_by_name = {}
        for iface in interfaces.get("interfaces", []) if isinstance(interfaces, dict) else []:
            if not isinstance(iface, dict):
                continue
            if self._is_deterministic_sdk_interface(iface, entity_names):
                continue
            compact = self._compact_sdk_interface(iface, entity_names, method_to_pages)
            if compact:
                name = compact.get("name", "")
                if name in methods_by_name:
                    methods_by_name[name] = self._merge_compact_sdk_methods(methods_by_name[name], compact)
                else:
                    compact["source_count"] = 1
                    methods_by_name[name] = compact
        return {
            "version": "sdk_implementation_view",
            "methods": list(methods_by_name.values()),
        }

    def _is_deterministic_sdk_interface(self, iface: Dict[str, Any], entity_names: List[str]) -> bool:
        operation = iface.get("operation") or self._infer_interface_operation(iface)
        deterministic_operations = set([
            "list",
            "list_page_model",
            "form_page_model",
            "get",
            "create",
            "update",
            "delete",
            "set_default",
            "save",
            "submit",
            "select",
            "apply",
            "create_record_draft",
            "update_current_draft",
            "commit_draft",
        ])
        if operation not in deterministic_operations:
            return False
        entity = iface.get("entity") or self._infer_interface_entity(iface, entity_names)
        return bool(entity and entity in set(entity_names))

    def _merge_compact_sdk_methods(self, base: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(base)
        merged["source_count"] = int(merged.get("source_count", 1)) + 1
        for key in ("entity", "operation", "category"):
            if not merged.get(key) and incoming.get(key):
                merged[key] = incoming.get(key)
        pages = []
        for value in (base.get("pages") or []) + (incoming.get("pages") or []):
            if value and value not in pages:
                pages.append(value)
        if pages:
            merged["pages"] = pages[:8]
        if not merged.get("parameters") and incoming.get("parameters"):
            merged["parameters"] = incoming.get("parameters")
        if not merged.get("returns") and incoming.get("returns"):
            merged["returns"] = incoming.get("returns")
        return merged

    def _invert_page_assignments(self, page_assignments: Any) -> Dict[str, List[str]]:
        result = {}
        if not isinstance(page_assignments, dict):
            return result
        for page, methods in page_assignments.items():
            if not isinstance(methods, list):
                continue
            for method in methods:
                if not method:
                    continue
                result.setdefault(method, []).append(page)
        return result

    def _compact_sdk_interface(self,
                               iface: Dict[str, Any],
                               entity_names: List[str],
                               method_to_pages: Dict[str, List[str]]) -> Dict[str, Any]:
        name = iface.get("name", "")
        if not name:
            return {}
        params = []
        for param in iface.get("parameters", []) or []:
            if not isinstance(param, dict):
                continue
            compact_param = {
                "name": param.get("name", ""),
                "type": param.get("type", "any"),
            }
            if param.get("required") is not None:
                compact_param["required"] = param.get("required")
            entity_ref = param.get("entityReference")
            if isinstance(entity_ref, dict) and entity_ref.get("entity"):
                compact_param["entity"] = entity_ref.get("entity")
            params.append(compact_param)

        entity = iface.get("entity") or self._infer_interface_entity(iface, entity_names)
        compact = {
            "name": name,
            "parameters": params,
            "returns": self._summarize_return_schema(iface.get("returns")),
        }
        if entity:
            compact["entity"] = entity
        operation = iface.get("operation") or self._infer_interface_operation(iface)
        if operation:
            compact["operation"] = operation
        if iface.get("category"):
            compact["category"] = iface.get("category")
        pages = method_to_pages.get(name) or []
        if pages:
            compact["pages"] = pages[:3]
        return compact

    def _infer_interface_entity(self, iface: Dict[str, Any], entity_names: List[str]) -> str:
        for param in iface.get("parameters", []) or []:
            if isinstance(param, dict):
                entity_ref = param.get("entityReference")
                if isinstance(entity_ref, dict) and entity_ref.get("entity"):
                    return entity_ref.get("entity")
        for alias in self._interface_return_aliases(iface):
            normalized_alias = self._normalize_entity_match_token(alias)
            if not normalized_alias:
                continue
            for entity_name in entity_names:
                if normalized_alias in self._entity_match_tokens(entity_name):
                    return entity_name
        method_name = iface.get("name", "")
        normalized_name = self._normalize_entity_match_token(method_name)
        for entity_name in entity_names:
            if self._normalize_entity_match_token(entity_name) in normalized_name:
                return entity_name
        return ""

    def _interface_return_aliases(self, iface: Dict[str, Any]) -> List[str]:
        returns = iface.get("returns", {}) if isinstance(iface, dict) else {}
        if not isinstance(returns, dict):
            return []
        props = returns.get("properties", {})
        if not isinstance(props, dict):
            return []
        aliases = []
        for name, spec in props.items():
            if not isinstance(spec, dict):
                continue
            spec_type = spec.get("type")
            if isinstance(spec_type, list):
                type_values = {str(item) for item in spec_type}
            elif isinstance(spec_type, str):
                type_values = {spec_type}
            else:
                type_values = set()
            if type_values & {"array", "object"}:
                aliases.append(str(name or ""))
        return aliases

    def _normalize_entity_match_token(self, value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())

    def _entity_match_tokens(self, entity_name: str) -> set:
        stem = "".join(ch for ch in str(entity_name or "") if ch.isalnum()) or str(entity_name or "")
        plural = self._camel_plural(stem)
        tokens = {
            entity_name,
            stem,
            plural,
            self._lower_camel(stem),
            self._lower_camel(plural),
            self._snake_case(entity_name),
            self._snake_case(plural),
        }
        return {self._normalize_entity_match_token(token) for token in tokens if token}

    def _infer_interface_operation(self, iface: Dict[str, Any]) -> str:
        name = iface.get("name", "")
        lowered = name.lower()
        if lowered.startswith("create") and "draft" in lowered:
            return "create_record_draft"
        if lowered.startswith("update") and "draft" in lowered:
            return "update_current_draft"
        if lowered.startswith("save") and "draft" in lowered:
            return "commit_draft"
        if name.startswith("list"):
            return "list"
        if name.startswith("create"):
            return "create"
        if name.startswith("update"):
            return "update"
        if name.startswith("delete") or name.startswith("remove"):
            return "delete"
        if name.startswith("setDefault"):
            return "set_default"
        if name.startswith("save"):
            return "save"
        if name.startswith("submit"):
            return "submit"
        if name.startswith("select"):
            return "select"
        if name.startswith("apply"):
            return "apply"
        if name.startswith("get") and name.endswith("ListPageModel"):
            return "list_page_model"
        if name.startswith("get") and name.endswith("FormPageModel"):
            return "form_page_model"
        if name.startswith("get") and name.endswith("Detail"):
            return "get"
        return ""

    def _summarize_return_schema(self, schema: Any, depth: int = 0) -> Any:
        if not isinstance(schema, dict):
            return schema
        result = {}
        schema_type = schema.get("type")
        if schema_type:
            result["type"] = schema_type
        if depth >= 2:
            props = schema.get("properties")
            if isinstance(props, dict):
                result["keys"] = sorted(props.keys())[:20]
            return result
        props = schema.get("properties")
        if isinstance(props, dict):
            compact_props = {}
            for key, value in list(props.items())[:20]:
                compact_props[key] = self._summarize_return_schema(value, depth + 1)
            result["properties"] = compact_props
        items = schema.get("items")
        if isinstance(items, dict):
            result["items"] = self._summarize_return_schema(items, depth + 1)
        return result

    def _build_sdk_semantic_shard_contracts(self,
                                            implementation_view: Dict[str, Any],
                                            storage_schema: Dict[str, Any],
                                            data_snapshot: Dict[str, Any],
                                            trace_context: str,
                                            helper_functions: List[Dict[str, Any]],
                                            website_type: str = "") -> List[Dict[str, Any]]:
        methods = implementation_view.get("methods", [])
        if not methods:
            return []
        grouped = {}
        for method in methods:
            key = self._sdk_method_group_key(method)
            grouped.setdefault(key, []).append(method)

        ordered_groups = sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0]))
        contracts = []
        total = len(ordered_groups)
        for index, (group_key, group_methods) in enumerate(ordered_groups):
            scoped_storage = self._scope_storage_schema(storage_schema, group_methods)
            scoped_data = self._scope_data_snapshot(data_snapshot, scoped_storage)
            methods_field = self._compact_json({"interfaces": group_methods})
            storage_field = self._compact_json(scoped_storage)
            data_field = self._compact_json(scoped_data) if scoped_data else ""
            helper_field = self._compact_json(helper_functions) if helper_functions and index == 0 else ""
            contract_fields = {
                "semantic_contract": (
                    "Semantic Shard Contract:\n"
                    + self._compact_json({
                        "semantic_shard_id": self._semantic_shard_id(group_key, index),
                        "semantic_key": group_key,
                        "website_type": website_type or "mobile web app",
                        "method_names": [method.get("name", "") for method in group_methods],
                        "source_pages": self._collect_method_pages(group_methods),
                    })
                    + "\n\n"
                ),
                "storage_schema_json": "Storage Schema for this semantic shard:\n" + storage_field + "\n\n",
                "preloaded_data_snapshot_json": "Pre-loaded Data preview for this semantic shard:\n" + data_field + "\n\n" if data_field else "",
                "sdk_trace_prompt": (trace_context + "\n\n") if trace_context else "",
                "methods_json": "Methods to implement:\n" + methods_field + "\n\n",
                "helper_functions_json": "helperFunctions to implement:\n" + helper_field + "\n\n" if helper_field else "",
            }
            contract_text = "".join(contract_fields.values())
            field_stats = {}
            for field_name, field_value in contract_fields.items():
                field_stats[field_name] = {
                    "chars": len(field_value),
                    "bytes": len(field_value.encode("utf-8")),
                    "estimated_tokens": self._estimate_prompt_tokens(field_value),
                }
            estimated_tokens = self._estimate_prompt_tokens(contract_text)
            budget = int(os.environ.get("T2W_SDK_METHOD_CONTEXT_BUDGET", str(self.SDK_METHOD_CONTEXT_BUDGET)))
            contracts.append({
                "semantic_shard_id": self._semantic_shard_id(group_key, index),
                "semantic_key": group_key,
                "index": index,
                "total": total,
                "methods": group_methods,
                "method_names": [method.get("name", "") for method in group_methods],
                "source_pages": self._collect_method_pages(group_methods),
                "storage_schema": scoped_storage,
                "data_snapshot": scoped_data,
                "helper_functions": helper_functions if index == 0 else [],
                "estimated_tokens": estimated_tokens,
                "chars": len(contract_text),
                "prompt_fields": field_stats,
                "over_budget": estimated_tokens > budget,
                "budget": budget,
            })
        return contracts

    def _build_sdk_method_shards(self,
                                 semantic_shard_contracts: Any,
                                 storage_schema: Dict[str, Any],
                                 data_snapshot: Dict[str, Any],
                                 trace_context: str,
                                 helper_functions: List[Dict[str, Any]],
                                 website_type: str = "") -> List[Dict[str, Any]]:
        if isinstance(semantic_shard_contracts, dict):
            semantic_shard_contracts = self._build_sdk_semantic_shard_contracts(
                semantic_shard_contracts,
                storage_schema,
                data_snapshot,
                trace_context,
                helper_functions,
                website_type,
            )
        if not semantic_shard_contracts:
            return []
        shards = []
        for contract in semantic_shard_contracts:
            current = []
            group_methods = contract.get("methods", []) or []
            for method in group_methods:
                candidate = current + [method]
                if current and self._should_flush_sdk_shard(
                    candidate,
                    storage_schema,
                    data_snapshot,
                    trace_context,
                    contract.get("helper_functions", []) if not shards else [],
                ):
                    shards.append({
                        "semantic_shard_id": contract.get("semantic_shard_id", ""),
                        "semantic_key": contract.get("semantic_key", ""),
                        "methods": current,
                        "helper_functions": contract.get("helper_functions", []) if not shards else [],
                    })
                    current = [method]
                else:
                    current = candidate
            if current:
                shards.append({
                    "semantic_shard_id": contract.get("semantic_shard_id", ""),
                    "semantic_key": contract.get("semantic_key", ""),
                    "methods": current,
                    "helper_functions": contract.get("helper_functions", []) if not shards else [],
                })

        result = []
        total = len(shards)
        for index, shard in enumerate(shards):
            shard_methods = shard.get("methods", []) if isinstance(shard, dict) else shard
            shard_helper_functions = shard.get("helper_functions", []) if isinstance(shard, dict) else (helper_functions if index == 0 else [])
            scoped_storage = self._scope_storage_schema(storage_schema, shard_methods)
            scoped_data = self._scope_data_snapshot(data_snapshot, scoped_storage)
            prompt, fields = self._build_sdk_method_shard_prompt(
                shard_methods,
                scoped_storage,
                scoped_data,
                trace_context,
                shard_helper_functions,
                index,
                total,
                website_type,
            )
            result.append({
                "shard_id": "sdk_methods_{:03d}".format(index + 1),
                "semantic_shard_id": shard.get("semantic_shard_id", "") if isinstance(shard, dict) else "",
                "semantic_key": shard.get("semantic_key", "") if isinstance(shard, dict) else "",
                "index": index,
                "total": total,
                "methods": [method.get("name", "") for method in shard_methods],
                "helper_functions": shard_helper_functions,
                "prompt": prompt,
                "prompt_fields": fields,
                "estimated_tokens": self._estimate_prompt_tokens(prompt),
                "chars": len(prompt),
            })
        return result

    def _semantic_shard_id(self, group_key: str, index: int) -> str:
        slug = re.sub(r"[^A-Za-z0-9]+", "_", group_key or "misc").strip("_").lower()
        return "sdk_semantic_{:03d}_{}".format(index + 1, slug or "misc")

    def _collect_method_pages(self, methods: List[Dict[str, Any]]) -> List[str]:
        pages = []
        for method in methods or []:
            for page in method.get("pages", []) or []:
                if page and page not in pages:
                    pages.append(page)
        return pages[:12]

    def _sdk_method_group_key(self, method: Dict[str, Any]) -> str:
        if method.get("entity"):
            return "entity:" + method.get("entity")
        if method.get("category"):
            return "category:" + str(method.get("category"))
        name = method.get("name", "")
        match = re.match(r"^(get|list|create|update|delete|set|save|search|select|apply)([A-Z][A-Za-z0-9_]*)", name)
        if match:
            token = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", match.group(2)).split("_")[0].lower()
            return "prefix:" + token
        return "misc"

    def _should_flush_sdk_shard(self,
                                methods: List[Dict[str, Any]],
                                storage_schema: Dict[str, Any],
                                data_snapshot: Dict[str, Any],
                                trace_context: str,
                                helper_functions: List[Dict[str, Any]]) -> bool:
        max_methods = int(os.environ.get(
            "T2W_SDK_METHOD_MAX_INTERFACES_PER_SHARD",
            str(self.SDK_METHOD_MAX_INTERFACES_PER_SHARD),
        ))
        if len(methods) > max(1, max_methods):
            return True
        prompt, _fields = self._build_sdk_method_shard_prompt(
            methods,
            self._scope_storage_schema(storage_schema, methods),
            self._scope_data_snapshot(data_snapshot, self._scope_storage_schema(storage_schema, methods)),
            trace_context,
            helper_functions,
            0,
            1,
        )
        budget = int(os.environ.get("T2W_SDK_METHOD_CONTEXT_BUDGET", str(self.SDK_METHOD_CONTEXT_BUDGET)))
        return self._estimate_prompt_tokens(prompt) > budget

    def _scope_storage_schema(self, storage_schema: Dict[str, Any], methods: List[Dict[str, Any]]) -> Dict[str, Any]:
        entities = sorted(set(method.get("entity", "") for method in methods if method.get("entity")))
        if not entities:
            return {}
        return {
            entity: storage_schema.get(entity, {})
            for entity in entities
            if entity in storage_schema
        }

    def _scope_data_snapshot(self, data_snapshot: Dict[str, Any], scoped_storage: Dict[str, Any]) -> Dict[str, Any]:
        if not scoped_storage:
            return {}
        storage_keys = set(
            spec.get("storage_key")
            for spec in scoped_storage.values()
            if isinstance(spec, dict) and spec.get("storage_key")
        )
        return {
            key: value
            for key, value in data_snapshot.items()
            if key in storage_keys
        }

    @staticmethod
    def _sdk_method_generation_stability_fewshot() -> str:
        return (
            "## JavaScript Stability Fewshot\n"
            "Use these examples to preserve the exact top-level method contract.\n\n"
            "BAD: inventing an unlisted top-level helper changes the class API even when the JavaScript is valid:\n"
            "_getDraftMerchantIds(draft) { return []; }\n"
            "submitOrder(orderId) {\n"
            "  return { merchantIds: this._getDraftMerchantIds({ id: orderId }) };\n"
            "}\n\n"
            "GOOD: keep auxiliary logic local when its helper name is not in the expected top-level list:\n"
            "submitOrder(orderId) {\n"
            "  const getDraftMerchantIds = (draft) => Array.isArray(draft && draft.items) ? draft.items.map(item => item.merchantId).filter(Boolean) : [];\n"
            "  const draft = this._findByIdOrName(this._getFromStorage('orders', []), orderId);\n"
            "  return { merchantIds: getDraftMerchantIds(draft) };\n"
            "}\n\n"
            "Syntax rules:\n"
            "- Every method/helper fragment must be valid inside a class body and pass node --check after assembly.\n"
            "- Close all braces, parentheses, object literals, and template literals before starting the next method/helper.\n"
            "- Do not close a template literal with a single quote or a single-quoted string with a backtick.\n"
            "- When using ?? with || or &&, wrap the ?? expression in parentheses.\n\n"
        )

    def _build_sdk_method_shard_prompt(self,
                                       methods: List[Dict[str, Any]],
                                       storage_schema: Dict[str, Any],
                                       data_snapshot: Dict[str, Any],
                                       trace_context: str,
                                       helper_functions: List[Dict[str, Any]],
                                       shard_index: int,
                                       total_shards: int,
                                       website_type: str = "") -> Tuple[str, Dict[str, Dict[str, int]]]:
        expected_top_level_method_names = self._method_names_from_contract_rows(methods)
        expected_top_level_method_names.extend(
            self._method_names_from_contract_rows(helper_functions)
        )
        fields = {
            "fixed_intro": (
                "You are a JavaScript developer. Generate method implementations for a BusinessLogic class.\n"
                "Website Type: {}\n"
                "Generate ONLY this shard's methods. Do not generate class wrapper, constructor, imports, exports, or methods from other shards.\n"
                "Shard: {} of {}\n\n"
            ).format(website_type or "mobile web app", shard_index + 1, total_shards),
            "storage_schema_json": "Storage Schema for this shard:\n" + self._compact_json(storage_schema) + "\n\n",
            "preloaded_data_snapshot_json": "Pre-loaded Data preview for this shard:\n" + self._compact_json(data_snapshot) + "\n\n" if data_snapshot else "",
            "sdk_trace_prompt": (trace_context + "\n\n") if trace_context else "",
            "methods_json": "Methods to implement:\n" + self._compact_json({"interfaces": methods}) + "\n\n",
            "helper_functions_json": "helperFunctions to implement:\n" + self._compact_json(helper_functions) + "\n\n" if helper_functions else "",
            "expected_top_level_method_names_json": (
                "Exact allowed top-level BusinessLogic method names (set equality required):\n"
                + self._compact_json(expected_top_level_method_names)
                + "\n\n"
            ),
            "javascript_stability_fewshot": self._sdk_method_generation_stability_fewshot(),
            "rules_and_return_schema": (
                "RULES:\n"
                "1. Use this._getFromStorage(key, []) / this._saveToStorage(key, data) for all data access.\n"
                "2. Use this._generateId('snake_prefix') for new IDs.\n"
                 "3. Use this._findByIdOrName(collection, id) for entity lookup when useful.\n"
                 "4. Positional arguments only; match method signatures from Methods to implement.\n"
                 "5. Data is pre-loaded by _initStorage(); never create unrelated seed data inside method bodies.\n"
                 "6. When mixing JavaScript ?? with || or &&, add explicit parentheses.\n"
                 "7. Return only methods for this shard. If helperFunctions are listed, implement them once in this shard.\n"
                 "8. Do not invent domain-specific validation constraints. If no explicit format/range/pattern constraint is provided by the Methods, Storage Schema, or Trace Action Context, validate only requiredness/basic type and accept any non-empty user-entered identifier value. Vague words like format, valid, standard, or realistic are not explicit constraints; an exact regex, length range, enum, or field-level constraint must be present before rejecting a filled value.\n\n"
                 "9. Do not synthesize business records from user input. Search/list/get methods must return records from storage or provided evidence only; if no evidence matches, return an empty result set instead of fabricated names, addresses, merchants, invoices, products, or recommendations.\n\n"
                 "10. The emitted top-level class method names must equal the Exact allowed top-level BusinessLogic method names list. Implement every listed name exactly once and emit no other top-level method.\n"
                 "11. A top-level helper is allowed only when its name appears in helperFunctions and therefore in the exact allowed list. Never invent an unlisted underscore/private top-level helper. Put any other auxiliary logic inside a required method as a local function or closure.\n"
                 "12. Before returning, enumerate your emitted top-level method declarations and verify set equality with the exact allowed list; missing or extra names fail the shard.\n\n"
                 'Return exactly one JSON object with exactly one top-level field: {"methods": "methodA(...) { ... }\\n\\nmethodB(...) { ... }"}. Do not return a code field, markdown fence, or prose.'
             ),
        }
        prompt = "".join(fields.values())
        field_stats = {}
        for key, value in fields.items():
            field_stats[key] = {
                "chars": len(value),
                "bytes": len(value.encode("utf-8")),
                "estimated_tokens": self._estimate_prompt_tokens(value),
            }
        return prompt, field_stats

    def _compact_json(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def _estimate_prompt_tokens(self, text: str) -> int:
        if not text:
            return 0
        cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
        non_cjk = re.sub(r"[\u4e00-\u9fff]", " ", text)
        pieces = re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?|[^\sA-Za-z0-9_]", non_cjk)
        return cjk + len(pieces)

    def _write_sdk_context_budget_report(self,
                                         semantic_shard_contracts: List[Dict[str, Any]],
                                         shards: List[Dict[str, Any]]) -> None:
        if not self.logger or not getattr(self.logger, "output_dir", None):
            return
        overloaded = [
            contract.get("semantic_shard_id")
            for contract in semantic_shard_contracts
            if contract.get("over_budget")
        ]
        report = {
            "version": "sdk_context_budget_report",
            "semantic_shard_count": len(semantic_shard_contracts),
            "shard_count": len(shards),
            "total_methods": sum(len(shard.get("methods", [])) for shard in shards),
            "total_semantic_estimated_tokens": sum(contract.get("estimated_tokens", 0) for contract in semantic_shard_contracts),
            "total_estimated_tokens": sum(shard.get("estimated_tokens", 0) for shard in shards),
            "overloaded_semantic_shards": overloaded,
            "semantic_shards": [
                {
                    "semantic_shard_id": contract.get("semantic_shard_id"),
                    "semantic_key": contract.get("semantic_key"),
                    "method_count": len(contract.get("method_names", [])),
                    "methods": contract.get("method_names", []),
                    "source_pages": contract.get("source_pages", []),
                    "chars": contract.get("chars", 0),
                    "estimated_tokens": contract.get("estimated_tokens", 0),
                    "budget": contract.get("budget", 0),
                    "over_budget": contract.get("over_budget", False),
                    "prompt_fields": contract.get("prompt_fields", {}),
                }
                for contract in semantic_shard_contracts
            ],
            "shards": [
                {
                    "shard_id": shard.get("shard_id"),
                    "semantic_shard_id": shard.get("semantic_shard_id"),
                    "semantic_key": shard.get("semantic_key"),
                    "method_count": len(shard.get("methods", [])),
                    "methods": shard.get("methods", []),
                    "chars": shard.get("chars", 0),
                    "estimated_tokens": shard.get("estimated_tokens", 0),
                    "prompt_fields": shard.get("prompt_fields", {}),
                }
                for shard in shards
            ],
        }
        try:
            stage_dirs = getattr(self.logger, "stage_directories", {}) or {}
            report_dir = stage_dirs.get("Generate SDK")
            if not report_dir:
                current_stage = getattr(self.logger, "current_stage", None)
                report_dir = stage_dirs.get(current_stage) if current_stage else None
            if not report_dir:
                report_dir = os.path.join(self.logger.output_dir, "backend", "generate_sdk")
                os.makedirs(report_dir, exist_ok=True)
            path = os.path.join(report_dir, "sdk_context_budget_report.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            self.logger.log_info("Wrote SDK context budget report: {}".format(path))
        except Exception as exc:
            self.logger.log_warning("Failed to write SDK context budget report: {}".format(exc))

    async def _generate_tests(self, tasks: List[Dict[str, Any]],
                              entity_schema: Dict[str, Any],
                              interfaces: Dict[str, Any],
                              website_type: str,
                              generated_data: Dict[str, Any],
                              trace_context: str = None) -> str:

        if len(tasks) > 4:
            if self.logger:
                self.logger.log_info(f"Using chunked parallel test generation ({len(tasks)} tasks)")
            return await self._generate_tests_chunked(tasks, entity_schema, interfaces, website_type, generated_data)

        if self.logger:
            self.logger.log_info(f"Using single-call test generation ({len(tasks)} tasks)")
        return await self._generate_tests_single(tasks, entity_schema, interfaces, website_type, generated_data)

    async def _generate_tests_single(self, tasks: List[Dict[str, Any]],
                              entity_schema: Dict[str, Any],
                              interfaces: Dict[str, Any],
                              website_type: str,
                              generated_data: Dict[str, Any]) -> str:


        storage_key_map = self._extract_storage_key_map(entity_schema)


        limited_generated_data = self._limit_generated_data(generated_data)


        trimmed_entity_schema = self._trim_entity_schema_for_tests(entity_schema)
        trimmed_interfaces = self._trim_interfaces_for_implementation(interfaces)


        prompt = self._build_test_prompt_full(
            tasks, trimmed_entity_schema, storage_key_map, trimmed_interfaces,
            limited_generated_data, website_type
        )


        call_id = None
        if self.logger:
            call_id = self.logger.log_api_call(
                "Generate Tests",
                prompt,
                additional_args={"website_type": website_type}
            )

        try:
            response, usage = await call_openai_api_json_async(
                [{"role": "user", "content": prompt}],
                model=self.model,
                reasoning_effort=self.reasoning_effort,
                stage="generate_tests",
            )


            if self.logger:
                self.logger.log_api_response(
                    "Generate Tests",
                    success=True,
                    response=response,
                    usage_info=usage,
                    stage="Parallel Generation",
                    call_id=call_id
                )


            if isinstance(response, str):
                parsed_response = json.loads(response)
            else:
                parsed_response = response


            if isinstance(parsed_response, dict) and 'code' in parsed_response:
                code = parsed_response['code']
            else:

                code = json.dumps(parsed_response, indent=2)


            code = self._replace_setup_test_data(code, limited_generated_data, storage_key_map)

            return code

        except Exception as e:
            import traceback
            if self.logger:
                self.logger.log_error(f"Failed to generate tests: {str(e)}")
                self.logger.log_error(f"Stack trace:\n{traceback.format_exc()}")

                self.logger.log_api_response(
                    "Generate Tests",
                    success=False,
                    error=str(e),
                    stage="Parallel Generation",
                    call_id=call_id
                )
            raise

    async def _generate_tests_chunked(self, tasks: List[Dict[str, Any]],
                                       entity_schema: Dict[str, Any],
                                       interfaces: Dict[str, Any],
                                       website_type: str,
                                       generated_data: Dict[str, Any]) -> str:
        storage_key_map = self._extract_storage_key_map(entity_schema)
        limited_generated_data = self._limit_generated_data(generated_data)
        trimmed_entity_schema = self._trim_entity_schema_for_tests(entity_schema)
        trimmed_interfaces = self._trim_interfaces_for_implementation(interfaces)


        setup_body = self._generate_setup_test_data_deterministic(limited_generated_data, storage_key_map)


        max_tasks_per_chunk = 3
        task_chunks = []
        for i in range(0, len(tasks), max_tasks_per_chunk):
            task_chunks.append(tasks[i:i + max_tasks_per_chunk])

        if self.logger:
            self.logger.log_info(f"Splitting {len(tasks)} tasks into {len(task_chunks)} test chunks")
            self.logger.log_info(f"setupTestData generated deterministically ({len(setup_body)} chars)")
            for i, chunk in enumerate(task_chunks):
                task_names = [t.get('name', t.get('task', f'task_{j}')) for j, t in enumerate(chunk)]
                self.logger.log_info(f"  Chunk {i+1}: {', '.join(task_names)}")


        chunk_tasks = [
            self._generate_test_chunk(
                chunk_tasks=chunk,
                all_entity_schema=trimmed_entity_schema,
                storage_key_map=storage_key_map,
                all_interfaces=trimmed_interfaces,
                limited_generated_data=limited_generated_data,
                website_type=website_type,
                chunk_index=i,
                total_chunks=len(task_chunks),
                is_first_chunk=False
            )
            for i, chunk in enumerate(task_chunks)
        ]

        chunk_results = await asyncio.gather(*chunk_tasks)


        result = self._merge_test_chunks(list(chunk_results), storage_key_map, limited_generated_data,
                                         setup_body_override=setup_body)

        if self.logger:
            self.logger.log_info(f"Merged {len(task_chunks)} test chunks into final TestRunner class ({len(result)} chars)")

        return result

    async def _generate_test_chunk(self,
                                    chunk_tasks: List[Dict[str, Any]],
                                    all_entity_schema: Dict[str, Any],
                                    storage_key_map: Dict[str, str],
                                    all_interfaces: Dict[str, Any],
                                    limited_generated_data: Dict[str, Any],
                                    website_type: str,
                                    chunk_index: int,
                                    total_chunks: int,
                                    is_first_chunk: bool) -> str:
        prompt = """You are an expert test engineer. Generate ONLY the test method implementations for the specified tasks.

Website Type: """ + website_type + """
Tasks to test (chunk """ + str(chunk_index + 1) + " of " + str(total_chunks) + """): """ + json.dumps(chunk_tasks, indent=2) + """
Data Models: """ + json.dumps(all_entity_schema, indent=2) + """
Storage Key Mapping: """ + json.dumps(storage_key_map, indent=2) + """
Interfaces: """ + json.dumps(all_interfaces, indent=2) + """
Generated Data: """ + json.dumps(limited_generated_data, indent=2) + """

REQUIREMENTS:
1. Generate COMPLETE test method declarations as class methods (2-space indent), each in the format:
   testSomeFlowName() {
     const testName = 'testSomeFlowName';
     try {
       // test logic here
       this.recordSuccess(testName);
     } catch (error) {
       this.recordFailure(testName, error);
     }
   }
2. Do NOT generate a class wrapper, constructor, setupTestData, clearStorage, assert, recordSuccess, recordFailure, runAllTests, helper methods, or module.exports
3. Each test method should follow the naming pattern: test<FlowName>Flow() or test<TaskAction>()
4. CRITICAL: Each test MUST start with a method declaration like `testFoo() {` — do NOT return just the body
5. Use this.logic.<methodName>() to call SDK methods with positional arguments only
6. Use Generated Data values for localStorage reads — access via storage keys from Storage Key Mapping
7. NEVER hardcode expected return values — always extract from actual API responses
8. Chain API calls properly: call method → capture response → use returned IDs for next call
9. Focus on happy path (successful scenarios)
10. Test REAL data flow through the system
11. After each task flow reaches its key state-mutating action (e.g. add to cart / create checkout / booking selection), assert instrumentation/localStorage side effects via helper methods that already exist on TestRunner:
    - this.assertStorageKeyExists(key)
    - this.assertStorageJSONHasFields(key, requiredFields)
    - this.assertStoragePrimitiveExists(key)
12. Do NOT hand-roll ad-hoc localStorage assertions when a helper exists; call the helper directly.
13. For object-shaped instrumentation payloads, prefer asserting required fields rather than exact whole-object equality.

Return JSON: {"test_methods": "// complete test method declarations"}"""

        call_id = None
        if self.logger:
            call_id = self.logger.log_api_call(
                f"Generate Test Chunk {chunk_index + 1}/{total_chunks}",
                prompt,
                additional_args={"website_type": website_type, "chunk": chunk_index + 1}
            )

        try:
            response, usage = await call_openai_api_json_async(
                [{"role": "user", "content": prompt}],
                model=self.model,
                reasoning_effort=self.reasoning_effort,
                stage="generate_test_chunk",
            )

            if self.logger:
                self.logger.log_api_response(
                    f"Generate Test Chunk {chunk_index + 1}/{total_chunks}",
                    success=True,
                    response=response,
                    usage_info=usage,
                    stage="Parallel Generation",
                    call_id=call_id
                )

            if isinstance(response, str):
                response = json.loads(response)


            return json.dumps(response)

        except Exception as e:
            import traceback
            if self.logger:
                self.logger.log_error(f"Test chunk {chunk_index + 1} failed: {str(e)}")
                self.logger.log_error(f"Stack trace:\n{traceback.format_exc()}")
                self.logger.log_api_response(
                    f"Generate Test Chunk {chunk_index + 1}/{total_chunks}",
                    success=False,
                    error=str(e),
                    stage="Parallel Generation",
                    call_id=call_id
                )
            raise

    def _merge_test_chunks(self, chunks: List[str], storage_key_map: Dict[str, str],
                           limited_generated_data: Dict[str, Any],
                           setup_body_override: str = None) -> str:
        setup_body = setup_body_override or ""
        all_test_methods = []
        all_test_method_names = []

        for i, chunk_str in enumerate(chunks):
            try:
                chunk_data = json.loads(chunk_str) if isinstance(chunk_str, str) else chunk_str
            except (json.JSONDecodeError, TypeError):

                all_test_methods.append(chunk_str if isinstance(chunk_str, str) else str(chunk_str))
                continue

            if isinstance(chunk_data, dict):
                if 'setup_method' in chunk_data and chunk_data['setup_method']:
                    setup_body = chunk_data['setup_method']
                if 'test_methods' in chunk_data and chunk_data['test_methods']:
                    methods_code = chunk_data['test_methods']


                    import re
                    names = re.findall(r'(test\w+)\s*\(\)\s*\{', methods_code)
                    if not names:


                        methods_code = self._wrap_unwrapped_test_bodies(methods_code)
                        names = re.findall(r'(test\w+)\s*\(\)\s*\{', methods_code)
                    all_test_methods.append(methods_code)
                    all_test_method_names.extend(names)
                elif 'code' in chunk_data:
                    all_test_methods.append(chunk_data['code'])


        if not setup_body:
            setup_lines = []
            setup_lines.append("    const generatedData = " + json.dumps(limited_generated_data, indent=6) + ";")
            for data_type in limited_generated_data:
                storage_key = storage_key_map.get(data_type, data_type)
                setup_lines.append(f"    if (generatedData['{data_type}']) localStorage.setItem('{storage_key}', JSON.stringify(generatedData['{data_type}']));")
            setup_body = "\n".join(setup_lines)


        run_all_lines = ["    console.log('Starting flow tests...');", ""]
        for name in all_test_method_names:
            run_all_lines.append(f"    this.{name}();")
        run_all_lines.append("")
        run_all_lines.append("    return this.results;")
        run_all_body = "\n".join(run_all_lines)


        result = """const BusinessLogic = require('./business_logic.js');

class TestRunner {
  constructor(businessLogic) {
    this.logic = businessLogic || new BusinessLogic();
    this.results = [];
    this.clearStorage();
    this.setupTestData();
  }

  clearStorage() {
    if (typeof localStorage !== 'undefined' && localStorage.clear) {
      localStorage.clear();
    }
    if (this.logic && typeof this.logic._initStorage === 'function') {
      this.logic._initStorage();
    }
  }

  setupTestData() {
""" + setup_body + """
  }

  runAllTests() {
""" + run_all_body + """
  }

""" + "\n\n".join(all_test_methods) + """

  assert(condition, message) {
    if (!condition) {
      throw new Error('Assertion failed: ' + message);
    }
  }

  assertStorageKeyExists(key) {
    const raw = localStorage.getItem(key);
    this.assert(raw !== null && raw !== undefined && raw !== '', `Expected localStorage key ${key} to exist`);
    return raw;
  }

  assertStorageJSONHasFields(key, requiredFields) {
    const raw = this.assertStorageKeyExists(key);
    let parsed = null;
    try {
      parsed = JSON.parse(raw);
    } catch (error) {
      throw new Error(`Expected ${key} to contain valid JSON, got: ${raw}`);
    }
    for (const field of requiredFields || []) {
      this.assert(Object.prototype.hasOwnProperty.call(parsed, field), `Expected ${key} JSON to contain field ${field}`);
    }
    return parsed;
  }

  assertStoragePrimitiveExists(key) {
    const raw = this.assertStorageKeyExists(key);
    this.assert(raw !== '[object Object]', `Expected ${key} to be stored as primitive, got [object Object]`);
    return raw;
  }

  recordSuccess(testName) {
    this.results.push({test: testName, success: true});
    console.log('\\u2713 ' + testName);
  }

  recordFailure(testName, error) {
    this.results.push({test: testName, success: false, error: error.message});
    console.log('\\u2717 ' + testName + ': ' + error.message);
  }
}

module.exports = TestRunner;
"""
        return result

    def _wrap_unwrapped_test_bodies(self, methods_code: str) -> str:
        import re

        parts = re.split(r"(?=\bconst\s+testName\s*=\s*'(test\w+)')", methods_code)
        if len(parts) <= 1:
            return methods_code

        wrapped_methods = []
        i = 0
        while i < len(parts):
            part = parts[i].strip()
            if not part:
                i += 1
                continue

            m = re.match(r"const\s+testName\s*=\s*'(test\w+)'", part)
            if m:
                method_name = m.group(1)

                wrapped = f"  {method_name}() {{\n    {part}\n  }}"
                wrapped_methods.append(wrapped)
            else:

                if part:
                    wrapped_methods.append(part)
            i += 1

        return "\n\n".join(wrapped_methods)

    def _generate_setup_test_data_deterministic(self, limited_generated_data: Dict[str, Any],
                                                 storage_key_map: Dict[str, str]) -> str:
        lines = ["    localStorage.clear();", ""]

        for data_type, data_items in limited_generated_data.items():
            storage_key = storage_key_map.get(data_type, data_type)

            json_str = json.dumps(data_items, ensure_ascii=False)

            js_str = json_str.replace("\\", "\\\\").replace("'", "\\'")
            lines.append(f"    localStorage.setItem('{storage_key}', '{js_str}');")

        lines.append("")
        lines.append("    // Re-initialize business logic after seeding data")
        lines.append("    if (this.logic && typeof this.logic._initStorage === 'function') {")
        lines.append("      this.logic._initStorage();")
        lines.append("    }")

        return "\n".join(lines)

    def _replace_setup_test_data(self, code: str,
                                  limited_generated_data: Dict[str, Any],
                                  storage_key_map: Dict[str, str]) -> str:
        import re as _re
        deterministic_body = self._generate_setup_test_data_deterministic(
            limited_generated_data, storage_key_map
        )


        pattern = r'(\s+)setupTestData\(\)\s*\{'
        match = _re.search(pattern, code)
        if not match:
            return code

        indent = match.group(1)
        start_pos = match.end()


        brace_depth = 1
        pos = start_pos
        while pos < len(code) and brace_depth > 0:
            if code[pos] == '{':
                brace_depth += 1
            elif code[pos] == '}':
                brace_depth -= 1
            pos += 1


        return code[:match.start()] + indent + 'setupTestData() {\n' + deterministic_body + '\n  }' + code[pos:]

    def _extract_storage_key_map(self, entity_schema: Dict[str, Any]) -> Dict[str, str]:
        storage_key_map = {}
        for entity in entity_schema.get('entities', []):
            entity_name = entity.get('name')
            storage_key = entity.get('storage_key', entity_name.lower() + 's' if entity_name else '')
            if entity_name:
                storage_key_map[entity_name] = storage_key
        return storage_key_map

    def _trim_entity_schema_for_tests(self, entity_schema: Dict[str, Any]) -> Dict[str, Any]:
        if not entity_schema:
            return entity_schema

        trimmed = {}
        for key, value in entity_schema.items():
            if key == 'entities' and isinstance(value, list):
                trimmed_entities = []
                for entity in value:
                    if not isinstance(entity, dict):
                        trimmed_entities.append(entity)
                        continue
                    trimmed_entity = {
                        'name': entity.get('name', ''),
                    }
                    if entity.get('storage_key'):
                        trimmed_entity['storage_key'] = entity['storage_key']

                    if 'fields' in entity and isinstance(entity['fields'], list):
                        trimmed_fields = []
                        for field in entity['fields']:
                            if not isinstance(field, dict):
                                trimmed_fields.append(field)
                                continue
                            tf = {'name': field.get('name', ''), 'type': field.get('type', 'string')}
                            if field.get('foreign_key'):
                                tf['foreign_key'] = field['foreign_key']
                            if field.get('references'):
                                tf['references'] = field['references']
                            if field.get('primary_key'):
                                tf['primary_key'] = True
                            trimmed_fields.append(tf)
                        trimmed_entity['fields'] = trimmed_fields
                    trimmed_entities.append(trimmed_entity)
                trimmed['entities'] = trimmed_entities
            elif key in ('description', 'observed_examples', 'sample_data', 'constraints_text'):
                continue
            else:
                trimmed[key] = value
        return trimmed

    def _build_test_prompt_full(self, tasks, trimmed_entity_schema, storage_key_map,
                                 trimmed_interfaces, limited_generated_data, website_type):
        return """You are an expert test engineer. Generate flow-based integration tests for the business logic.

Website Type: """ + website_type + """
Tasks: """ + json.dumps(tasks, indent=2) + """
Data Models: """ + json.dumps(trimmed_entity_schema, indent=2) + """
Storage Key Mapping: """ + json.dumps(storage_key_map, indent=2) + """
Interfaces: """ + json.dumps(trimmed_interfaces, indent=2) + """

IMPORTANT: Use the following pre-generated data for INITIAL SETUP ONLY:
CRITICAL: Copy ALL data values EXACTLY as provided. DO NOT modify, translate, abbreviate, or paraphrase any names, IDs, prices, or other values from the Generated Data.
Every assertion in your tests must check for values that EXIST in the Generated Data above. Never invent or modify data values.

Generated Data: """ + json.dumps(limited_generated_data, indent=2) + """

NOTE: The Generated Data is limited (max 3 items per type). Adapt original tasks to use available data while preserving core functionality being tested.

CRITICAL REQUIREMENTS FOR INTEGRATION TESTING:
1. Use Generated Data ONLY in setupTestData() for initial localStorage population
2. NEVER hardcode expected return values - always extract from actual API responses
3. When testing flows, chain API calls properly:
   - Call API method and capture its FULL response
   - Extract needed values from the response for next calls
   - Use actual returned IDs, not hardcoded ones
4. Example of CORRECT flow testing:
   ```javascript
   // CORRECT: Using actual API responses
   const addResult = this.logic.addToCart(userId, productId, 2);
   const actualCartId = addResult.cartId;  // Extract from response
   const cartData = this.logic.getCart(actualCartId);  // Use actual ID
   const actualTotal = cartData.total;  // Extract from response
   this.assert(actualTotal > 0, 'Total should be positive');
   ```
5. Test REAL data flow through the system
6. Verify relationships using actual returned data

REQUIREMENTS:
1. Test complete user flows, not individual functions
2. Simulate real user actions sequence
3. Focus on happy path (successful scenarios)
4. Use simple assertions (no complex testing framework)
5. Must run in Node.js environment
6. Test ALL tasks provided
7. IMPORTANT: Use ONLY simple CommonJS exports: module.exports = TestRunner;
8. DO NOT use AMD, UMD, define, or any other module patterns
9. CRITICAL: When accessing localStorage, use the storage_key from the Storage Key Mapping
10. Call SDK interfaces with positional arguments only
11. After each task flow reaches its key state-mutating action (e.g. add to cart / create checkout / booking selection), assert instrumentation/localStorage side effects via TestRunner helpers:
    - assertStorageKeyExists(key)
    - assertStorageJSONHasFields(key, requiredFields)
    - assertStoragePrimitiveExists(key)
12. Prefer helper-based instrumentation assertions over ad-hoc localStorage logic.

STRUCTURE:
```javascript
class TestRunner {
  constructor(businessLogic) {
    this.logic = businessLogic || new BusinessLogic();
    this.results = [];
    this.clearStorage();
    this.setupTestData();
  }
  clearStorage() {
    if (typeof localStorage !== 'undefined' && localStorage.clear) { localStorage.clear(); }
    if (this.logic && typeof this.logic._initStorage === 'function') { this.logic._initStorage(); }
  }
  setupTestData() { /* populate localStorage from Generated Data using Storage Key Mapping */ }
  runAllTests() { /* call all test methods, return this.results */ }
  // test methods...
  assert(condition, message) { if (!condition) throw new Error('Assertion failed: ' + message); }
  recordSuccess(testName) { this.results.push({test: testName, success: true}); console.log('✓ ' + testName); }
  recordFailure(testName, error) { this.results.push({test: testName, success: false, error: error.message}); console.log('✗ ' + testName + ': ' + error.message); }
}
module.exports = TestRunner;
```

Generate COMPLETE test flows for ALL user tasks in JSON format: {"code": "javascript code here"}
"""

    def _trim_entity_schema_for_implementation(self, entity_schema: Dict[str, Any]) -> Dict[str, Any]:
        if not entity_schema:
            return entity_schema

        trimmed = {}
        for entity_name, entity_def in entity_schema.items():
            if entity_name.startswith('_'):

                trimmed[entity_name] = entity_def
                continue

            if not isinstance(entity_def, dict):
                trimmed[entity_name] = entity_def
                continue

            trimmed_entity = {}
            for key, value in entity_def.items():
                if key == 'fields' and isinstance(value, list):

                    trimmed_fields = []
                    for field in value:
                        if not isinstance(field, dict):
                            trimmed_fields.append(field)
                            continue
                        trimmed_field = {
                            'name': field.get('name', ''),
                            'type': field.get('type', 'string'),
                        }

                        if field.get('foreign_key'):
                            trimmed_field['foreign_key'] = field['foreign_key']
                        if field.get('references'):
                            trimmed_field['references'] = field['references']
                        if field.get('required'):
                            trimmed_field['required'] = True
                        if field.get('unique'):
                            trimmed_field['unique'] = True
                        if field.get('primary_key'):
                            trimmed_field['primary_key'] = True
                        if field.get('default') is not None:
                            trimmed_field['default'] = field['default']

                        if field.get('enum'):
                            enum_vals = field['enum']
                            if len(enum_vals) > 5:
                                trimmed_field['enum'] = enum_vals[:5] + ['...']
                            else:
                                trimmed_field['enum'] = enum_vals
                        trimmed_fields.append(trimmed_field)
                    trimmed_entity['fields'] = trimmed_fields
                elif key in ('description', 'observed_examples', 'sample_data', 'constraints_text'):

                    continue
                else:

                    trimmed_entity[key] = value

            trimmed[entity_name] = trimmed_entity

        return trimmed

    def _trim_interfaces_for_implementation(self, interfaces: Dict[str, Any]) -> Dict[str, Any]:
        if not interfaces:
            return interfaces

        trimmed = {}
        for group_name, group_interfaces in interfaces.items():
            if not isinstance(group_interfaces, list):
                trimmed[group_name] = group_interfaces
                continue

            trimmed_group = []
            for iface in group_interfaces:
                if not isinstance(iface, dict):
                    trimmed_group.append(iface)
                    continue

                trimmed_iface = {
                    'name': iface.get('name', ''),
                }

                if 'parameters' in iface:
                    params = iface['parameters']
                    if isinstance(params, list):
                        trimmed_params = []
                        for p in params:
                            if isinstance(p, dict):
                                tp = {'name': p.get('name', ''), 'type': p.get('type', 'any')}
                                if p.get('required') is not None:
                                    tp['required'] = p['required']
                                if p.get('default') is not None:
                                    tp['default'] = p['default']
                                if p.get('entityReference'):
                                    tp['entityReference'] = p['entityReference']
                                trimmed_params.append(tp)
                            else:
                                trimmed_params.append(p)
                        trimmed_iface['parameters'] = trimmed_params
                    else:
                        trimmed_iface['parameters'] = params


                if 'returns' in iface:
                    trimmed_iface['returns'] = iface['returns']


                if 'category' in iface:
                    trimmed_iface['category'] = iface['category']

                trimmed_group.append(trimmed_iface)

            trimmed[group_name] = trimmed_group

        return trimmed

    def generate_sync(self, tasks: List[Dict[str, Any]],
                     entity_schema: Dict[str, Any],
                     interfaces: Dict[str, Any],
                     website_type: str,
                     generated_data: Dict[str, Any],
                     task_interaction_contracts: Dict[str, Any] = None) -> Tuple[str, str]:

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            return loop.run_until_complete(
                self.generate_parallel(
                    tasks,
                    entity_schema,
                    interfaces,
                    website_type,
                    generated_data,
                    task_interaction_contracts=task_interaction_contracts,
                )
            )
        finally:
            loop.close()

    async def generate_async(self, tasks: List[Dict[str, Any]],
                           entity_schema: Dict[str, Any],
                           interfaces: Dict[str, Any],
                           website_type: str,
                           generated_data: Dict[str, Any],
                           task_interaction_contracts: Dict[str, Any] = None) -> Tuple[str, str]:
        return await self.generate_parallel(
            tasks,
            entity_schema,
            interfaces,
            website_type,
            generated_data,
            task_interaction_contracts=task_interaction_contracts,
        )

    def _limit_generated_data(self, generated_data: Dict[str, Any], max_items_per_type: int = 3) -> Dict[str, Any]:
        limited_data = {}

        for data_type, data_items in generated_data.items():
            if isinstance(data_items, list):

                limited_data[data_type] = data_items[:max_items_per_type]


                if self.logger and len(data_items) > max_items_per_type:
                    self.logger.log_info(f"Limited {data_type} from {len(data_items)} to {max_items_per_type} items for test generation")
            else:

                limited_data[data_type] = data_items

        return limited_data
