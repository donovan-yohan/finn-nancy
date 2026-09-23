/* Same-origin transport only. Sessions, credentials and model routing stay on
 * the server. Drafts live in this page; reconnect never replays a submission. */
(function () {
  'use strict';
  const shell = document.querySelector('.hermes-chat');
  if (!shell) return;
  const byId = id => document.getElementById(id);
  const form = byId('chat-form');
  const input = byId('chat-input');
  const list = byId('chat-list');
  const send = byId('chat-send');
  const stop = byId('chat-stop');
  const status = byId('chat-status');
  const error = byId('chat-error');
  const uncertain = byId('chat-uncertain');
  const reconnect = byId('chat-reconnect');
  const requestList = byId('chat-requests');
  const empty = byId('chat-empty');
  const enabled = shell.dataset.chatEnabled === 'true';
  const retryDelays = [500, 1000, 2000, 4000, 8000];
  const requests = new Map();
  let questionSequence = 0;
  let socket;
  let ready = false;
  let running = false;
  let stopping = false;
  let pending = null;
  let turn = null;
  let retry = 0;
  let retryTimer;
  let connectTimer;
  let leaving = false;
  let threadInUse = false;

  function node(tag, className, text) {
    const element = document.createElement(tag);
    element.className = className || '';
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function notice(element, text) {
    element.textContent = text;
    element.hidden = !text;
  }

  function controls() {
    send.disabled = !ready || running || !!pending || !input.value.trim();
    stop.disabled = !ready || !running || stopping;
    input.disabled = !enabled;
    empty.hidden = list.children.length > 0;
    list.setAttribute('aria-busy', running ? 'true' : 'false');
    for (const request of requests.values()) {
      for (const control of request.card.querySelectorAll('button, input, textarea')) {
        control.disabled = !ready || !request.fresh || request.sent;
      }
    }
  }

  function message(role, text) {
    const article = node('article', 'chat-message ' + role);
    const bubble = node('div', 'chat-bubble');
    const body = node('div', 'chat-text', text || '');
    bubble.append(node('p', 'chat-speaker', role === 'user' ? 'You' : 'Namako'), body);
    article.append(bubble);
    list.append(article);
    return { article, bubble, body };
  }

  function assistant() {
    if (turn) return turn;
    turn = message('assistant', '');
    turn.progress = node('details', 'chat-steps');
    turn.progress.hidden = true;
    turn.progress.append(node('summary', '', 'Progress'));
    turn.steps = new Map();
    turn.outcome = node('p', 'chat-outcome');
    turn.bubble.append(turn.progress, turn.outcome);
    return turn;
  }

  function interim(text) {
    const current = assistant();
    if (text) {
      current.progress.append(node('p', 'chat-text', text));
      current.progress.hidden = false;
    }
    current.body.textContent = '';
  }

  function step(event) {
    const current = assistant();
    let item = current.steps.get(event.id);
    if (!item) {
      const section = node('section', 'chat-step');
      item = { title: node('div', 'chat-step-title'), input: node('pre'), output: node('pre') };
      section.append(item.title, item.input, item.output);
      current.progress.append(section);
      current.progress.hidden = false;
      current.steps.set(event.id, item);
    }
    item.title.textContent = (event.tool || 'Tool') + (event.type === 'step_start' ? ' · Working…' : ' · Finished');
    const format = value => typeof value === 'string' ? value : JSON.stringify(value ?? {}, null, 2);
    if (event.type === 'step_start') item.input.textContent = format(event.input);
    else item.output.textContent = format(event.output);
  }

  function answerRequest(request, answer) {
    if (!request.fresh || request.sent) return;
    if (write({ type: 'answer', id: request.id, answer })) {
      request.sent = true;
      request.note.textContent = 'Answer sent. Waiting for server confirmation…';
      controls();
    }
  }

  function questionField(question, initial) {
    const field = node('fieldset', 'chat-question');
    field.append(node('legend', '', question.question || 'Your answer'));
    const choices = Array.isArray(question.choices) ? question.choices : [];
    const name = 'chat-question-' + (++questionSequence);
    const options = [];
    for (const choice of choices) {
      const value = typeof choice === 'string' ? choice : String(choice.value ?? choice.label ?? choice.id ?? '');
      const label = node('label', 'chat-choice');
      const option = node('input');
      option.type = question.multi_select ? 'checkbox' : 'radio';
      option.name = name;
      option.value = value;
      option.checked = typeof initial === 'string' && (question.multi_select ? initial.split(', ').includes(value) : initial === value);
      label.append(option, node('span', '', typeof choice === 'string' ? choice : choice.label || value));
      field.append(label);
      options.push(option);
    }
    const label = node('label', 'chat-answer-text');
    label.append(node('span', '', choices.length ? 'Other answer (optional; replaces selected choices)' : 'Answer'));
    const text = node('textarea');
    text.rows = 2;
    if (typeof initial === 'string' && !options.some(option => option.checked)) text.value = initial;
    label.append(text);
    field.append(label);
    return { field, value: () => text.value.trim() || options.filter(option => option.checked).map(option => option.value).join(', ') };
  }

  function showRequest(event) {
    if (!event.id || !['approval', 'clarify'].includes(event.kind)) return;
    const previous = requests.get(event.id);
    // Replays re-enable this exact request, preserving any unsent answer draft.
    if (previous) {
      if (previous.fresh) return;
      previous.fresh = true;
      previous.sent = false;
      previous.note.textContent = 'Waiting for your answer.';
      return;
    }
    const params = event.params || {};
    const card = node('section', 'chat-request');
    const heading = node('h2', '', event.kind === 'approval' ? 'Permission requested' : 'Namako has a question');
    const note = node('p', 'chat-request-note', 'Waiting for your answer.');
    note.setAttribute('role', 'status');
    const request = { id: event.id, card, note, fresh: true, sent: false };
    card.append(heading);
    if (event.kind === 'approval') {
      card.append(node('p', 'chat-text', params.description || 'Review this command before allowing it to run.'), node('pre', 'chat-command', params.command || 'No command supplied.'));
      const actions = node('div', 'chat-actions');
      for (const choice of ['once', 'deny']) {
        // Never offer a persistent grant, including if an upstream adds one.
        if (choice === 'once' && (!params.command || !(params.choices || []).includes('once'))) continue;
        const button = node('button', 'ghost', choice === 'once' ? 'Allow once' : 'Deny');
        button.type = 'button';
        button.addEventListener('click', () => answerRequest(request, { choice }));
        actions.append(button);
      }
      card.append(actions);
    } else {
      const answerForm = node('form', 'chat-answer-form');
      const multiple = Array.isArray(params.questions) && params.questions.length > 0;
      const questions = multiple ? params.questions : [params];
      const fields = questions.map(question => {
        const entry = questionField(question, params.answers && params.answers[question.qid]);
        answerForm.append(entry.field);
        return { qid: question.qid, ...entry };
      });
      const button = node('button', 'ghost', 'Send answer');
      button.type = 'submit';
      answerForm.append(button);
      answerForm.addEventListener('submit', function (submitEvent) {
        submitEvent.preventDefault();
        if (fields.some(field => !field.value())) {
          note.textContent = 'Please answer each question.';
          return;
        }
        const answer = multiple ? { answers: Object.fromEntries(fields.map(field => [field.qid, field.value()])) } : { answer: fields[0].value() };
        answerRequest(request, answer);
      });
      card.append(answerForm);
    }
    card.append(note);
    requests.set(event.id, request);
    requestList.append(card);
    status.textContent = 'Waiting for your answer.';
  }

  function cancelRequest(id) {
    const request = requests.get(id);
    if (!request) return;
    const hadFocus = request.card.contains(document.activeElement);
    request.card.remove();
    requests.delete(id);
    if (hadFocus) input.focus();
    if (!requests.size) status.textContent = running ? 'Namako is working…' : 'Connected';
  }

  function receive(event) {
    if (!event || typeof event.type !== 'string') return;
    // Follow new output only when the reader is already at the bottom.
    const follow = list.scrollHeight - list.scrollTop - list.clientHeight < 80;
    switch (event.type) {
      case 'snapshot': {
        if (Array.isArray(event.open_request_ids)) {
          const open = new Set(event.open_request_ids);
          for (const id of requests.keys()) if (!open.has(id)) cancelRequest(id);
        }
        list.replaceChildren();
        const historyWindow = event.history_window;
        if (historyWindow) {
          const disclosure = node('p', 'chat-outcome', historyWindow.status === 'recent'
            ? `Showing chat messages from a recent saved-history window (up to ${historyWindow.limit} stored entries). Older history remains in Hermes; this is not the full conversation.`
            : 'Saved history is unavailable in this view. History has not been deleted from Hermes; reconnect to retry.');
          disclosure.setAttribute('role', 'status');
          list.append(disclosure);
        }
        turn = null;
        const history = Array.isArray(event.messages) ? event.messages : [];
        for (const item of history) {
          if (item.role === 'user' || item.role === 'assistant') message(item.role, item.content);
        }
        if (event.inflight) {
          const live = event.inflight;
          const last = history[history.length - 1];
          if (live.user && !(last && last.role === 'user' && last.content === live.user)) message('user', live.user);
          const current = assistant();
          if (live.display_kind === 'interim') interim(live.assistant);
          else current.body.textContent = live.assistant || '';
          if (live.error) current.outcome.textContent = live.error;
        }
        running = !!event.running;
        stopping = false;
        pending = null;
        ready = true;
        retry = 0;
        window.clearTimeout(connectTimer);
        reconnect.hidden = true;
        notice(error, '');
        status.textContent = running ? 'Namako is working…' : 'Connected';
        break;
      }
      case 'accepted':
        if (pending && event.id === pending.id) {
          message('user', pending.text);
          turn = null;
          assistant();
          if (input.value === pending.draft) input.value = '';
          pending = null;
          running = true;
          notice(uncertain, '');
          status.textContent = 'Namako is working…';
        }
        break;
      case 'token':
        running = true;
        assistant().body.textContent += event.text || '';
        break;
      case 'interim':
        // Already-streamed text moves into progress; it is not appended twice.
        interim(event.text || (event.already_streamed && assistant().body.textContent) || '');
        break;
      case 'step_start':
      case 'step_end':
        step(event);
        break;
      case 'done': {
        const current = assistant();
        if (event.message || event.status === 'complete') current.body.textContent = event.message || '';
        current.outcome.textContent = event.status === 'error' ? 'Response failed.' : event.status === 'interrupted' ? 'Interrupted.' : '';
        running = false;
        stopping = false;
        status.textContent = event.status === 'complete' ? 'Connected' : current.outcome.textContent;
        turn = null;
        break;
      }
      case 'request':
        showRequest(event);
        break;
      case 'request_cancel':
        cancelRequest(event.id);
        break;
      case 'error':
        if (event.code === 'thread_in_use') {
          threadInUse = true;
          socket.close();
          break;
        }
        notice(error, event.message || 'Chat request failed.');
        if (event.terminal) {
          running = false;
          if (turn) turn.outcome.textContent = 'Response failed.';
          turn = null;
        }
        if (pending) pending = null; // Rejected submission: its draft is still present.
        stopping = false;
        status.textContent = running ? 'Namako is working…' : 'Request failed.';
        break;
      default:
        break;
    }
    controls();
    if (follow) list.scrollTop = list.scrollHeight;
  }

  function write(frame) {
    if (!ready || !socket || socket.readyState !== 1) return false;
    try {
      socket.send(JSON.stringify(frame));
      return true;
    } catch (_) {
      notice(error, 'Connection lost. Your draft has not been cleared.');
      socket.close();
      return false;
    }
  }

  function submit(event) {
    event.preventDefault();
    const text = input.value.trim();
    if (!ready || running || pending || !text) return;
    if (!window.crypto || !window.crypto.randomUUID) {
      notice(error, 'Sending requires a secure browser connection. Open this page over HTTPS or localhost.');
      return;
    }
    const submission = { id: window.crypto.randomUUID(), text, draft: input.value };
    if (write({ type: 'submit', id: submission.id, message: text })) {
      pending = submission;
      notice(error, '');
      status.textContent = 'Waiting for acceptance…';
      controls();
    }
  }

  function disconnected(event = {}) {
    window.clearTimeout(connectTimer);
    window.clearTimeout(retryTimer);
    if (event.code === 4409) threadInUse = true;
    ready = false;
    for (const request of requests.values()) {
      request.fresh = false;
      request.note.textContent = 'Disconnected. Waiting for the server to confirm this request is still pending.';
    }
    if (pending) {
      notice(uncertain, 'Delivery is uncertain. Your draft is kept; check the restored conversation before sending it again. Nothing is resent automatically.');
    }
    controls();
    if (leaving) return;
    if (threadInUse) {
      notice(error, 'Another tab is using this conversation. Close the other tab, then reconnect, or start a new chat.');
      status.textContent = 'Conversation already open.';
      reconnect.hidden = false;
      return;
    }
    if (retry < retryDelays.length) {
      status.textContent = 'Disconnected · Reconnecting…';
      retryTimer = window.setTimeout(connect, retryDelays[retry++]);
    } else {
      status.textContent = 'Disconnected. Reconnect when you are ready.';
      reconnect.hidden = false;
    }
  }

  function connect() {
    ready = false;
    reconnect.hidden = true;
    status.textContent = 'Connecting…';
    controls();
    let connection;
    try {
      connection = new window.WebSocket((window.location.protocol === 'https:' ? 'wss://' : 'ws://') + window.location.host + '/chat/socket');
    } catch (_) {
      disconnected();
      return;
    }
    socket = connection;
    connectTimer = window.setTimeout(() => connection.close(), 15000);
    connection.onopen = () => { status.textContent = 'Restoring conversation…'; };
    connection.onmessage = function (event) {
      if (connection !== socket) return;
      try { receive(JSON.parse(event.data)); }
      catch (_) { notice(error, 'Could not read the chat update. Reconnect to restore the conversation.'); connection.close(); }
    };
    connection.onerror = () => connection.close();
    connection.onclose = event => { if (connection === socket) disconnected(event); };
  }

  form.addEventListener('submit', submit);
  input.addEventListener('input', controls);
  input.addEventListener('keydown', function (event) {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) submit(event);
  });
  stop.addEventListener('click', function () {
    if (running && !stopping && write({ type: 'stop' })) {
      stopping = true;
      status.textContent = 'Stopping…';
      controls();
    }
  });
  reconnect.addEventListener('click', function () {
    window.clearTimeout(retryTimer);
    threadInUse = false;
    retry = 0;
    connect();
  });
  window.addEventListener('pagehide', function () {
    leaving = true;
    window.clearTimeout(retryTimer);
    window.clearTimeout(connectTimer);
    if (socket) socket.close();
  });
  window.addEventListener('pageshow', function (event) {
    if (event.persisted && enabled) {
      leaving = false;
      retry = 0;
      connect();
    }
  });
  input.value = shell.dataset.seedMessage || '';
  controls();
  if (enabled) connect();
})();
