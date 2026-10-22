/* Candidate-only UI. No credentials, tokens or session state in browser storage. */
(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const page = document.querySelector('.sandbox-page');
  if (!page) return;
  const consentVersion = 'plaid-sandbox.v1';
  let state = null, busy = false, linkScript = null, activityRequest = 0, activityAbort;
  let ready = false;
  let pendingFocus = null;
  const lockedControls = new Map();
  const accountDrafts = new Map();
  let pagination = {offset: 0, limit: 50, total: 0, has_more: false};
  function el(tag, text, className) {
    const n = document.createElement(tag);
    if (text !== undefined) n.textContent = text;
    if (className) n.className = className;
    return n;
  }
  function option(value, text) { const n = el('option', text); n.value = value; return n; }
  function message(text, reviewConnection) {
    $('sandbox-message').textContent = text;
    if (reviewConnection) $('sandbox-message').append(document.createTextNode(' '), link('Review accounts', '/accounts?connection_id=' + encodeURIComponent(reviewConnection)));
  }
  function unlockControls() {
    for (const [control, disabled] of lockedControls) if (control.isConnected) control.disabled = disabled;
    lockedControls.clear();
  }
  function lockControls() {
    if (!busy && ready) {unlockControls(); return;}
    for (const control of page.querySelectorAll('button,input,select')) {
      if (control.id === 'sandbox-retry' || lockedControls.has(control)) continue;
      lockedControls.set(control, control.disabled); control.disabled = true;
    }
  }
  function focusNextPrerequisite() {
    if (!$('sandbox-link')) return;
    const target = !state.enabled ? ($('sandbox-file-consent') || $('sandbox-client')) : !state.members.length ? $('member-name') : $('link-owner');
    target?.focus();
  }
  function person(id) { return state.members.find(m => m.id === id)?.name || 'Unknown member'; }
  function ownership(id) { return id === 'shared' ? 'Joint / shared' : id === 'unassigned' || !id ? 'Unassigned' : person(id); }
  function accountLabel(record) {
    const name = record.account_name || 'Unknown account', owner = person(record.owner_id);
    // Overview order is stable across Activity filters/pages and Home's recent list.
    const connections = state.connections || [];
    const matches = (state.accounts || []).filter(account => (account.name || 'Unknown account') === name
      && person(connections.find(connection => connection.id === account.connection_id)?.owner_id) === owner);
    let label = name + ' · ' + owner;
    if (new Set(matches.map(account => account.connection_id)).size > 1) {
      const index = connections.findIndex(connection => connection.id === record.connection_id);
      if (index >= 0) label += ' · Connection ' + (index + 1);
    }
    if (matches.filter(account => account.connection_id === record.connection_id).length > 1) {
      const index = state.accounts.filter(account => account.connection_id === record.connection_id)
        .findIndex(account => account.account_id === record.account_id);
      if (index >= 0) label += ' · Account ' + (index + 1);
    }
    return label;
  }
  function timestamp(value) {
    if (!value) return 'Never';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? 'Unknown' : date.toLocaleString();
  }
  function money(cents, currency) {
    const code = currency || 'Unknown currency';
    if ((typeof cents === 'number' && !Number.isSafeInteger(cents)) || !/^-?\d+$/.test(String(cents))) return 'Unknown amount · ' + code;
    const amount = BigInt(cents), absolute = amount < 0n ? -amount : amount;
    return (amount < 0n ? '−' : amount > 0n ? '+' : '') + new Intl.NumberFormat(undefined, {maximumFractionDigits: 0}).format(absolute / 100n) + '.' + (absolute % 100n).toString().padStart(2, '0') + ' ' + code;
  }
  function activityURL(fields) { const params = new URLSearchParams(fields); return '/activity' + (params.size ? '?' + params : ''); }
  function link(text, href) { const a = el('a', text, 'link'); a.href = href; return a; }
  function button(text, action, primary = false) {
    const b = el('button', text, primary ? 'primary' : 'ghost'); b.type = 'button'; b.onclick = action; return b;
  }
  function detail(title, values) {
    const d = el('details', undefined, 'sandbox-details'); d.append(el('summary', title));
    const list = el('dl', undefined, 'sandbox-facts');
    for (const [key, value] of values) list.append(el('dt', key), el('dd', value ?? 'Unknown'));
    d.append(list); return d;
  }
  async function get(endpoint, signal) {
    const response = await fetch('/api/sandbox/' + endpoint, {credentials: 'same-origin', cache: 'no-store', signal});
    if (!response.ok) throw Error('Could not load imports. Reload if your session expired.');
    return response.json();
  }
  async function api(action, body) {
    const response = await fetch('/api/sandbox/' + action, {method: 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': state.csrf}, body: JSON.stringify(body)});
    const data = await response.json();
    if (!response.ok) throw Error(data.error || 'sandbox_request_failed');
    return data;
  }
  function empty(holder, text, destination = '/connections', label = 'Manage connections') {
    holder.replaceChildren(el('p', text, 'tx-empty subtle'), link(label, destination));
  }
  function suppliedText(value) { return typeof value === 'string' && value.trim() ? value : null; }
  function metadata(value) { return value && typeof value === 'object' && !Array.isArray(value) ? value : {}; }
  function readableLabel(value) {
    const label = suppliedText(value);
    if (!label || !/^[A-Z][A-Z0-9_]*$/.test(label)) return label;
    return label.charAt(0) + label.slice(1).toLowerCase().replaceAll('_', ' ');
  }
  function row(r, recent = false) {
    const article = el(recent ? 'a' : 'details', undefined, recent ? 'tx-row sandbox-recent-row' : 'sandbox-transaction');
    article.dataset.stagedRow = '';
    if (recent) article.href = activityURL({connection_id: r.connection_id, account_id: r.account_id});
    const summary = recent ? article : el('summary', undefined, 'tx-row');
    const enrichment = metadata(r.enrichment);
    const merchantName = suppliedText(enrichment.merchant_name);
    const merchant = el('strong', merchantName || suppliedText(r.name) || 'Unnamed transaction', 'sandbox-merchant');
    const account = el('span', accountLabel(r), 'sandbox-account-label subtle');
    const meta = el('span', undefined, 'sandbox-row-meta');
    const status = (r.state || 'Unknown status').replaceAll('_', ' ');
    meta.append(el('span', r.date || 'Unknown date', 'sandbox-date subtle'), el('span', status.charAt(0).toUpperCase() + status.slice(1) + (r.selected ? '' : ' · Excluded'), 'sandbox-state subtle'));
    const formattedAmount = money(r.amount_cents, r.currency);
    if (formattedAmount.length > 22) summary.classList.add('sandbox-wide-amount');
    const chevron = el('span', '›', 'sandbox-chevron'); chevron.ariaHidden = 'true';
    summary.append(merchant, account, meta, el('span', formattedAmount, 'sandbox-money'), chevron);
    if (!recent) {
      article.append(summary);
      const facts = el('dl', undefined, 'sandbox-facts');
      for (const [name, value] of [['Merchant', merchantName || 'Not supplied'], ['Date', r.date || 'Unknown'], ['Signed amount', money(r.amount_cents, r.currency)], ['Account', r.account_name || 'Unknown'], ['Connection owner', person(r.owner_id)], ['Household account ownership', ownership(r.ownership)], ['Status', r.state], ['Accounting meaning', 'Unknown · not booked']]) facts.append(el('dt', name), el('dd', value));
      const category = metadata(enrichment.category_suggestion);
      const categories = [readableLabel(category.primary), readableLabel(category.detailed)].filter(Boolean);
      if (categories.length) {
        const suggestion = ['Suggested by Plaid', ...categories];
        if (suppliedText(category.confidence_level)) suggestion.push('Confidence: ' + readableLabel(category.confidence_level));
        if (suppliedText(category.version)) suggestion.push('Version: ' + category.version);
        facts.append(el('dt', 'Category suggestion'), el('dd', suggestion.join(' · '), 'nancy'));
      }
      const source = [['Provider', r.provider || 'Plaid'], ['Environment', r.environment || 'sandbox'],
        ['Provider description', suppliedText(r.name) || 'Not supplied'],
        ['Original bank description', suppliedText(enrichment.original_description) || 'Not supplied'],
        ['Connection ID', r.connection_id], ['Item ID', r.item_id], ['Account ID', r.account_id],
        ['Transaction ID', r.transaction_id], ['Replaces pending ID', r.pending_transaction_id || 'None']];
      if (suppliedText(enrichment.merchant_entity_id)) source.push(['Plaid merchant entity ID', enrichment.merchant_entity_id]);
      let counterpartyNumber = 0;
      for (const value of Array.isArray(enrichment.counterparties) ? enrichment.counterparties : []) {
        const counterparty = metadata(value), evidence = [];
        if (suppliedText(counterparty.name)) evidence.push(counterparty.name);
        for (const [key, label] of [['type', 'Type'], ['entity_id', 'Entity ID'], ['confidence_level', 'Confidence']]) {
          if (suppliedText(counterparty[key])) evidence.push(label + ': ' + counterparty[key]);
        }
        if (evidence.length) source.push(['Plaid counterparty ' + (++counterpartyNumber), evidence.join(' · ')]);
      }
      article.append(facts, detail('Source details', source));
    }
    return article;
  }
  function renderHome() {
    const summary = $('sandbox-summary'); if (!summary) return;
    summary.replaceChildren();
    for (const [key, label] of [['connection_count', 'Connections'], ['account_count', 'Accounts'], ['transaction_count', 'Current transactions'], ['pending_count', 'Pending']]) {
      const card = el('div', undefined, 'stat-card'); card.append(el('div', String(state.summary[key]), 'stat-number'), el('div', label, 'subtle')); summary.append(card);
    }

    const holder = $('sandbox-recent'); holder.replaceChildren(...state.recent.map(r => row(r, true)));
    if (!state.recent.length) empty(holder, state.connections.length ? 'No activity imported yet. Sync a connected bank to begin.' : 'Connect a test bank to see imported accounts and activity.');
  }
  function renderAccounts() {
    const holder = $('sandbox-accounts'); if (!holder) return;
    holder.replaceChildren();
    const requestedConnection = new URLSearchParams(location.search).get('connection_id');
    if (requestedConnection) holder.append(link('All accounts', '/accounts'));
    if (!state.accounts.length) { holder.append(el('p', emptyAccountMessage(requestedConnection), 'subtle'), link('Manage connections', '/connections')); return; }
    for (const [index, c] of state.connections.entries()) {
      if (requestedConnection && c.id !== requestedConnection) continue;
      const accounts = state.accounts.filter(a => a.connection_id === c.id);
      if (!accounts.length) continue;
      const group = el('section', undefined, 'sandbox-account-group');
      group.id = 'connection-' + encodeURIComponent(c.id);
      group.append(el('h2', 'Connection ' + (index + 1) + ' · ' + person(c.owner_id)));
      for (const a of accounts) group.append(accountRow(a, c));
      holder.append(group);
    }
    if (requestedConnection && !holder.querySelector('.sandbox-account-group')) {
      holder.append(el('p', emptyAccountMessage(requestedConnection), 'subtle'), link('Manage connections', '/connections'));
    }
  }
  function emptyAccountMessage(connectionId) {
    const connections = connectionId ? state.connections.filter(c => c.id === connectionId) : state.connections;
    const active = connections.filter(c => ['connected', 'needs_repair'].includes(c.status));
    if (!connections.length) return connectionId ? 'No accounts available for this connection. Check Connections.' : 'No accounts imported yet. Connect a test bank to begin.';
    if (!state.enabled) return 'No accounts available. Sandbox network access is disabled. Check Connections.';
    if (active.some(c => c.error !== 'ITEM_LOGIN_REQUIRED')) return 'Accounts have not loaded yet. Use Sync on the existing connection in Connections; do not relink.';
    if (active.length) return 'No accounts available. Bank login repair is required in Connections.';
    if (connections.some(c => ['revoking', 'revocation_uncertain', 'revocation_unconfirmed'].includes(c.status))) return 'No accounts available. Remote revocation is unconfirmed. Check Connections.';
    return 'No accounts available. This connection is disconnected. Check Connections.';
  }
  function accountKey(a) { return JSON.stringify([a.connection_id, a.account_id]); }
  function accountMapping(a) { return {selected: Boolean(a.selected), ownership: a.ownership || 'unassigned'}; }
  function focusAccount(key, selector) {
    const article = [...document.querySelectorAll('.sandbox-account')].find(n => n.dataset.accountKey === key);
    article?.querySelector(selector)?.focus();
  }
  function accountRow(a, c) {
    const key = accountKey(a), draft = accountDrafts.get(key);
    if (draft) {
      const fresh = accountMapping(a);
      // Preserve only user changes; untouched fields follow authoritative refreshes.
      for (const field of ['selected', 'ownership']) if (draft[field] === draft.baseline[field]) draft[field] = fresh[field];
      draft.baseline = fresh;
    }
    const article = el('article', undefined, 'sandbox-account'); article.dataset.accountKey = key;
    const identity = el('div', undefined, 'sandbox-account-identity');
    identity.append(el('h3', a.name || 'Unnamed account'), el('p', [a.type || 'Unknown type', a.subtype, a.mask ? 'Ending ' + a.mask : 'No suffix available'].filter(Boolean).join(' · '), 'subtle'));
    const mapping = el('p', (a.selected ? 'Included' : 'Excluded') + ' · Account owner: ' + ownership(a.ownership), 'sandbox-account-mapping');
    const actions = el('div', undefined, 'sandbox-actions');
    const edit = button('Edit', () => {
      const baseline = accountMapping(a);
      accountDrafts.set(key, {...baseline, baseline});
      renderAccounts(); focusAccount(key, 'select');
    });
    const peers = state.accounts.filter(other => other.connection_id === c.id);
    const ambiguous = peers.filter(other => other.name === a.name && other.mask === a.mask).length > 1;
    const suffix = a.mask ? ' · Ending ' + a.mask : '';
    const ordinal = !a.mask || ambiguous ? ' · Account ' + (peers.indexOf(a) + 1) : '';
    edit.dataset.action = 'edit-account'; edit.setAttribute('aria-label', 'Edit ' + (a.name || 'account') + suffix + ordinal + ' · Connection ' + (state.connections.indexOf(c) + 1));
    edit.hidden = Boolean(draft);
    actions.append(edit, link('View activity', activityURL({connection_id: a.connection_id, account_id: a.account_id})));
    article.append(identity, mapping, actions);
    if (draft) article.append(accountEditor(a, key, draft));
    const evidence = detail('Source details', [['Provider', c.provider || 'Plaid'], ['Environment', c.environment || 'sandbox'], ['Connection ID', a.connection_id], ['Account ID', a.account_id], ['Item ID', c.item_id]]);
    evidence.classList.add('sandbox-account-source'); article.append(evidence);
    return article;
  }
  function accountEditor(a, key, draft) {
    const form = el('form'); form.dataset.accountForm = '';
    const ownerLabel = el('label', 'Account owner'), owner = el('select');
    owner.append(option('unassigned', 'Unassigned'), option('shared', 'Joint / shared'), ...state.members.map(m => option(m.id, m.name))); owner.value = draft.ownership; ownerLabel.append(owner);
    const includeLabel = el('label', undefined, 'sandbox-check'), include = el('input'); include.type = 'checkbox'; include.checked = draft.selected; includeLabel.append(include, document.createTextNode('Include account in household view'));
    const save = el('button', 'Save changes', 'primary'); save.type = 'submit';
    const update = () => {
      draft.ownership = owner.value; draft.selected = include.checked;
      save.disabled = !owner.value || (draft.ownership === draft.baseline.ownership && draft.selected === draft.baseline.selected);
    };
    owner.onchange = update; include.onchange = update; update();
    const cancel = button('Cancel', () => {accountDrafts.delete(key); renderAccounts(); focusAccount(key, '[data-action=edit-account]');}); cancel.dataset.action = 'cancel-account';
    form.append(ownerLabel, includeLabel, save, cancel);
    form.onsubmit = e => {
      e.preventDefault(); if (save.disabled || busy || !ready) return;
      const body = {connection_id: a.connection_id, account_id: a.account_id, selected: draft.selected, ownership: draft.ownership};
      let saved = false;
      run(async () => {
        try {await api('mapping', body);}
        catch {throw Error('mapping_failed');}
        accountDrafts.delete(key); saved = true; message('');
      }, () => focusAccount(key, saved ? '[data-action=edit-account]' : 'button[type=submit]'));
    };
    return form;
  }
  function updateLinkAvailability() {
    if (!$('sandbox-link')) return;
    const uncertain = (state.link_attempts || []).some(a => a.status === 'uncertain' && !a.acknowledged_at && a.owner_id === $('link-owner').value);
    $('sandbox-link').disabled = !state.enabled || !state.members.length || uncertain || !$('link-consent').checked;
    $('sandbox-link-help').textContent = uncertain ? 'This member has an uncertain exchange. Check Link recovery before creating another connection.' : 'Use a Plaid test login, never real bank credentials.';
  }
  function renderConnections() {
    const holder = $('sandbox-connections'); if (!holder) return;
    $('sandbox-disable').disabled = !state.enabled;
    $('sandbox-disable').hidden = !state.enabled;
    const owner = $('link-owner').value;
    $('link-owner').replaceChildren(...state.members.map(m => option(m.id, m.name)));
    if (state.members.some(m => m.id === owner)) $('link-owner').value = owner;
    $('link-consent').checked = false;
    updateLinkAvailability();
    const attempts = $('sandbox-attempts'), uncertain = (state.link_attempts || []).filter(a => a.status === 'uncertain');
    attempts.parentElement.open = uncertain.some(a => !a.acknowledged_at);
    attempts.parentElement.querySelector('summary').textContent = uncertain.length ? 'Link recovery · ' + uncertain.length + ' uncertain exchange' + (uncertain.length === 1 ? '' : 's') : 'Link recovery';
    attempts.replaceChildren(...uncertain.map(a => {
      const history = el('article'); history.dataset.attemptId = a.id;
      history.append(el('p', 'Exchange delivery uncertain for ' + person(a.owner_id) + '. Never replay this exchange.'));
      history.append(detail('Attempt details', [['Attempt ID', a.id], ['Connection owner', person(a.owner_id)], ['Exchange status', 'Uncertain'], ['Dashboard check recorded', a.acknowledged_at ? timestamp(a.acknowledged_at) : 'Not recorded']]));
      if (a.acknowledged_at) {
        history.append(el('p', 'Dashboard check recorded. Remote outcome remains unverified: this does not prove remote deletion or that the original exchange failed. A separate connection requires fresh owner consent.'));
      } else {
        history.append(el('p', 'Check this exact attempt in the Plaid Sandbox dashboard before allowing a separate connection. Recording a check does not prove remote deletion or exchange failure.'));
        if (!a.connection_id) {
          const label = el('label', undefined, 'sandbox-check'), checked = el('input'); checked.type = 'checkbox';
          label.append(checked, document.createTextNode('I checked this attempt in Plaid Sandbox and want to start a separate connection'));
          const record = button('Record check', () => {
            if (!checked.checked) return;
            checked.checked = false; record.disabled = true;
            let recorded = false;
            run(async () => {
              await api('acknowledge-exchange', {session_id: a.id, consent: 'plaid-sandbox-recovery.v1'});
              recorded = true;
              message('Dashboard check recorded. Remote outcome remains unverified. Confirm fresh owner consent to connect separately.');
            }, () => {
              if (recorded && $('link-owner').value === a.owner_id) {
                document.querySelector('.sandbox-connect-panel').open = true; $('link-consent').focus();
              } else $('sandbox-attempts').parentElement.querySelector('summary').focus();
            });
          }, true);
          record.dataset.action = 'acknowledge-exchange'; record.disabled = true;
          checked.onchange = () => {record.disabled = !checked.checked;};
          history.append(label, record);
        }
      }
      return history;
    }));
    $('sandbox-members').replaceChildren(...state.members.map(m => el('li', m.name)));
    const needsSetup = !state.enabled, needsMember = !state.members.length;
    if (!state.connections.length || needsSetup || needsMember) document.querySelector('.sandbox-connect-panel').open = true;
    $('setup-details').open = needsSetup;
    $('members-details').open = !needsSetup && needsMember;
    $('sandbox-link-form').hidden = needsSetup || needsMember;
    attempts.parentElement.hidden = !uncertain.length;
    holder.closest('section').hidden = !state.connections.length && !uncertain.length;
    holder.replaceChildren();
    if (!state.connections.length && state.enabled && state.members.length) holder.append(el('p', 'No connected banks.', 'subtle'));
    for (const [index, c] of state.connections.entries()) {
      const article = el('article', undefined, 'sandbox-connection'); article.dataset.connectionId = c.id;
      const active = ['connected', 'needs_repair'].includes(c.status);
      const loginRequired = active && c.error === 'ITEM_LOGIN_REQUIRED', usable = state.enabled && active;
      const status = loginRequired ? 'Bank login required' : active && c.error ? 'Sync needs attention' : ({connected: 'Connected', needs_repair: 'Needs attention', disconnected: 'Disconnected', revoking: 'Disconnect in progress', revocation_uncertain: 'Revocation uncertain', revocation_unconfirmed: 'Access invalid · remote revocation unconfirmed'}[c.status] || c.status.replaceAll('_', ' '));
      article.append(el('h3', 'Connection ' + (index + 1) + ' · ' + person(c.owner_id)), el('p', status + ' · ' + state.accounts.filter(a => a.connection_id === c.id).length + ' accounts', 'subtle'), el('p', 'Last local sync: ' + timestamp(c.last_local_sync), 'subtle'));
      if (active && c.error && !loginRequired) article.append(el('p', 'Sync failed: ' + c.error + '. Try Sync; do not add this bank again.', 'subtle'));
      const actions = el('div', undefined, 'sandbox-actions');
      const sync = button('Sync', () => run(async () => {message('Syncing…'); await api('sync', {connection_id: c.id}); message('');}, () => focusConnection(c.id)), !loginRequired);
      sync.dataset.action = 'sync'; sync.disabled = !usable || loginRequired;
      actions.append(link('View accounts', '/accounts?connection_id=' + encodeURIComponent(c.id)), link('View activity', activityURL({connection_id: c.id})), sync);
      const advanced = detail('Connection details', [['Connection owner', person(c.owner_id)], ['Provider', c.provider || 'Plaid'], ['Environment', c.environment || 'sandbox'], ['Last bank update', c.last_bank_update ? timestamp(c.last_bank_update) : 'Unknown'], ['Connection ID', c.id], ['Item ID', c.item_id]]);
      const repairPanel = el('details', undefined, 'sandbox-details'); repairPanel.append(el('summary', 'Repair connection')); repairPanel.open = loginRequired;
      const label = el('label', undefined, 'sandbox-check'); const consent = el('input'); consent.type = 'checkbox'; consent.dataset.repairConsent = '';
      label.append(consent, document.createTextNode('As this connection’s consent grantor, I authorize Sandbox repair (plaid-sandbox.v1).'));
      const repair = button('Repair connection', () => {if (!consent.checked || !usable) return; consent.checked = false; repair.disabled = true; run(() => openLink(c.owner_id, c.id), () => focusConnection(c.id, '[data-repair-consent]'));}, loginRequired);
      repair.dataset.action = 'repair'; repair.disabled = true; consent.disabled = !usable;
      consent.onchange = () => {repair.disabled = !usable || !consent.checked;};
      repairPanel.append(label, repair);
      const disconnect = button(['revoking', 'revocation_uncertain'].includes(c.status) ? 'Reconcile uncertain revocation' : 'Disconnect', () => {
        if (window.confirm('Revoke this Sandbox connection? Local imported data and audit will be retained.')) run(async () => {message('Disconnecting…'); const result = await api('disconnect', {connection_id: c.id}); message(result.revocation === 'confirmed' ? 'Disconnected. Local evidence retained.' : 'Access is invalid; remote revocation is unconfirmed. Verify this Item in the Plaid Sandbox dashboard. Evidence retained.');}, () => focusConnection(c.id, 'summary'));
      }); disconnect.dataset.action = 'disconnect'; disconnect.disabled = !state.enabled || ['disconnected', 'revocation_unconfirmed'].includes(c.status);
      if (loginRequired) article.append(repairPanel);
      else advanced.append(repairPanel);
      if (['revoking', 'revocation_uncertain', 'revocation_unconfirmed'].includes(c.status)) {
        article.append(el('p', 'Remote revocation is unconfirmed. Check this Item in the Plaid Sandbox dashboard; do not assume it was deleted.', 'subtle'));
        actions.append(disconnect);
      } else advanced.append(disconnect);
      article.append(actions, advanced); holder.append(article);
    }
  }
  function focusConnection(id, selector) {
    const article = [...document.querySelectorAll('.sandbox-connection')].find(n => n.dataset.connectionId === id);
    const current = state.connections.find(c => c.id === id);
    const eligible = state.enabled && current && ['connected', 'needs_repair'].includes(current.status);
    const preferred = selector || (eligible ? current.error === 'ITEM_LOGIN_REQUIRED' ? '[data-repair-consent]' : '[data-action=sync]' : 'a');
    let target = article?.querySelector(preferred);
    if (!target || target.disabled) target = article?.querySelector('a') || $('link-heading');
    for (let ancestor = target?.parentElement; ancestor; ancestor = ancestor.parentElement) if (ancestor.tagName === 'DETAILS') ancestor.open = true;
    target?.focus();
  }
  function fillFilterOptions() {
    const form = $('sandbox-filters'); if (!form) return;
    const params = new URLSearchParams(location.search);
    const members = form.elements.owner_id, connections = form.elements.connection_id;
    members.replaceChildren(option('', 'Everyone'), ...state.members.map(m => option(m.id, m.name)));
    connections.replaceChildren(option('', 'All connections'), ...state.connections.map((c, i) => option(c.id, person(c.owner_id) + ' · Connection ' + (i + 1) + ' · ' + c.status.replaceAll('_', ' '))));
    for (const name of ['q', 'owner_id', 'connection_id', 'date_from', 'date_to', 'state', 'inclusion']) {
      if (params.has(name)) {
        const control = form.elements[name], value = params.get(name);
        if (control.tagName === 'SELECT' && ![...control.options].some(o => o.value === value)) control.append(option(value, 'Unavailable selection'));
        control.value = value;
      }
    }
    accountOptions(params.get('account_id') || '');
  }
  function accountOptions(value = '') {
    const form = $('sandbox-filters'), c = form.elements.connection_id.value, account = form.elements.account_id;
    account.replaceChildren(option('', c ? 'All accounts' : 'Choose a connection first'), ...state.accounts.filter(a => a.connection_id === c).map(a => option(a.account_id, a.name + (a.mask ? ' · ' + a.mask : ''))));
    if (value && ![...account.options].some(o => o.value === value)) account.append(option(value, 'Unavailable account'));
    account.value = value; account.disabled = !c;
  }
  async function loadActivity() {
    if (!$('sandbox-staged')) return;
    const ordinal = ++activityRequest;
    activityAbort?.abort(); activityAbort = new AbortController();
    const params = new URLSearchParams(location.search);
    const activeFilters = ['owner_id', 'connection_id', 'account_id', 'date_from', 'date_to'].filter(key => params.get(key)).length + Number(Boolean(params.get('state') && params.get('state') !== 'current')) + Number(Boolean(params.get('inclusion') && params.get('inclusion') !== 'all'));
    $('sandbox-advanced-filters').querySelector('summary').textContent = 'Filters' + (activeFilters ? ' · ' + activeFilters + ' active' : '');
    $('sandbox-previous').disabled = true; $('sandbox-next').disabled = true;
    $('sandbox-result-count').textContent = 'Loading activity…'; $('sandbox-staged').replaceChildren();
    $('sandbox-page-status').textContent = '';
    if (params.get('account_id') && !params.get('connection_id')) {
      $('sandbox-result-count').textContent = 'Choose a connection to filter by account.'; return;
    }
    try {
      const result = await get('activity?' + params, activityAbort.signal);
      if (ordinal !== activityRequest) return;
      pagination = result;
      $('sandbox-staged').replaceChildren(...result.rows.map(r => row(r)));
      $('sandbox-result-count').textContent = result.total ? (result.rows.length ? (result.offset + 1) + '–' + (result.offset + result.rows.length) + ' of ' + result.total + ' transactions' : 'No transactions on this page · ' + result.total + ' match these filters') : '0 transactions';
      if (!result.rows.length) empty($('sandbox-staged'), 'No imported activity matches these filters.', '/activity', 'Clear filters');
      $('sandbox-previous').disabled = result.offset <= 0;
      $('sandbox-next').disabled = !result.has_more;
      $('sandbox-page-status').textContent = result.has_more ? 'More activity available' : 'End of results';
    } catch (error) {
      if (ordinal !== activityRequest || error.name === 'AbortError') return;
      $('sandbox-result-count').textContent = 'Activity could not be loaded.';
      message(error.message); $('sandbox-retry').hidden = false;
    }
  }
  async function refresh() {
    ready = false; lockControls();
    $('sandbox-retry').hidden = true;
    try {
      state = await get('overview');
      unlockControls();
      $('sandbox-status').textContent = state.enabled ? '' : 'Disabled · ' + (state.configured ? 'private setup available' : 'Sandbox setup required');
      renderHome(); renderAccounts(); renderConnections();
      if ($('sandbox-filters')) {fillFilterOptions(); await loadActivity();}
      page.dataset.loaded = 'true';
      ready = true;
    } catch (error) {
      page.dataset.loaded = 'false'; $('sandbox-status').textContent = 'Import status unavailable.';
      message(error.message); $('sandbox-retry').hidden = false;
      for (const id of ['sandbox-summary', 'sandbox-recent', 'sandbox-accounts', 'sandbox-connections']) if ($(id)) $(id).replaceChildren(el('p', 'Unable to load data. Try loading again.', 'subtle'));
    }
    lockControls();
  }
  function loadLink() {
    if (!state.enabled) return Promise.reject(Error('sandbox_disabled'));
    if (window.Plaid) return Promise.resolve();
    if (!linkScript) linkScript = new Promise((resolve, reject) => {
      const script = document.createElement('script'); script.src = 'https://cdn.plaid.com/link/v2/stable/link-initialize.js';
      script.onload = resolve; script.onerror = () => {script.remove(); linkScript = null; reject(Error('link_script_unavailable'));}; document.head.append(script);
    });
    return linkScript;
  }
  async function openLink(owner_id, connection_id) {
    message('Opening official Sandbox Link…');
    const result = await api('link', {owner_id, connection_id, consent: consentVersion});
    await loadLink();
    await new Promise((resolve, reject) => {
      let completed = false;
      const handler = window.Plaid.create({token: result.link_token,
        onSuccess: async publicToken => {
          if (completed) return; completed = true;
          try {
            if (connection_id) {await api('repair-complete', {session_id: result.session_id}); message('Repair checked. Existing connection retained; ready to sync.');}
            else {
              let linked;
              try {linked = await api('exchange', {session_id: result.session_id, public_token: publicToken});}
              catch (error) {
                if (['exchange_uncertain_do_not_replay', 'link_completion_unverified'].includes(error.message)) throw error;
                throw Error('exchange_check_required');
              }
              // Only a successfully authenticated exchange authorizes the first import. Never replay exchange.
              try {await api('sync', {connection_id: linked.connection_id}); message('Bank connected.', linked.connection_id);}
              catch {message('Bank connected, but first sync failed. Use Sync on this connection; do not relink.', linked.connection_id);}
            }
            resolve();
          } catch (error) {reject(error);} finally {handler.destroy(); result.link_token = ''; publicToken = '';}
        },
        onExit: () => {if (completed) return; completed = true; handler.destroy(); result.link_token = ''; message('Link closed. No automatic relink or exchange retry.'); resolve();}
      });
      handler.open();
    });
  }
  async function run(work, focus) {
    if (busy || !ready || !state) return;
    busy = true; page.setAttribute('aria-busy', 'true'); lockControls();
    try {await work();}
    catch (error) {
      const safe = {revocation_uncertain: 'Revocation delivery is uncertain. Use Reconcile uncertain revocation; do not assume this connection is active or deleted.', link_completion_unverified: 'Plaid has not authenticated this Link completion. Nothing was exchanged or attributed. Verify Link event access with Plaid before creating another Item.', exchange_uncertain_do_not_replay: 'Exchange delivery is uncertain. Do not replay or relink. Check Link recovery and the Plaid Sandbox dashboard.', ITEM_LOGIN_REQUIRED: 'Bank login repair required. Use Repair connection.', sandbox_disabled: 'Sandbox network access is disabled.', member_consent_required: 'Select a member and confirm current Sandbox consent.'};
      message(error.message === 'mapping_failed' ? 'Account changes could not be saved. Your draft is still open.' : error.message === 'exchange_check_required' ? 'Connection outcome needs checking. Do not relink or replay the exchange. If a connection appears below, use its Sync action; otherwise check Link recovery and the Plaid Sandbox dashboard.' : safe[error.message] || 'The operation failed. Reload to check current state before retrying. No automatic exchange retry was made.');
    } finally {
      await refresh(); busy = false; page.removeAttribute('aria-busy'); lockControls();
      pendingFocus = focus || null;
      if (ready) restoreFocus(); else $('sandbox-retry').focus();
    }
  }
  function restoreFocus() { const focus = pendingFocus; pendingFocus = null; focus?.(); }
  $('sandbox-retry').onclick = async () => {if (busy) return; busy = true; message(''); await refresh(); busy = false; lockControls(); if (ready) restoreFocus();};
  $('sandbox-link-form')?.addEventListener('submit', e => {e.preventDefault(); if (!busy && state?.enabled && !$('sandbox-link').disabled && $('link-consent').checked) {const owner = $('link-owner').value; $('link-consent').checked = false; updateLinkAvailability(); run(() => openLink(owner, null), () => ($('sandbox-message').querySelector('a') || $('link-consent')).focus());}});
  $('link-owner')?.addEventListener('change', () => {$('link-consent').checked = false; updateLinkAvailability();});
  $('link-consent')?.addEventListener('change', updateLinkAvailability);
  $('sandbox-member')?.addEventListener('submit', e => {e.preventDefault(); const name = $('member-name').value; run(async () => {await api('member', {name}); $('member-name').value = ''; message('');}, focusNextPrerequisite);});
  $('sandbox-file-setup')?.addEventListener('submit', e => {e.preventDefault(); if (!busy && $('sandbox-file-consent').checked) run(async () => {await api('enable-private-file', {consent: consentVersion}); $('sandbox-file-consent').checked = false; message('');}, focusNextPrerequisite);});
  $('sandbox-setup')?.addEventListener('submit', e => {
    e.preventDefault(); if (busy || !$('sandbox-enable-consent').checked) return;
    const body = {client_id: $('sandbox-client').value, secret: $('sandbox-secret').value, consent: consentVersion};
    $('sandbox-client').value = ''; $('sandbox-secret').value = ''; $('sandbox-enable-consent').checked = false;
    run(async () => {try {await api('setup', body); message('');} finally {body.client_id = ''; body.secret = ''; }}, focusNextPrerequisite);
  });
  $('sandbox-disable')?.addEventListener('click', () => run(async () => {await api('disable', {}); message('Sandbox network access disabled. Local evidence retained.');}));
  $('sandbox-filters')?.addEventListener('submit', e => {
    e.preventDefault(); const form = e.currentTarget, url = new URL(location.href);
    for (const name of ['q', 'owner_id', 'connection_id', 'account_id', 'date_from', 'date_to', 'state', 'inclusion']) {const value = form.elements[name].value; value ? url.searchParams.set(name, value) : url.searchParams.delete(name);}
    url.searchParams.delete('offset'); history.pushState(null, '', url); message(''); loadActivity();
  });
  document.querySelector('#sandbox-filters [name=connection_id]')?.addEventListener('change', () => accountOptions());
  for (const [id, direction] of [['sandbox-previous', -1], ['sandbox-next', 1]]) if ($(id)) $(id).onclick = () => {const url = new URL(location.href); url.searchParams.set('offset', String(Math.max(0, pagination.offset + direction * pagination.limit))); history.pushState(null, '', url); loadActivity();};
  window.addEventListener('popstate', () => {if ($('sandbox-filters') && state) { $('sandbox-filters').reset(); fillFilterOptions(); loadActivity();}});
  refresh();
})();
