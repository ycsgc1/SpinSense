// db_utils.js — shared dBFS conversion. Loaded before any page-specific
// script via _layout.html so window.SpinSense.db is always available.
//
// The Python mirror in gui/tests/test_db_utils.py pins the contract.
(function () {
  if (!window.SpinSense) window.SpinSense = {};
  window.SpinSense.db = {
    // Canonical dB display/threshold floor. Anything quieter clamps here.
    // The Python mirror in gui/tests/test_db_utils.py pins this value.
    FLOOR_DB: -120,
    rmsToDb(rms) {
      if (rms <= 0) return this.FLOOR_DB;
      return Math.max(this.FLOOR_DB, 20 * Math.log10(rms));
    },
    dbToRms(db) {
      return Math.pow(10, db / 20);
    },
    formatDb(db) {
      return `${db.toFixed(1)} dB`;
    },
  };
})();
