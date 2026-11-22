'use strict';
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const {test} = require('node:test');
const vm = require('node:vm');

// Execute the shipped script. Only browser elements and the network boundary are
// replaced; the actual submit listener must survive the browser clearing currentTarget.
async function loginPage() {
  const elements = new Map();
  const document = {
    querySelector(selector) {
      if (!elements.has(selector)) elements.set(selector, {
        hidden: false, textContent: '', listeners: {},
        addEventListener(type, listener) { this.listeners[type] = listener; },
        focus() {},
      });
      return elements.get(selector);
    },
    querySelectorAll() { return []; },
    addEventListener() {},
  };
  const context = vm.createContext({
    document, window: {addEventListener() {}}, FormData: class {},
    fetch: async () => ({ok: false, status: 401, json: async () => ({detail: 'Sign in to continue.'})}),
    setInterval() {}, setTimeout() {}, clearTimeout() {},
  });
  vm.runInContext(readFileSync(resolve(__dirname, '../app/static/app.js'), 'utf8'), context);
  await new Promise(setImmediate); // Let the initial signed-out request settle.
  const form = document.querySelector('#login-form');
  form.elements = {password: {value: 'test-only-password'}};
  form.resets = 0;
  form.reset = () => { form.resets++; form.elements.password.value = ''; };
  const page = {context, form, error: document.querySelector('#login-error'), starts: 0, requests: []};
  context.testApi = (path, method, body) => {
    page.requests.push({path, method, body});
    return new Promise((resolve, reject) => { page.accept = resolve; page.reject = reject; });
  };
  context.testStart = async () => { page.starts++; };
  vm.runInContext('api = testApi; start = testStart;', context);
  page.submit = () => {
    const event = {currentTarget: form, preventDefault() {}};
    const completion = form.listeners.submit(event);
    // A real browser dispatch returns before the asynchronous request completes.
    event.currentTarget = null;
    return completion;
  };
  return page;
}

test('successful delayed login opens the desk without refreshing or showing an error', async () => {
  const page = await loginPage();
  const completion = page.submit();
  assert.equal(page.requests[0].path, '/api/login');
  assert.equal(page.requests[0].method, 'POST');
  assert.equal(page.requests[0].body.password, 'test-only-password');
  page.accept({ok: true});
  await completion;
  assert.equal(page.error.textContent, '');
  assert.equal(page.form.resets, 1);
  assert.equal(page.starts, 1);
});

test('bulk rewrite runs as one server batch and keeps unfinished clips selected', async () => {
  const {context} = await loginPage();
  const requests = [];
  context.rewriteApi = async (path, method, body) => {
    requests.push({path, method, body});
    if (path === '/api/rewrite-batches') return {id: 'b1', status: 'running', total: 3, done: 0, failed: 0, skipped: 0, items: []};
    return {batch: {id: 'b1', status: 'paused', total: 3, done: 1, failed: 0, skipped: 0, message: 'Paused: credit is exhausted.',
      items: [{clip_id: 'first', status: 'done'}, {clip_id: 'second', status: 'pending'}, {clip_id: 'third', status: 'pending'}]}};
  };
  const remaining = await vm.runInContext(`
    state = {settings: {}};
    $('#shell').hidden = false;
    currentPage = 'library';
    records = [{id:'first'}, {id:'unselected'}, {id:'second'}, {id:'third'}];
    selectedClips.set('first','First').set('second','Second').set('third','Third');
    batchGuidance = 'Keep it playful.';
    api = rewriteApi;
    captionControls = () => {};
    refresh = async () => {};
    regenerateSelected().then(() => watchBatch()).then(() => [...selectedClips.keys()].join(',') + '|' + captionProgress);
  `, context);
  const [selection, progress] = remaining.split('|');
  assert.equal(selection, 'second,third');
  assert.match(progress, /credit is exhausted/);
  assert.equal(JSON.stringify(requests[0]), JSON.stringify({path: '/api/rewrite-batches', method: 'POST', body: {clips: ['first','second','third'], instructions: 'Keep it playful.'}}));
  assert.equal(requests[1].path, '/api/rewrite-batches/latest');
});

test('rejected login preserves the input and allows a successful retry', async () => {
  const page = await loginPage();
  let completion = page.submit();
  page.reject(new Error('Incorrect password.'));
  await completion;
  assert.equal(page.error.textContent, 'Incorrect password.');
  assert.equal(page.form.elements.password.value, 'test-only-password');
  assert.equal(page.form.resets, 0);
  assert.equal(page.starts, 0);
  page.form.elements.password.value = 'corrected-test-password';
  completion = page.submit();
  assert.equal(page.error.textContent, '');
  assert.equal(page.requests[1].body.password, 'corrected-test-password');
  page.accept({ok: true});
  await completion;
  assert.equal(page.error.textContent, '');
  assert.equal(page.form.resets, 1);
  assert.equal(page.starts, 1);
});
