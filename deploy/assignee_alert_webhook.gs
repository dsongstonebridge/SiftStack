// DataSift -> Slack: alert someone when a property is assigned to them.
// Receives DataSift's "Send a webhook" sequence action and posts to the
// assignments Slack channel, tagging the person named in ?who= on the URL.
// Deploy: Deploy > New deployment > Web app, Execute as: Me, Who has access: Anyone.

const SLACK_WEBHOOK = '<SLACK INCOMING WEBHOOK URL>';
const TOKEN = '<RANDOM TOKEN, also on the DataSift webhook URL as ?k=>';   // must match ?k= on the URL DataSift calls
const PEOPLE = { diego: '<slack member id>', eric: '<slack member id>' };  // add jeff later

function doPost(e) {
  if (!e || !e.parameter || e.parameter.k !== TOKEN) return reply_('forbidden');
  const raw = (e.postData && e.postData.contents) || '';
  let body;
  try { body = JSON.parse(raw); } catch (err) { return reply_('bad json'); }

  // DataSift retries (and Google answers POSTs with a redirect DataSift may
  // count as a failure), so never post the same event twice.
  const id = body.event_id || '';
  const cache = CacheService.getScriptCache();
  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    if (id && cache.get(id)) return reply_('duplicate');

    let text;
    if (body.event_type === 'sequence.action.triggered') {
      // ?who=diego (named above) or ?slack=U0XXXXXXX (any Slack member id, no script edit needed)
      const direct = String(e.parameter.slack || '');
      const slackId = PEOPLE[String(e.parameter.who || '').toLowerCase()] ||
                      (/^U[A-Z0-9]{6,}$/.test(direct) ? direct : '');
      const p = body.property || {};
      const a = p.address || {};
      const street = fixOrdinals_(a.street || '(no street)');
      const place = [street, a.city].filter(String).join(', ');
      const o = p.owner || {};
      const owner = o.company || [o.first_name, o.last_name].filter(String).join(' ');
      const link = p.uuid ? '<https://app.reisift.io/records/properties/' + p.uuid + '/details|' + place + '>' : place;
      text = (slackId ? '<@' + slackId + '> ' : '') + 'A property was just assigned to you:\n- ' + link + (owner ? ' (' + owner + ')' : '');
    } else {
      // webhook.test or anything unexpected: no mention, show what arrived.
      text = 'DataSift webhook received (' + (body.event_type || 'unknown') + '):\n```' + raw.slice(0, 2500) + '```';
    }

    const r = UrlFetchApp.fetch(SLACK_WEBHOOK, {
      method: 'post', contentType: 'application/json',
      payload: JSON.stringify({ text: text }), muteHttpExceptions: true });
    if (r.getResponseCode() !== 200) return reply_('slack failed ' + r.getResponseCode());
    if (id) cache.put(id, '1', 21600);
    return reply_('ok');
  } finally {
    lock.releaseLock();
  }
}

function doGet() { return reply_('assignee alert receiver is up'); }

function fixOrdinals_(s) {
  return s.replace(/(\d)(St|Nd|Rd|Th)(?![a-z])/g, function (m, d, suf) { return d + suf.toLowerCase(); });
}

function reply_(s) { return ContentService.createTextOutput(s); }

// Run this once from the editor to grant permission to post to Slack.
function authorize() { UrlFetchApp.fetch('https://slack.com/api/api.test'); }
