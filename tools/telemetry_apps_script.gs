// Rogue Pi telemetry: receives rows from car/telemetry.py on the Pi and appends them to a tab of this spreadsheet.
// Install (once): in the Setup Log spreadsheet, Extensions > Apps Script, paste this file, save.
//   Project Settings > Script properties > Add: SECRET = a long random string (the same one goes in
//   ~/.config/telemetry.json on the Pi). Then Deploy > New deployment > Web app, Execute as: Me,
//   Who has access: Anyone. Copy the /exec URL into ~/.config/telemetry.json on the Pi.
// The URL alone is not enough to write: every request must carry the SECRET. Only tabs listed in TABS accept rows.
// After editing: Deploy > Manage deployments > edit (pencil) > Version: New version > Deploy. The /exec URL stays the same.
const TABS = ["Pi Temps", "Drives", "Drive Samples"];
const MAX_ROWS = { "Drive Samples": 60000 };   // oldest samples go first (~110 drives of 45 min); the Pi keeps them all
const DATE_RE = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}(:\d{2})?$/;

function doPost(e) {
  let req;
  try {
    req = JSON.parse(e.postData.contents);
  } catch (err) {
    return out("Error: not JSON");
  }
  const secret = PropertiesService.getScriptProperties().getProperty("SECRET");
  if (!secret || req.secret !== secret) return out("Error: bad secret");
  if (TABS.indexOf(req.sheet) < 0 || !Array.isArray(req.rows) || !req.rows.length) return out("Error: bad request");
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sh = ss.getSheetByName(req.sheet);
  if (!sh) return out("Error: no tab " + req.sheet);
  const tz = ss.getSpreadsheetTimeZone();
  // "2026-11-18 10:06[:00]" (the Pi's local time) becomes a date with the same wall-clock time whatever the
  // spreadsheet's time zone; number-looking text becomes a number so the charts can use it ("0x0", gear, states stay text)
  const width = req.rows[0].length;          // only the data columns: the summary formulas to the right stay untouched
  const rows = req.rows.map(function (r) {
    const row = r.slice(0, width).map(function (v, i) {
      if (DATE_RE.test(String(v))) return Utilities.parseDate(String(v), tz, String(v).length > 16 ? "yyyy-MM-dd HH:mm:ss" : "yyyy-MM-dd HH:mm");
      return v !== "" && !isNaN(Number(v)) && !/^0x/i.test(String(v)) ? Number(v) : v;
    });
    while (row.length < width) row.push("");
    return row;
  });
  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    const start = sh.getLastRow() + 1;
    if (start + rows.length - 1 > sh.getMaxRows()) sh.insertRowsAfter(sh.getMaxRows(), rows.length + 500);
    sh.getRange(start, 1, rows.length, width).setValues(rows);
    const extra = sh.getLastRow() - 1 - (MAX_ROWS[req.sheet] || Infinity);
    if (extra > 0) sh.deleteRows(2, extra);
  } finally {
    lock.releaseLock();
  }
  return out("ok");
}

function out(text) {
  return ContentService.createTextOutput(text).setMimeType(ContentService.MimeType.TEXT);
}
