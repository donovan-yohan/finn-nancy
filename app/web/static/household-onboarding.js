/* Connections-only controller. No financial drafts are stored outside this page. */
'use strict';
(function (global) {
  function hostedLink(value) {
    if (typeof value !== 'string' || value.trim() !== value || /[\u0000-\u0020\u007f]/.test(value)) throw new Error('Invalid Plaid destination.');
    let url;
    try { url = new URL(value); } catch (_) { throw new Error('Invalid Plaid destination.'); }
    if (url.origin !== 'https://secure.plaid.com' || !url.pathname.startsWith('/hl/') || url.username || url.password) {
      throw new Error('Invalid Plaid destination.');
    }
    return url.href;
  }
  function bindingPayload(context, values) {
    const positive = value => /^[1-9]\d*$/.test(String(value)) && Number.isSafeInteger(Number(value));
    if (!values.owner || (values.owner !== 'shared' && !positive(values.owner))) throw new Error('Choose a personal or shared account owner.');
    if (values.account_id !== 'new' && !positive(values.account_id)) throw new Error('Choose a ledger account or create one.');
    if (!['primary','secondary'].includes(values.role)) throw new Error('Choose a bank feed source role.');
    if (![true,false,'include','exclude'].includes(values.included)) throw new Error('Choose include or exclude for household review.');
    const creating = values.account_id === 'new';
    if (creating && !String(values.new_name || '').trim()) throw new Error('Enter the new account name.');
    return {
      connection_id:Number(context.connectionId), provider_account_id:context.providerAccount,
      expected_revision:Number(context.revision), account_id:creating ? null : Number(values.account_id),
      included:values.included === true || values.included === 'include', ownership:values.owner === 'shared' ? 'shared' : 'personal',
      owner_member_id:values.owner === 'shared' ? null : Number(values.owner), role:values.role,
      new_name:creating ? String(values.new_name) : null, new_kind:creating ? String(values.new_kind) : null
    };
  }

  // Both POST and authoritative GET belong to the same admission. A failed GET
  // leaves mutations fenced; recovery reads state without replaying the POST.
  function createOperationOwner(hooks) {
    let busy = false, blocked = false;
    async function read() {
      try {
        await hooks.refresh();
        blocked = false;
        busy = false;
        hooks.unlock();
      } catch (error) {
        blocked = true;
        busy = false;
        hooks.fail(error, true);
      }
    }
    return {
      get busy() { return busy; }, get blocked() { return blocked; },
      async run(action) {
        if (busy || blocked) return;
        busy = true;
        hooks.lock();
        let result;
        try { result = await action(); }
        catch (error) { hooks.fail(error, false); }
        await read();
        busy = false;
        return result;
      },
      async retryRead() {
        if (busy) return;
        busy = true;
        hooks.lock();
        await read();
        busy = false;
      }
    };
  }
  function uncertainError(message = 'The result is not confirmed. Check current state before trying again.') {
    const error = new Error(message);
    error.uncertain = true;
    return error;
  }
  function retainedDrafts(drafts, clearedKey) {
    return drafts.filter(draft => draft.key !== clearedKey);
  }
  function mount(initialRoot) {
    if (!initialRoot || initialRoot.dataset.onboardingReady) return null;
    const doc = initialRoot.ownerDocument;
    const win = doc.defaultView;
    const root = initialRoot;
    const baselines = new WeakMap();
    const disabledStates = new Map();
    let csrf = null;
    let capture = null;
    let context = null;
    let outcome = null;
    let activeLink = null;
    const messages = {
      duplicate_member_name:'That name is already used. Choose a different household name.',
      household_member_name_exists:'That name is already used. Choose a different household name.',
      invalid_member_name:'Enter a name from 1 to 100 characters without control characters.',
      member_name_required:'Enter a name for this household member.',
      member_name_exists:'That name is already used. Choose a different household name.',
      member_name_control_characters:'Enter a name without control characters or line breaks.',
      member_name_too_long:'Use a name of 100 characters or fewer.',
      household_member_name_required:'Name this household member before connecting a bank.',
      current_member_consent_required:'Give current consent for this member before connecting.',
      member_link_unresolved_check_existing_session:'A connection for this member is unresolved. Check it in the browser that started it. Do not connect again until its outcome is resolved.',
      egress_blocked:'Bank access is unavailable. Ask the operator to check connection settings.',
      read_only:'This workspace is read-only. Ask the operator to enable changes.',
      stale_binding:'This account changed. Check the current mapping before saving again.',
      stale_binding_revision:'This account changed. Check the current mapping before saving again.',
      account_or_evidence_conflict:'This mapping conflicts with existing evidence. Check the ledger account and source role.',
      explicit_owner_required:'Choose a personal or shared account owner.',
      link_outcome_uncertain_do_not_retry:'Connection outcome uncertain. Do not connect again. Verify access in Plaid or your bank.',
      revocation_uncertain_do_not_retry:'Disconnection is not confirmed. Do not retry. Check access in Plaid or your bank.',
      browser_session_required:'Your connection session expired. Check current state before trying again.',
      trusted_origin_required:'This address cannot make changes. Open the configured trusted address.'
    };
    const all = (selector, parent = root) => [...parent.querySelectorAll(selector)];
    const findScope = key => all('[data-scope]').find(el => el.dataset.scope === key);
    const fields = form => [...form.elements].filter(el => el.name && ['INPUT','SELECT','TEXTAREA'].includes(el.tagName));
    function values(form) {
      return Object.fromEntries(fields(form).map(el => [el.name, el.type === 'checkbox' ? el.checked : el.value]));
    }
    const same = (a,b) => JSON.stringify(a) === JSON.stringify(b);
    function initialize() {
      for (const form of all('form[data-form]')) {
        baselines.set(form, values(form));
        if (form.dataset.revision !== undefined) form.dataset.baselineRevision = form.dataset.revision;
        updateForm(form);
      }
    }
    function updateForm(form) {
      if (form.dataset.form !== 'binding') return;
      const creating = form.elements.account_id.value === 'new';
      const group = form.querySelector('[data-new-account]');
      group.hidden = !creating;
      const readOnly = root.dataset.readOnly === 'true' || form.dataset.readOnly === 'true';
      group.disabled = !creating || readOnly || owner.busy || owner.blocked;
      form.querySelector('[data-save]').disabled = readOnly || owner.busy || owner.blocked || same(values(form), baselines.get(form));
    }
    function rememberFocus(el = doc.activeElement) {
      if (!el || !root.contains(el)) return null;
      return {id:el.id, key:el.dataset.focusKey,
        form:el.form?.dataset.draftKey, name:el.name,
        start:typeof el.selectionStart === 'number' ? el.selectionStart : null,
        end:typeof el.selectionEnd === 'number' ? el.selectionEnd : null};
    }
    function restoreFocus(saved) {
      if (!saved || saved.external) return;
      let target = saved.id ? doc.getElementById(saved.id) : null;
      if (!target && saved.key) target = all('[data-focus-key]').find(el => el.dataset.focusKey === saved.key);
      if (!target && saved.form) {
        const form = all('form[data-draft-key]').find(el => el.dataset.draftKey === saved.form);
        target = form && (saved.name ? fields(form).find(el => el.name === saved.name) : form.querySelector('input,select,button'));
      }
      if (target?.disabled) target = null;
      if (!target && context?.memberId) target = doc.getElementById('member-' + context.memberId + '-title');
      if (!target && context?.scope) target = findScope(context.scope)?.querySelector('summary,[tabindex],input,button');
      if (target) {
        for (let parent = target.parentElement; parent && parent !== root; parent = parent.parentElement) {
          if (parent.tagName === 'DETAILS') parent.open = true;
        }
      }
      if (target && !target.disabled) {
        target.focus({preventScroll:true});
        if (outcome?.focus) target.scrollIntoView?.({block:'nearest'});
        if (saved.start !== null && target.setSelectionRange) {
          try { target.setSelectionRange(saved.start, saved.end); } catch (_) { /* native select */ }
        }
      }
    }
    function snapshot() {
      return {
        drafts:all('form[data-draft-key]').filter(form => !same(values(form), baselines.get(form)))
          .map(form => ({key:form.dataset.draftKey, revision:form.dataset.revision, values:values(form)})),
        disclosures:all('details[data-disclosure-key]').map(el => [el.dataset.disclosureKey, el.open]),
        focus:rememberFocus()
      };
    }
    function disableControls() {
      for (const el of all('input,select,textarea,button,fieldset')) {
        if (el.dataset.action === 'refresh') continue;
        if (!disabledStates.has(el)) disabledStates.set(el, el.disabled);
        el.disabled = true;
      }
      root.setAttribute('aria-busy', 'true');
    }
    function feedback(scope, text, kind = 'error') {
      const host = findScope(scope);
      const region = host?.querySelector('[data-feedback]');
      if (!region) return;
      region.textContent = text;
      region.dataset.kind = kind;
      region.hidden = false;
    }
    function renderActiveLink() {
      if (!activeLink) return;
      const member = all('[data-member]').find(el => el.dataset.member === String(activeLink.memberId));
      if (!member) return;
      const pending = findScope('session-' + activeLink.sessionId);
      // An uncertain or retired server session revokes the page-held URL and
      // callback. Never hide uncertainty behind a still-clickable check panel.
      if (!pending || pending.dataset.linkState !== 'pending' || member.dataset.linkAllowed !== 'true') {
        activeLink = null;
        return;
      }
      const panel = doc.createElement('section');
      panel.className = 'household-pending';
      panel.dataset.scope = 'active-link';
      panel.dataset.liveLink = 'true';
      const title = doc.createElement('h3');
      title.textContent = 'Continue connecting for ' + member.dataset.memberName;
      const instructions = doc.createElement('p');
      instructions.textContent = 'Select your bank and accounts in Plaid, then return and check this connection.';
      const anchor = doc.createElement('a');
      anchor.href = hostedLink(activeLink.url);
      anchor.target = '_blank'; anchor.rel = 'noopener noreferrer';
      anchor.className = 'btn btn-primary';
      anchor.dataset.focusKey = 'hosted-link';
      anchor.textContent = 'Open Plaid for ' + member.dataset.memberName;
      const check = doc.createElement('button');
      check.type = 'button'; check.className = 'btn btn-outline-secondary';
      check.dataset.action = 'finish'; check.dataset.sessionId = activeLink.sessionId;
      check.dataset.focusKey = 'active-link-check'; check.textContent = 'Check connection';
      check.disabled = root.dataset.networkAllowed !== 'true';
      if (root.dataset.networkAllowed !== 'true') anchor.removeAttribute('href');
      const region = doc.createElement('p');
      region.dataset.feedback = ''; region.setAttribute('role','status'); region.hidden = true;
      panel.append(title,instructions,anchor,check,region);
      // The returned URL is held only in this browser page; server pending state
      // remains the recovery authority after reload. Avoid two check controls.
      pending.hidden = true;
      member.append(panel);
    }
    async function refresh() {
      const response = await win.fetch('/connections', {credentials:'same-origin', cache:'no-store'});
      if (!response.ok) throw new Error('Current connection state could not load.');
      const html = await response.text();
      const parsed = new win.DOMParser().parseFromString(html,'text/html');
      const next = parsed.querySelector('[data-household-onboarding]');
      if (!next) throw new Error('Current connection state could not load.');
      // No scripts from the fetched page execute. Jinja escapes provider values;
      // all dynamic feedback/Hosted Link content below uses DOM textContent.
      const active = doc.activeElement;
      const focus = active && active !== doc.body && !root.contains(active)
        ? {external:true} : rememberFocus() || capture?.focus;
      root.replaceChildren(...next.childNodes);
      root.dataset.consentVersion = next.dataset.consentVersion;
      root.dataset.readOnly = next.dataset.readOnly;
      root.dataset.networkAllowed = next.dataset.networkAllowed;
      initialize();
      for (const draft of retainedDrafts(capture?.drafts || [], outcome?.clearDraft)) {
        const form = all('form[data-draft-key]').find(el => el.dataset.draftKey === draft.key);
        if (!form) continue;
        // A draft must not silently acquire a newer revision after another tab
        // changed its binding. Preserve the original optimistic write fence.
        if (draft.revision !== undefined) form.dataset.revision = draft.revision;
        for (const el of fields(form)) {
          if (!(el.name in draft.values)) continue;
          if (el.type === 'checkbox') el.checked = draft.values[el.name];
          else el.value = draft.values[el.name];
        }
      }
      for (const [key, open] of capture?.disclosures || []) {
        const detail = all('details[data-disclosure-key]').find(el => el.dataset.disclosureKey === key);
        if (detail) detail.open = open;
      }
      if (outcome?.openDisclosure) {
        const detail = all('details[data-disclosure-key]').find(el => el.dataset.disclosureKey === outcome.openDisclosure);
        if (detail) detail.open = true;
      }
      renderActiveLink();
      // New server controls also remain locked until the read consumer finishes.
      // Save/new-account state is recomputed from its canonical baseline below.
      disabledStates.clear();
      for (const form of all('form[data-form="binding"]')) {
        const group = form.querySelector('[data-new-account]');
        group.hidden = form.elements.account_id.value !== 'new';
        group.disabled = group.hidden || root.dataset.readOnly === 'true';
      }
      disableControls();
      if (outcome?.message) feedback(outcome.scope || context?.scope, outcome.message, outcome.error ? 'error' : 'success');
      capture.focus = focus?.external ? focus : outcome?.focus || focus;
      csrf = null;
    }
    const owner = createOperationOwner({
      lock() {
        if (!capture) capture = snapshot();
        disableControls();
        const retry = root.querySelector('[data-action="refresh"]');
        if (retry) retry.disabled = true;
      },
      unlock() {
        for (const [el, disabled] of disabledStates) if (el.isConnected) el.disabled = disabled;
        disabledStates.clear();
        root.removeAttribute('aria-busy');
        for (const form of all('form[data-form]')) updateForm(form);
        if (outcome?.uncertain) {
          const scope = findScope(context?.scope);
          for (const control of scope?.querySelectorAll('input,select,textarea,button') || []) control.disabled = true;
          const panel = root.querySelector('[data-refresh-panel]');
          panel.hidden = false;
          panel.querySelector('[data-refresh-message]').textContent = 'The result is not confirmed. Check current state before repeating this action.';
        }
        restoreFocus(capture?.focus);
        capture = null;
        root.dataset.lastOperation = 'settled';
      },
      refresh,
      fail(error, readFailed) {
        if (readFailed) {
          const panel = root.querySelector('[data-refresh-panel]');
          panel.hidden = false;
          panel.querySelector('[data-refresh-message]').textContent = 'Current state could not load. Changes are paused. Check current state before trying again.';
          const retry = panel.querySelector('button'); retry.disabled = false;
          root.removeAttribute('aria-busy');
          root.dataset.lastOperation = 'read-failed';
          feedback(context?.scope, 'The result is not confirmed. Check current state; do not repeat this action.');
          restoreFocus(capture?.focus);
        } else {
          outcome = {message:error.message || 'The result is not confirmed. Check current state before trying again.', error:true, uncertain:error.uncertain === true};
          feedback(context?.scope, outcome.message);
        }
      }
    });
    async function post(path, body) {
      // body is a plain immutable submission snapshot, never read from disabled
      // controls after fetching the CSRF token or awaiting another request.
      const serialized = JSON.stringify(body);
      if (!csrf) {
        const response = await win.fetch('/connections/data/overview', {credentials:'same-origin',cache:'no-store'});
        if (!response.ok) throw new Error('Connection settings could not load. Check current state.');
        const value = await response.json();
        if (typeof value.csrf !== 'string' || !value.csrf) throw new Error('Connection session could not load. Check current state.');
        csrf = value.csrf;
      }
      let response;
      try {
        response = await win.fetch('/connections/data/' + path, {
          method:'POST', headers:{'Content-Type':'application/json','X-Banking-CSRF':csrf},
          credentials:'same-origin', cache:'no-store', body:serialized
        });
      } catch (_) { throw uncertainError(); }
      let value;
      try { value = await response.json(); }
      catch (_) { throw uncertainError(); }
      if (!response.ok) {
        const message = messages[value.detail] || 'This change could not be confirmed. Check the values and current state.';
        if (String(value.detail).includes('uncertain')) throw uncertainError(message);
        throw new Error(message);
      }
      return value;
    }
    function begin(el, operation) {
      if (owner.busy || owner.blocked || el.disabled || root.dataset.readOnly === 'true') return;
      context = {scope:el.closest('[data-scope]')?.dataset.scope,
        memberId:el.dataset.memberId || el.closest('[data-member]')?.dataset.member};
      outcome = null;
      root.dataset.lastOperation = 'pending';
      feedback(context.scope, 'Working…', 'success');
      void owner.run(operation);
    }
    root.addEventListener('input', event => {
      const form = event.target.closest('form[data-form]');
      if (form) updateForm(form);
    });
    root.addEventListener('change', event => {
      const form = event.target.closest('form[data-form]');
      if (form) updateForm(form);
    });
    root.addEventListener('reset', event => {
      const form = event.target.closest('form[data-form]');
      if (!form) return;
      event.preventDefault();
      if (owner.busy || owner.blocked || root.dataset.readOnly === 'true') return;
      const initial = baselines.get(form);
      if (form.dataset.baselineRevision !== undefined) form.dataset.revision = form.dataset.baselineRevision;
      for (const el of fields(form)) {
        if (el.type === 'checkbox') el.checked = initial[el.name]; else el.value = initial[el.name];
      }
      updateForm(form);
      const detail = form.closest('details');
      if (detail) { detail.open = false; detail.querySelector('summary')?.focus(); }
      const region = form.closest('[data-scope]')?.querySelector('[data-feedback]');
      if (region) region.hidden = true;
    });
    root.addEventListener('submit', event => {
      const form = event.target.closest('form[data-form]');
      if (!form) return;
      event.preventDefault();
      if (owner.busy || owner.blocked || root.dataset.readOnly === 'true' || form.querySelector('[type="submit"]')?.disabled || !form.reportValidity()) return;
      const submitted = values(form);
      let payload;
      try { payload = form.dataset.form === 'binding' ? bindingPayload(form.dataset, submitted) : {name:submitted.name}; }
      catch (error) { feedback(form.closest('[data-scope]').dataset.scope, error.message); return; }
      const key = form.dataset.draftKey;
      const mode = form.dataset.form;
      const memberId = form.dataset.memberId;
      begin(form, async () => {
        const result = await post(mode === 'binding' ? 'bind' : mode === 'member-rename' ? 'member/' + memberId : 'member', payload);
        outcome = {clearDraft:key, message:mode === 'binding' ? 'Account saved.' : 'Name saved.'};
        if (mode === 'member-add') {
          context.memberId = result.member_id;
          outcome.focus = {id:'member-' + result.member_id + '-title'};
        } else if (mode === 'binding') {
          outcome.focus = {key:'edit-' + form.closest('[data-account-key]').dataset.accountKey};
        } else outcome.focus = {id:'member-' + memberId + '-title'};
      });
    });
    root.addEventListener('click', event => {
      const hosted = event.target.closest('a[data-focus-key="hosted-link"]');
      if (hosted && (owner.busy || owner.blocked)) { event.preventDefault(); return; }
      const button = event.target.closest('button[data-action]');
      if (!button || button.disabled) return;
      const action = button.dataset.action;
      if (action === 'cancel-consent') {
        if (owner.busy) return;
        const disclosure = button.closest('details');
        disclosure.open = false;
        disclosure.querySelector('summary').focus();
        return;
      }
      if (action === 'refresh') { if (outcome?.uncertain) outcome = null; void owner.retryRead(); return; }
      if (owner.busy || owner.blocked || root.dataset.readOnly === 'true') return;
      const memberId = Number(button.dataset.memberId);
      const connectionId = Number(button.dataset.connectionId);
      const sessionId = button.dataset.sessionId;
      const version = root.dataset.consentVersion;
      if (action === 'disconnect' && !win.confirm('Disconnect this bank for ' + button.closest('[data-member]').dataset.memberName + '? Retained bank evidence will remain.')) return;
      if (action === 'withdraw' && !win.confirm('Withdraw consent for ' + button.closest('[data-member]').dataset.memberName + '? Further bank requests will stop.')) return;
      begin(button, async () => {
        if (action === 'consent' || action === 'withdraw') {
          await post('consent', {member_id:memberId, granted:action === 'consent', version});
          outcome = {message:action === 'consent' ? 'Consent saved. Connect a bank when ready.' : 'Consent withdrawn.',
            openDisclosure:action === 'consent' ? 'consent-' + memberId : null,
            focus:{key:action === 'consent' ? 'link-' + memberId : 'add-bank-' + memberId}};
        } else if (action === 'link' || action === 'repair') {
          const body = {member_id:memberId};
          if (action === 'repair') body.connection_id = connectionId;
          const result = await post('link/start', body);
          activeLink = {memberId, sessionId:result.session_id, url:hostedLink(result.hosted_link_url)};
          outcome = {focus:{key:'hosted-link'}};
        } else if (action === 'finish') {
          const result = await post('link/finish', {session_id:sessionId});
          if (result.status === 'connected') {
            activeLink = null;
            outcome = {message:'Bank connected. Load accounts from this connection.', focus:{id:'member-' + context.memberId + '-title'}};
          } else outcome = {scope:activeLink?.sessionId === sessionId ? 'active-link' : context.scope,
            message:'Complete the bank and account selection in Plaid, then check again.', focus:{key:button.dataset.focusKey}};
        } else if (['accounts','sync','disconnect'].includes(action)) {
          await post(action, {connection_id:connectionId});
          outcome = {message:action === 'accounts' ? 'Accounts loaded.' : action === 'sync' ? 'Bank feed synced. Review new activity in Bank feed.' : 'Bank disconnected.',
            focus:{key:action === 'disconnect' ? 'connection-' + connectionId + '-title' : button.dataset.focusKey}};
        }
      });
    });
    initialize();
    root.dataset.onboardingReady = 'true';
    return owner;
  }
  const api = {hostedLink, bindingPayload, createOperationOwner, uncertainError, retainedDrafts, mount};
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (global.document) {
    global.HouseholdOnboarding = api;
    mount(global.document.querySelector('[data-household-onboarding]'));
  }
}(typeof window === 'undefined' ? globalThis : window));
