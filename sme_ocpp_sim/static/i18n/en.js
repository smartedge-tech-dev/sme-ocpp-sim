"use strict";
// SME OCPPSim — English UI strings: the reference dictionary. ja.js / zh.js fall back to English for missing keys.
// The prefixes shell.*, vendor.* and common.* belong to sme-sim-common and must not be defined here
// (checked by sme_sim_common.testing.check_i18n_keys in the product tests).
Shell.addStrings("en", {
  "chargers.title": "Chargers",
  "chargers.empty": "No chargers configured yet.",
  "about.stack": "Stack",
  "about.stack.value": "Python, FastAPI, websockets (OCPP-J client)",
});
