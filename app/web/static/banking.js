'use strict';
(() => {
  const root = document.querySelector('[data-banking]');
  if (!root) return;
  let csrf = null;
  let activeLink = null;
  let busy = false;
  const error = document.getElementById('banking-error');
  const explain = {
    link_outcome_uncertain_do_not_retry: 'Connection outcome is uncertain. Do not connect again. Check provider access before recovery.',
    revocation_uncertain_do_not_retry: 'Disconnection outcome is uncertain. Do not retry. Check access in Plaid or your bank.',
    stale_review_plan: 'This transaction changed. Reload and review it again.',
    current_member_consent_required: 'Current consent from this member is required.',
    egress_blocked: 'Bank connections are disabled by the current local-access policy.',
    candidate_acknowledgement_required: 'Review the equal-amount candidates and confirm this is a separate transaction.',
    flow_requires_reviewed_relationship: 'Choose accounting meaning. Transfers, refunds and reimbursements need relationship review.',
    account_or_evidence_conflict: 'Account mapping or accepted evidence conflicts. Revoke the existing association before changing it.'
  };
  async function call(path, body) {
    if (!csrf) {
      const response = await fetch('/connections/data/overview', {cache:'no-store'});
      if (!response.ok) throw new Error('Unable to load connection settings.');
      csrf = (await response.json()).csrf;
    }
    const response = await fetch('/connections/data/' + path, {
      method:'POST', headers:{'Content-Type':'application/json','X-Banking-CSRF':csrf},
      body:JSON.stringify(body), credentials:'same-origin', cache:'no-store'
    });
    const value = await response.json();
    if (!response.ok) throw new Error(explain[value.detail] || String(value.detail || 'Unable to save.').replaceAll('_',' '));
    return value;
  }
  async function run(action) {
    if (busy) return;
    busy = true;
    if (error) error.hidden = true;
    const controls = [...root.querySelectorAll('button')].map(el => [el,el.disabled]);
    controls.forEach(([el]) => {el.disabled = true;});
    try { await action(); }
    catch (e) {if (error) {error.textContent = e.message; error.hidden = false; error.scrollIntoView({block:'nearest'});}}
    finally {busy = false; controls.forEach(([el,disabled]) => {el.disabled = disabled;});}
  }
  const reload = () => window.location.reload();
  root.addEventListener('click', event => {
    const button = event.target.closest('button');
    if (!button || (button.closest('form') && (button.type === 'submit' || button.type === 'reset'))) return;
    if (button.dataset.consent) run(async () => {await call('consent',{member_id:Number(button.dataset.consent),granted:true,version:'plaid-transactions-v1'}); reload();});
    if (button.dataset.withdraw) run(async () => {await call('consent',{member_id:Number(button.dataset.withdraw),granted:false,version:'plaid-transactions-v1'}); reload();});
    if (button.dataset.link || button.dataset.repair) run(async () => {
      const body = {member_id:Number(button.dataset.link || button.dataset.memberId)};
      if (button.dataset.repair) body.connection_id = Number(button.dataset.repair);
      const result = await call('link/start', body);
      activeLink = result.session_id;
      const panel = document.getElementById('banking-link'); panel.hidden = false;
      const link = document.getElementById('banking-hosted');
      const url = new URL(result.hosted_link_url);
      if (url.origin !== 'https://secure.plaid.com' || !url.pathname.startsWith('/hl/')) throw new Error('Invalid Plaid destination.');
      link.href = result.hosted_link_url; link.focus();
    });
    if (button.id === 'banking-finish') run(async () => {
      if (!activeLink) throw new Error('Start a connection first.');
      const result = await call('link/finish',{session_id:activeLink});
      if (result.status === 'connected') reload();
      else document.getElementById('banking-link-status').textContent = 'Complete Plaid, then check again.';
    });
    if (button.dataset.finishSession) run(async () => {
      const result = await call('link/finish',{session_id:button.dataset.finishSession});
      if (result.status === 'connected') reload();
      else throw new Error('Complete Plaid in its open window, then check again.');
    });
    for (const action of ['sync','accounts','disconnect']) {
      if (button.dataset[action]) run(async () => {
        if (action === 'disconnect' && !window.confirm('Disconnect provider access? Retained bank evidence remains available.')) return;
        await call(action,{connection_id:Number(button.dataset[action])}); reload();
      });
    }
  });
  const member = document.getElementById('banking-member');
  if (member) member.addEventListener('submit', event => {event.preventDefault();run(async () => {await call('member',{name:new FormData(member).get('name')});reload();});});
  for (const form of document.querySelectorAll('.banking-binding')) {
    const initial = JSON.stringify([...new FormData(form)]);
    const save = form.querySelector('[type=submit]');
    form.addEventListener('input', () => {save.disabled = initial === JSON.stringify([...new FormData(form)]);});
    form.addEventListener('change', () => {save.disabled = initial === JSON.stringify([...new FormData(form)]);});
    form.addEventListener('reset', () => {save.disabled = true;});
    form.addEventListener('submit', event => {event.preventDefault();run(async () => {
      const data = new FormData(form); const owner = data.get('owner');
      await call('bind', {connection_id:Number(form.dataset.connection),provider_account_id:form.dataset.providerAccount,
        expected_revision:Number(form.dataset.revision), account_id:data.get('account_id') ? Number(data.get('account_id')) : null,
        included:data.has('included'), ownership:owner === 'shared' ? 'shared' : 'personal',
        owner_member_id:owner === 'shared' ? null : Number(owner), role:data.get('role'),
        new_name:data.get('new_name') || null, new_kind:data.get('new_kind') || null}); reload();
    });});
  }
  const decision = document.getElementById('banking-decision');
  for (const form of document.querySelectorAll('.banking-receipt')) {
    form.addEventListener('submit', event => {event.preventDefault();run(async () => {
      const data = new FormData(form);
      form.dataset.operationKey ||= crypto.randomUUID();
      await call('receipt',{extraction_id:Number(form.dataset.extraction),transaction_id:Number(data.get('transaction_id')),
        reason:String(data.get('reason')),operation_key:form.dataset.operationKey});reload();
    });});
  }
  if (decision) decision.addEventListener('submit', event => {event.preventDefault();run(async () => {
    const data = new FormData(decision); const selection = String(data.get('action'));
    let operationKey = decision.dataset.operationKey;
    if (!operationKey) {operationKey = crypto.randomUUID(); decision.dataset.operationKey = operationKey;}
    await call('decision/' + root.dataset.subject, {fingerprint:root.dataset.fingerprint, operation_key:operationKey,
      action:selection.startsWith('match:') ? 'match' : selection, transaction_id:selection.startsWith('match:') ? Number(selection.split(':')[1]) : null,
      flow_kind:data.get('flow_kind') || 'unknown',reason:String(data.get('reason')), acknowledge_candidates:data.has('acknowledge_candidates')});
    window.location.assign('/bank-feed');
  });});
  root.dataset.bankingReady = 'true';
})();
